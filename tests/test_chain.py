import hashlib

import pytest

from polybit import chain, ledger
from polybit.money import pb

ALICE, BOB = 101, 102


def _leaf(i: int) -> bytes:
    return hashlib.sha256(str(i).encode()).digest()


@pytest.mark.parametrize("n", range(1, 10))
def test_merkle_proof_works_for_every_leaf(n):
    leaves = [_leaf(i) for i in range(n)]
    root = chain.merkle_root(leaves)
    for i, leaf in enumerate(leaves):
        assert chain.verify_proof(leaf, chain.merkle_proof(leaves, i), root)
    assert not chain.verify_proof(_leaf(99), chain.merkle_proof(leaves, 0), root)


def test_single_leaf_root_is_the_leaf():
    assert chain.merkle_root([_leaf(1)]) == _leaf(1)


async def _txs(economy, n):
    await economy.register(ALICE)
    return [(await economy.pay(ALICE, BOB, pb(1))).journal_id for _ in range(n)]


async def test_seal_links_blocks(economy, pool):
    await _txs(economy, 3)  # + the signup grant = 4 entries
    b1 = await chain.seal_block(pool)
    assert (b1.height, b1.prev_hash, b1.tx_count) == (1, chain.GENESIS, 4)
    assert await chain.seal_block(pool) is None  # nothing new
    await economy.pay(ALICE, BOB, pb(1))
    b2 = await chain.seal_block(pool)
    assert (b2.height, b2.prev_hash, b2.tx_count) == (2, b1.hash, 1)
    assert await chain.get_block(pool) == b2
    assert await chain.get_block(pool, 1) == b1
    assert (await economy.audit()).ok


async def test_block_size_cap_carries_over(economy, pool):
    await _txs(economy, 4)  # 5 entries
    assert (await chain.seal_block(pool, max_tx=3)).tx_count == 3
    assert (await chain.seal_block(pool, max_tx=3)).tx_count == 2


async def test_late_commit_lands_in_next_block(economy, pool):
    await economy.register(ALICE)
    wallet_conn = await pool.acquire()
    try:
        tx = wallet_conn.transaction()
        await tx.start()
        # Touch only system accounts, so the payment below doesn't wait on this open transaction.
        mint = await wallet_conn.fetchval("SELECT id FROM accounts WHERE name = 'mint'")
        treasury = await wallet_conn.fetchval("SELECT id FROM accounts WHERE name = 'treasury'")
        late = await ledger.post(wallet_conn, kind="test", legs=[(mint, -1), (treasury, 1)])
        await economy.pay(ALICE, BOB, pb(1))  # higher id, commits first
        await chain.seal_block(pool)
        await tx.commit()
    finally:
        await pool.release(wallet_conn)
    b2 = await chain.seal_block(pool)
    assert b2 is not None and b2.tx_count == 1
    v = await chain.verify_entry(pool, late.journal_id)
    assert v.ok and v.block.height == 2


async def test_verify_entry_end_to_end(economy, pool):
    ids = await _txs(economy, 5)
    pending = await chain.verify_entry(pool, ids[2])
    assert pending.entry_ok and pending.block is None and not pending.ok
    await chain.seal_block(pool)
    for jid in ids:
        v = await chain.verify_entry(pool, jid)
        assert v.ok, jid
    assert await chain.verify_entry(pool, 999_999) is None


async def _forge(pool, sql, *args):
    async with pool.acquire() as conn:
        await conn.execute("SET session_replication_role = replica")  # skip append-only triggers
        await conn.execute(sql, *args)
        await conn.execute("SET session_replication_role = DEFAULT")


async def test_audit_catches_rehashed_forgery(economy, pool):
    """Forger edits an entry AND fixes its hash. The block's Merkle root still catches it."""
    ids = await _txs(economy, 2)
    await chain.seal_block(pool)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(ledger.ENTRY_QUERY + " WHERE j.id = $1 GROUP BY j.id", ids[0])
    forged = ledger.entry_hash(
        journal_id=row["id"], created_at=row["created_at"], kind=row["kind"], memo="forged",
        actor_id=row["actor_id"], guild_id=row["guild_id"], idempotency_key=row["idempotency_key"],
        legs=zip(row["accounts"], row["amounts"]),
    )
    await _forge(pool, "UPDATE journal SET memo = 'forged', hash = $2 WHERE id = $1", ids[0], forged)
    report = await economy.audit()
    assert any("Block #1 Merkle root" in p for p in report.problems)
    assert not (await chain.verify_entry(pool, ids[0])).ok


async def test_audit_catches_edited_block(economy, pool):
    await _txs(economy, 1)
    await chain.seal_block(pool)
    await economy.pay(ALICE, BOB, pb(1))
    await chain.seal_block(pool)
    await _forge(pool, "UPDATE blocks SET tx_count = tx_count + 1 WHERE height = 1")
    problems = (await economy.audit()).problems
    assert any("Block #1 hash" in p for p in problems)


async def test_blocks_are_append_only(economy, pool):
    import asyncpg

    await _txs(economy, 1)
    await chain.seal_block(pool)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("DELETE FROM blocks")
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("UPDATE block_entries SET position = 0")
