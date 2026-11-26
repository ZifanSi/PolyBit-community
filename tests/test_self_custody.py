"""Self-custody wallets: only the key holder can move the PB."""

import asyncio
import dataclasses

import asyncpg
import pytest

from polybit import chain, ledger, signing
from polybit.economy import Economy
from polybit.errors import AccountFrozen, InvalidTransfer, SelfCustody
from polybit.money import pb
from polybit.signing import SignatureError
from tests.conftest import ADMIN_ID

ALICE, BOB, CAROL = 101, 102, 103


async def _bind(economy, user_id=ALICE, key=None):
    key = key or signing.new_key()
    token = signing.sign(key, signing.bind_payload(chain="polybit", user_id=user_id, pubkey=signing.public_bytes(key)))
    await economy.register(user_id)
    await economy.bind_key(user_id, token)
    return key


def _tx(key, *, nonce, to=((BOB, pb(5)),), sender=ALICE, fee=0, memo=None):
    return signing.sign(key, signing.transfer_payload(
        chain="polybit", sender_id=sender, payouts=list(to), nonce=nonce, fee=fee, memo=memo,
    ))


async def test_signed_transfer_moves_pb(economy, pool):
    key = await _bind(economy)
    tx, result = await economy.submit(_tx(key, nonce=1, memo="rent"))
    assert (result.amount, result.sender_balance, result.replayed) == (pb(5), pb(95), False)
    assert await economy.balance(BOB) == pb(5)
    assert (await economy.wallet_key(ALICE)).next_nonce == 2
    shown = await economy.get_transaction(result.journal_id, viewer_id=BOB)
    assert shown.signer == signing.public_bytes(key) and shown.memo == "rent"
    assert (await economy.audit()).ok


async def test_custodial_paths_are_closed(economy, pool):
    await _bind(economy)
    with pytest.raises(SelfCustody):
        await economy.pay(ALICE, BOB, pb(1))
    with pytest.raises(SelfCustody, match="Nobody but its key holder"):
        await economy.revoke(ADMIN_ID, ALICE, pb(1), reason="nope")
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(SelfCustody):
            await economy.hold(conn, ALICE, pb(1))
    # Receiving still works.
    await economy.register(BOB)
    await economy.pay(BOB, ALICE, pb(1))
    await economy.grant(ADMIN_ID, ALICE, pb(1), reason="gift")
    assert await economy.balance(ALICE) == pb(102)


async def test_database_refuses_unsigned_debit(economy, pool):
    await _bind(economy)
    async with pool.acquire() as conn:
        wallet = await conn.fetchval("SELECT id FROM accounts WHERE owner_id = $1", ALICE)
        burn = await economy.system_account(conn, ledger.SYSTEM_BURN)
        with pytest.raises(asyncpg.RaiseError, match="self-custody"):
            async with conn.transaction():
                await ledger.post(conn, kind="revoke", legs=[(wallet, -pb(1)), (burn, pb(1))])


async def test_key_is_permanent_in_the_database(economy, pool):
    await _bind(economy)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="permanent"):
            await conn.execute("UPDATE users SET pubkey = NULL WHERE discord_id = $1", ALICE)


async def test_replay_and_nonces(economy):
    key = await _bind(economy)
    token = _tx(key, nonce=1)
    first = (await economy.submit(token))[1]
    again = (await economy.submit(token))[1]
    assert again.replayed and again.journal_id == first.journal_id
    assert await economy.balance(BOB) == pb(5)
    with pytest.raises(InvalidTransfer, match="already used. The next one is 2"):
        await economy.submit(_tx(key, nonce=1, to=[(BOB, pb(1))]))
    with pytest.raises(InvalidTransfer, match="too high"):
        await economy.submit(_tx(key, nonce=3))
    await economy.submit(_tx(key, nonce=2))
    assert await economy.balance(BOB) == pb(10)


async def test_wrong_key_or_no_key(economy):
    await _bind(economy)
    with pytest.raises(SignatureError, match="doesn't match"):
        await economy.submit(_tx(signing.new_key(), nonce=1))
    await economy.register(CAROL)
    with pytest.raises(SignatureError, match="no key"):
        await economy.submit(_tx(signing.new_key(), nonce=1, sender=CAROL))
    assert await economy.balance(ALICE) == pb(100)


async def test_bind_rules(economy):
    key = signing.new_key()
    proof = signing.sign(key, signing.bind_payload(chain="polybit", user_id=ALICE, pubkey=signing.public_bytes(key)))
    with pytest.raises(SignatureError, match="different user"):
        await economy.bind_key(BOB, proof)
    await economy.bind_key(ALICE, proof)
    with pytest.raises(InvalidTransfer, match="permanent"):
        await _bind(economy, ALICE)
    with pytest.raises(InvalidTransfer, match="another wallet"):
        await _bind(economy, BOB, key=key)


async def test_bind_is_on_the_chain(economy, pool):
    await _bind(economy)
    block = await chain.seal_block(pool)
    async with pool.acquire() as conn:
        bind_id = await conn.fetchval("SELECT id FROM journal WHERE kind = 'bind-key'")
    v = await chain.verify_entry(pool, bind_id)
    assert v.ok and v.block.height == block.height
    tx = await economy.get_transaction(bind_id, viewer_id=ALICE)
    assert tx.legs == [] and tx.signer is not None
    assert await economy.get_transaction(bind_id, viewer_id=BOB) is None


async def test_fee_is_signed_by_the_sender(pool, settings):
    economy = Economy(pool, dataclasses.replace(settings, transfer_fee_bps=100))
    key = await _bind(economy)
    with pytest.raises(InvalidTransfer, match="fee of at least 0.1 PB"):
        await economy.submit(_tx(key, nonce=1, to=[(BOB, pb(10))]))
    _, result = await economy.submit(_tx(key, nonce=1, to=[(BOB, pb(10))], fee=pb("0.2")))
    assert result.fee == pb("0.2") and result.sender_balance == pb("89.8")
    assert (await economy.supply()).treasury == pb("0.2")
    assert (await economy.audit()).ok


async def test_frozen_signer_cannot_spend(economy):
    key = await _bind(economy)
    await economy.set_frozen(ADMIN_ID, ALICE, True, reason="review")
    with pytest.raises(AccountFrozen):
        await economy.submit(_tx(key, nonce=1))


async def test_sendmany_style_signed(economy):
    key = await _bind(economy)
    await economy.submit(_tx(key, nonce=1, to=[(BOB, pb(1)), (CAROL, pb(2))]))
    assert (await economy.balance(BOB), await economy.balance(CAROL)) == (pb(1), pb(2))
    with pytest.raises(InvalidTransfer, match="yourself"):
        await economy.submit(_tx(key, nonce=2, to=[(ALICE, pb(1))]))


async def test_bind_racing_payments_leaves_a_clean_ledger(economy):
    # Payments either land before the bind (unsigned is fine then) or are refused.
    await economy.register(ALICE)
    key = signing.new_key()
    proof = signing.sign(key, signing.bind_payload(chain="polybit", user_id=ALICE, pubkey=signing.public_bytes(key)))

    async def pay():
        try:
            await economy.pay(ALICE, BOB, pb(1))
        except SelfCustody:
            pass

    await asyncio.gather(*[pay() for _ in range(10)], economy.bind_key(ALICE, proof), *[pay() for _ in range(10)])
    report = await economy.audit()
    assert report.ok, report.problems


# ---------------------------------------------------------------- audit

async def _raw(pool, sql, *args):
    async with pool.acquire() as conn:
        await conn.execute("SET session_replication_role = replica")  # skip the triggers
        try:
            await conn.execute(sql, *args)
        finally:
            await conn.execute("SET session_replication_role = DEFAULT")


async def test_audit_catches_debit_forged_in_the_database(economy, pool):
    await _bind(economy)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        wallet = await conn.fetchval("SELECT id FROM accounts WHERE owner_id = $1", ALICE)
        burn = await economy.system_account(conn, ledger.SYSTEM_BURN)
        await ledger.post(conn, kind="revoke", legs=[(wallet, -pb(50)), (burn, pb(50))])
    problems = (await economy.audit()).problems
    assert any("without its signature" in p for p in problems), problems


async def test_audit_catches_changed_amounts(economy, pool):
    key = await _bind(economy)
    _, result = await economy.submit(_tx(key, nonce=1))
    await _raw(pool, "UPDATE postings SET amount = amount * 2 WHERE journal_id = $1", result.journal_id)
    problems = (await economy.audit()).problems
    assert any("different amounts than were signed" in p for p in problems), problems


async def test_audit_catches_swapped_key(economy, pool):
    await _bind(economy)
    await _raw(pool, "UPDATE users SET pubkey = $2 WHERE discord_id = $1", ALICE,
               signing.public_bytes(signing.new_key()))
    problems = (await economy.audit()).problems
    assert any("doesn't match its binding" in p for p in problems), problems


async def test_audit_catches_bad_signature(economy, pool):
    key = await _bind(economy)
    _, result = await economy.submit(_tx(key, nonce=1))
    await _raw(pool, "UPDATE journal SET signature = $2 WHERE id = $1", result.journal_id, bytes(64))
    problems = (await economy.audit()).problems
    assert any("doesn't verify" in p for p in problems), problems
