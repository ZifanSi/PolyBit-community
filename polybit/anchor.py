"""Anchor the PB chain to a public blockchain (Base Sepolia by default).

Each block hash commits to every block before it, and each block's Merkle root commits
to every entry in it. So writing the latest block hash into a public chain now and then
pins the whole ledger: after that, nobody can rewrite PB history, us included, without
the result disagreeing with what's on Base for everyone to see.

An anchor is a 0-value transaction from the anchor address to itself carrying 44 bytes:

    b"PBA1" + height (uint64, big-endian) + block hash (32 bytes)

`polybit anchors` re-reads every confirmed anchor from the RPC and checks it against the
blocks in the database.

The anchor key only pays gas. It holds a little testnet ETH and nothing else, but it
still never leaves .env, and every fee is capped by PB_ANCHOR_MAX_GWEI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp
import asyncpg

from polybit.config import Settings

log = logging.getLogger(__name__)

MAGIC = b"PBA1"
ANCHOR_LOCK = 0x50424143  # advisory lock: one anchorer at a time
DROP_AFTER_MINUTES = 30
GWEI = 10**9


def anchor_data(height: int, block_hash: bytes) -> bytes:
    return MAGIC + height.to_bytes(8, "big") + block_hash


def parse_anchor_data(data: bytes) -> tuple[int, bytes] | None:
    if len(data) != 44 or not data.startswith(MAGIC):
        return None
    return int.from_bytes(data[4:12], "big"), data[12:]


class RpcError(Exception):
    pass


class Rpc:
    """Minimal JSON-RPC client. Only the handful of eth_ methods anchoring needs."""

    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self.session = session
        self.url = url
        self._id = 0

    async def call(self, method: str, *params):
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}
        try:
            async with self.session.post(self.url, json=body, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise RpcError(f"{method}: HTTP {resp.status}")
                reply = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise RpcError(f"{method}: {exc.__class__.__name__}") from None
        if not isinstance(reply, dict):
            raise RpcError(f"{method}: bad reply")
        if reply.get("error"):
            message = reply["error"].get("message", "error") if isinstance(reply["error"], dict) else "error"
            raise RpcError(f"{method}: {message}")
        return reply.get("result")


def _int(value) -> int:
    return int(value, 16)


@dataclass(frozen=True)
class Anchor:
    id: int
    height: int
    block_hash: bytes
    chain_id: int
    address: str
    tx_hash: bytes
    status: str
    l1_block: int | None
    sent_at: datetime
    settled_at: datetime | None


def _anchor(row: asyncpg.Record) -> Anchor:
    return Anchor(
        id=row["id"], height=row["height"], block_hash=bytes(row["block_hash"]), chain_id=row["chain_id"],
        address=row["address"], tx_hash=bytes(row["tx_hash"]), status=row["status"],
        l1_block=row["l1_block"], sent_at=row["sent_at"], settled_at=row["settled_at"],
    )


def tx_url(settings: Settings, anchor: Anchor) -> str:
    return f"{settings.anchor_explorer.rstrip('/')}/tx/0x{anchor.tx_hash.hex()}"


async def covering(pool: asyncpg.Pool, height: int) -> Anchor | None:
    """The first confirmed anchor at or above `height`: it pins that block too."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM anchors WHERE status = 'confirmed' AND height >= $1 ORDER BY height LIMIT 1", height
        )
    return _anchor(row) if row else None


class Anchorer:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, rpc: Rpc) -> None:
        from eth_account import Account

        self.pool = pool
        self.settings = settings
        self.rpc = rpc
        self.account = Account.from_key(settings.anchor_key)

    @property
    def address(self) -> str:
        return self.account.address

    async def tick(self) -> Anchor | None:
        """Settle the pending anchor if it's mined; otherwise maybe send a new one.
        Returns an anchor that was sent or settled this tick."""
        async with self.pool.acquire() as lock:
            if not await lock.fetchval("SELECT pg_try_advisory_lock($1)", ANCHOR_LOCK):
                return None
            try:
                pending = await lock.fetchrow("SELECT * FROM anchors WHERE status = 'pending'")
                if pending is not None:  # one in flight: wait for it before sending another
                    return await self._settle(pending)
                return await self._send()
            finally:
                await lock.execute("SELECT pg_advisory_unlock($1)", ANCHOR_LOCK)

    async def _settle(self, row: asyncpg.Record) -> Anchor | None:
        async with self.pool.acquire() as conn:
            receipt = await self.rpc.call("eth_getTransactionReceipt", "0x" + bytes(row["tx_hash"]).hex())
            if receipt is None:
                old = await conn.fetchval(
                    "SELECT $1 < now() - make_interval(mins => $2)", row["sent_at"], DROP_AFTER_MINUTES
                )
                if not old:
                    return None
                status, l1_block = "dropped", None
            else:
                status = "confirmed" if _int(receipt["status"]) == 1 else "failed"
                l1_block = _int(receipt["blockNumber"])
            settled = await conn.fetchrow(
                """
                UPDATE anchors SET status = $2, l1_block = $3, settled_at = now()
                WHERE id = $1 AND status = 'pending' RETURNING *
                """,
                row["id"], status, l1_block,
            )
        anchor = _anchor(settled)
        log.info("Anchor of block #%d %s (tx 0x%s)", anchor.height, status, anchor.tx_hash.hex())
        return anchor

    async def _send(self) -> Anchor | None:
        async with self.pool.acquire() as conn:
            block = await conn.fetchrow("SELECT height, hash FROM blocks ORDER BY height DESC LIMIT 1")
            last = await conn.fetchrow(
                """
                SELECT height, sent_at > now() - make_interval(secs => $1) AS recent
                FROM anchors WHERE status = 'confirmed' ORDER BY height DESC LIMIT 1
                """,
                self.settings.anchor_minutes * 60,
            )
        if block is None or (last is not None and (last["height"] >= block["height"] or last["recent"])):
            return None

        chain_id = _int(await self.rpc.call("eth_chainId"))
        if chain_id != self.settings.anchor_chain_id:
            # Fail closed: never sign for a chain we weren't configured for.
            raise RpcError(f"RPC is on chain {chain_id}, expected {self.settings.anchor_chain_id}")
        data = anchor_data(block["height"], bytes(block["hash"]))
        nonce = _int(await self.rpc.call("eth_getTransactionCount", self.address, "pending"))
        base_fee = _int((await self.rpc.call("eth_getBlockByNumber", "latest", False))["baseFeePerGas"])
        tip = _int(await self.rpc.call("eth_maxPriorityFeePerGas"))
        max_fee = 2 * base_fee + tip
        cap = int(self.settings.anchor_max_gwei * GWEI)
        if max_fee > cap:
            log.warning("Not anchoring: gas %.3f gwei is over the %.3f gwei cap", max_fee / GWEI, cap / GWEI)
            return None
        call = {"from": self.address, "to": self.address, "value": "0x0", "data": "0x" + data.hex()}
        gas = _int(await self.rpc.call("eth_estimateGas", call)) * 6 // 5
        signed = self.account.sign_transaction({
            "type": 2, "chainId": chain_id, "nonce": nonce, "to": self.address, "value": 0, "data": data,
            "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": min(tip, max_fee),
        })
        tx_hash = bytes(signed.hash)
        # Record first, then broadcast: a crash in between leaves a pending row that
        # settles as dropped, never an anchor on chain that we don't know about.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO anchors (height, block_hash, chain_id, address, tx_hash)
                VALUES ($1, $2, $3, $4, $5) RETURNING *
                """,
                block["height"], bytes(block["hash"]), chain_id, self.address, tx_hash,
            )
        try:
            await self.rpc.call("eth_sendRawTransaction", "0x" + bytes(signed.raw_transaction).hex())
        except RpcError:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE anchors SET status = 'failed', settled_at = now() WHERE id = $1", row["id"]
                )
            raise
        log.info("Anchoring block #%d in tx 0x%s", block["height"], tx_hash.hex())
        return _anchor(row)


@dataclass(frozen=True)
class AnchorCheck:
    anchor: Anchor
    ok: bool
    problem: str | None = None


async def verify_anchors(pool: asyncpg.Pool, rpc: Rpc) -> list[AnchorCheck]:
    """Re-read every confirmed anchor from the public chain and compare it to our blocks."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.*, b.hash AS current_hash
            FROM anchors a LEFT JOIN blocks b ON b.height = a.height
            WHERE a.status = 'confirmed' ORDER BY a.height
            """
        )
    results = []
    for row in rows:
        anchor = _anchor(row)
        problem = None
        tx = await rpc.call("eth_getTransactionByHash", "0x" + anchor.tx_hash.hex())
        receipt = await rpc.call("eth_getTransactionReceipt", "0x" + anchor.tx_hash.hex())
        if tx is None or receipt is None:
            problem = "transaction not found on chain"
        elif _int(receipt["status"]) != 1:
            problem = "transaction reverted"
        elif tx["from"].lower() != anchor.address.lower() or (tx.get("to") or "").lower() != anchor.address.lower():
            problem = "sent from or to the wrong address"
        elif tx.get("chainId") is not None and _int(tx["chainId"]) != anchor.chain_id:
            problem = "on the wrong chain"
        else:
            on_chain = parse_anchor_data(bytes.fromhex(tx["input"].removeprefix("0x")))
            current = bytes(row["current_hash"]) if row["current_hash"] is not None else None
            if on_chain is None:
                problem = "transaction doesn't carry an anchor"
            elif on_chain != (anchor.height, current):
                problem = f"block #{anchor.height} in the database no longer matches what's on chain"
        results.append(AnchorCheck(anchor, problem is None, problem))
    return results
