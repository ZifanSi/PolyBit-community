"""Anchoring against a fake EVM JSON-RPC node that decodes and checks every raw transaction."""

import dataclasses

import asyncpg
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from polybit import anchor, chain
from polybit.config import ConfigError, Settings
from polybit.money import pb

ALICE, BOB = 101, 102
KEY = "0x" + "11" * 32
SEPOLIA = 84532


class FakeChain:
    def __init__(self, chain_id=SEPOLIA, base_fee=1_000_000):
        self.chain_id = chain_id
        self.base_fee = base_fee
        self.txs: dict[str, dict] = {}
        self.mined: dict[str, int] = {}  # tx hash -> receipt status
        self.height = 100
        self.fail_send = False

    def mine(self, status=1):
        for h in self.txs:
            self.mined.setdefault(h, status)
        self.height += 1

    async def handle(self, request):
        body = await request.json()
        method, params = body["method"], body["params"]
        result = None
        if method == "eth_chainId":
            result = hex(self.chain_id)
        elif method == "eth_getTransactionCount":
            result = hex(sum(1 for t in self.txs.values() if t["from"] == params[0]))
        elif method == "eth_getBlockByNumber":
            result = {"number": hex(self.height), "baseFeePerGas": hex(self.base_fee)}
        elif method == "eth_maxPriorityFeePerGas":
            result = hex(1000)
        elif method == "eth_estimateGas":
            result = hex(21_704)
        elif method == "eth_sendRawTransaction":
            if self.fail_send:
                return web.json_response({"jsonrpc": "2.0", "id": body["id"], "error": {"message": "nope"}})
            raw = HexBytes(bytes.fromhex(params[0][2:]))
            fields = TypedTransaction.from_bytes(raw).as_dict()
            h = "0x" + keccak(raw).hex()
            self.txs[h] = {
                "hash": h, "from": Account.recover_transaction(raw), "to": to_checksum_address(fields["to"]),
                "input": "0x" + bytes(fields["data"]).hex(), "chainId": hex(fields["chainId"]),
                "fields": fields,
            }
            result = h
        elif method == "eth_getTransactionReceipt":
            h = params[0]
            if h in self.mined:
                result = {"status": hex(self.mined[h]), "blockNumber": hex(self.height)}
        elif method == "eth_getTransactionByHash":
            t = self.txs.get(params[0])
            result = None if t is None else {k: v for k, v in t.items() if k != "fields"}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
async def node():
    fake = FakeChain()
    app = web.Application()
    app.router.add_post("/", fake.handle)
    server = TestServer(app)
    await server.start_server()
    async with ClientSession() as session:
        fake.rpc = anchor.Rpc(session, str(server.make_url("/")))
        yield fake
    await server.close()


def _settings(settings, **kw):
    return dataclasses.replace(settings, anchor_rpc_url="http://localhost", anchor_key=KEY, **kw)


async def _block(economy, pool):
    await economy.register(ALICE)
    await economy.pay(ALICE, BOB, pb(1))
    return await chain.seal_block(pool)


async def test_nothing_to_anchor_without_blocks(pool, settings, node):
    assert await anchor.Anchorer(pool, _settings(settings), node.rpc).tick() is None
    assert node.txs == {}


async def test_anchor_lifecycle(economy, pool, settings, node):
    block = await _block(economy, pool)
    anchorer = anchor.Anchorer(pool, _settings(settings), node.rpc)

    sent = await anchorer.tick()
    assert sent.status == "pending" and sent.height == block.height
    tx = node.txs["0x" + sent.tx_hash.hex()]
    # A 0-value self-send on Base Sepolia carrying exactly the block's anchor bytes.
    assert tx["from"] == tx["to"] == anchorer.address == Account.from_key(KEY).address
    assert tx["fields"]["value"] == 0 and tx["fields"]["chainId"] == SEPOLIA
    assert anchor.parse_anchor_data(bytes(tx["fields"]["data"])) == (block.height, block.hash)

    assert await anchorer.tick() is None  # still pending: no second tx
    assert len(node.txs) == 1

    node.mine()
    settled = await anchorer.tick()
    assert settled.status == "confirmed" and settled.l1_block == node.height
    assert (await anchor.covering(pool, block.height)).tx_hash == sent.tx_hash

    checks = await anchor.verify_anchors(pool, node.rpc)
    assert [c.ok for c in checks] == [True]


async def test_waits_for_interval_then_anchors_latest(economy, pool, settings, node):
    await _block(economy, pool)
    anchorer = anchor.Anchorer(pool, _settings(settings), node.rpc)  # default: hourly
    await anchorer.tick()
    node.mine()
    await anchorer.tick()
    second = await _block(economy, pool)
    assert await anchorer.tick() is None  # anchored under an hour ago

    eager = anchor.Anchorer(pool, _settings(settings, anchor_minutes=1e-9), node.rpc)
    sent = await eager.tick()
    assert sent.height == second.height
    # Anchoring #2 pins #1 too, but #1 is covered by its own (earlier) anchor.
    assert (await anchor.covering(pool, 1)).height == 1


async def test_refuses_wrong_chain(economy, pool, settings, node):
    await _block(economy, pool)
    node.chain_id = 8453  # mainnet, but we're configured for Sepolia
    with pytest.raises(anchor.RpcError, match="expected 84532"):
        await anchor.Anchorer(pool, _settings(settings), node.rpc).tick()
    assert node.txs == {}


async def test_respects_gas_cap(economy, pool, settings, node):
    await _block(economy, pool)
    node.base_fee = 50 * anchor.GWEI
    assert await anchor.Anchorer(pool, _settings(settings), node.rpc).tick() is None
    assert node.txs == {}


async def test_failed_send_and_revert_are_retried(economy, pool, settings, node):
    await _block(economy, pool)
    anchorer = anchor.Anchorer(pool, _settings(settings), node.rpc)
    node.fail_send = True
    with pytest.raises(anchor.RpcError):
        await anchorer.tick()
    node.fail_send = False
    await anchorer.tick()
    node.mine(status=0)  # reverted
    assert (await anchorer.tick()).status == "failed"
    retry = await anchorer.tick()
    assert retry.status == "pending"
    async with pool.acquire() as conn:
        statuses = [r["status"] for r in await conn.fetch("SELECT status FROM anchors ORDER BY id")]
    assert statuses == ["failed", "failed", "pending"]


async def test_verify_catches_rewritten_history(economy, pool, settings, node):
    await _block(economy, pool)
    anchorer = anchor.Anchorer(pool, _settings(settings), node.rpc)
    await anchorer.tick()
    node.mine()
    await anchorer.tick()
    async with pool.acquire() as conn:
        await conn.execute("SET session_replication_role = replica")
        await conn.execute("UPDATE blocks SET hash = $1 WHERE height = 1", b"\x01" * 32)
        await conn.execute("SET session_replication_role = DEFAULT")
    [check] = await anchor.verify_anchors(pool, node.rpc)
    assert not check.ok and "no longer matches" in check.problem


async def test_anchor_records_settle_once(economy, pool, settings, node):
    await _block(economy, pool)
    anchorer = anchor.Anchorer(pool, _settings(settings), node.rpc)
    await anchorer.tick()
    node.mine()
    await anchorer.tick()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="settles once"):
            await conn.execute("UPDATE anchors SET status = 'pending'")
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("DELETE FROM anchors")


def test_anchor_data_roundtrip():
    data = anchor.anchor_data(7, b"\xab" * 32)
    assert len(data) == 44 and anchor.parse_anchor_data(data) == (7, b"\xab" * 32)
    assert anchor.parse_anchor_data(b"nope") is None


@pytest.mark.parametrize(
    "overrides, message",
    [
        (dict(anchor_key=""), "PB_ANCHOR_KEY"),
        (dict(anchor_key="0x1234"), "PB_ANCHOR_KEY"),
        (dict(anchor_rpc_url="http://rpc.example.com"), "https"),
        (dict(anchor_minutes=0), "PB_ANCHOR_MINUTES"),
        (dict(anchor_explorer="http://x"), "PB_ANCHOR_EXPLORER"),
    ],
)
def test_anchor_config_is_checked(overrides, message):
    settings = dataclasses.replace(Settings(), anchor_rpc_url="https://sepolia.base.org", anchor_key=KEY)
    settings.validate()
    with pytest.raises(ConfigError, match=message):
        dataclasses.replace(settings, **overrides).validate()


def test_secrets_stay_out_of_repr():
    settings = dataclasses.replace(Settings(), anchor_key=KEY, discord_token="tok")
    assert KEY not in repr(settings) and "tok" not in repr(settings)


def test_new_key_cli(capsys):
    from polybit.__main__ import main

    assert main(["anchors", "new-key"]) == 0
    out = capsys.readouterr().out
    key = out.splitlines()[1]
    assert Account.from_key(key).address in out


def test_anchor_line_links_to_explorer():
    from datetime import datetime, timezone

    from polybit.bot.cogs.chain import _anchor_line

    a = anchor.Anchor(1, 3, bytes(32), SEPOLIA, "0x" + "0" * 40, b"\xcd" * 32, "confirmed", 9,
                      datetime.now(timezone.utc), None)
    assert "https://sepolia.basescan.org/tx/0x" + "cd" * 32 in _anchor_line(Settings(), a)
    assert "not anchored" in _anchor_line(Settings(), None)
