import asyncpg
import pytest

from polybit import ledger
from polybit.money import pb

ALICE, BOB = 101, 102


async def _entry(pool, journal_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow(ledger.ENTRY_QUERY + " WHERE j.id = $1 GROUP BY j.id", journal_id)


async def test_every_entry_is_hashed_and_recomputable(economy, pool):
    await economy.register(ALICE)
    result = await economy.pay(ALICE, BOB, pb(5), memo="lunch")
    row = await _entry(pool, result.journal_id)
    assert len(row["hash"]) == 32
    assert ledger.recompute(row) == bytes(row["hash"])


async def test_hash_shown_on_transaction(economy):
    await economy.register(ALICE)
    result = await economy.pay(ALICE, BOB, pb(5))
    tx = await economy.get_transaction(result.journal_id, viewer_id=ALICE)
    assert tx.hash is not None and len(tx.hash) == 32


async def test_db_rejects_entry_without_hash(pool):
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="need a hash"):
            await conn.execute("INSERT INTO journal (kind) VALUES ('hack')")


async def test_audit_catches_edits_behind_the_apps_back(economy, pool):
    await economy.register(ALICE)
    result = await economy.pay(ALICE, BOB, pb(5), memo="honest")
    assert (await economy.audit()).ok
    async with pool.acquire() as conn:
        # Someone with direct DB access switches off the guard and rewrites the memo.
        await conn.execute("ALTER TABLE journal DISABLE TRIGGER journal_append_only")
        await conn.execute("UPDATE journal SET memo = 'forged' WHERE id = $1", result.journal_id)
        await conn.execute("ALTER TABLE journal ENABLE TRIGGER journal_append_only")
    report = await economy.audit()
    assert not report.ok
    assert any(f"Journal {result.journal_id}" in p and "hash" in p for p in report.problems)


def test_hash_covers_every_field():
    from datetime import datetime, timezone

    base = dict(
        journal_id=1, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), kind="transfer",
        memo=None, actor_id=None, guild_id=None, idempotency_key=None, legs=[(1, -5), (2, 5)],
    )
    h = ledger.entry_hash(**base)
    for field, other in [("journal_id", 2), ("kind", "grant"), ("memo", "x"), ("actor_id", 7),
                         ("guild_id", 8), ("idempotency_key", "k"), ("legs", [(1, -6), (2, 6)])]:
        assert ledger.entry_hash(**{**base, field: other}) != h, field
    assert ledger.entry_hash(**{**base, "legs": [(2, 5), (1, -5)]}) == h  # leg order doesn't matter
