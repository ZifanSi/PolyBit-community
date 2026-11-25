"""Low-level double-entry posting.

`post()` is the only code path that moves money. It must run inside a transaction
and guarantees that:

* the legs of an entry sum to exactly zero (also enforced by a DB trigger);
* no account without `allow_negative` goes below zero (also a DB CHECK);
* accounts are locked in ascending id order, so concurrent postings cannot deadlock;
* an entry with an idempotency key is applied at most once;
* every entry carries a SHA-256 of its full content (see `entry_hash`), so any later
  edit to an entry or its legs is detectable by `polybit audit`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import asyncpg

from polybit.errors import InsufficientFunds

SYSTEM_MINT = "mint"
SYSTEM_TREASURY = "treasury"
SYSTEM_BURN = "burn"
SYSTEM_ESCROW = "escrow"


@dataclass(frozen=True)
class PostResult:
    journal_id: int
    replayed: bool  # True if the idempotency key had already been used


def entry_hash(
    *,
    journal_id: int,
    created_at: datetime,
    kind: str,
    memo: str | None,
    actor_id: int | None,
    guild_id: int | None,
    idempotency_key: str | None,
    legs: Iterable[tuple[int, int]],
) -> bytes:
    """SHA-256 over a canonical JSON form of one journal entry and its legs.

    Anyone with a copy of the ledger can recompute it; changing any field changes the hash.
    """
    body = {
        "v": 1,
        "id": journal_id,
        "at": created_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "kind": kind,
        "memo": memo,
        "actor": actor_id,
        "guild": guild_id,
        "key": idempotency_key,
        "legs": sorted([int(a), int(amt)] for a, amt in legs),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).digest()


ENTRY_QUERY = """
    SELECT j.id, j.created_at, j.kind, j.memo, j.actor_id, j.guild_id, j.idempotency_key, j.hash,
           array_agg(p.account_id ORDER BY p.account_id) AS accounts,
           array_agg(p.amount ORDER BY p.account_id) AS amounts
    FROM journal j JOIN postings p ON p.journal_id = j.id
"""


def recompute(row: asyncpg.Record) -> bytes:
    """Hash of an entry as fetched with ENTRY_QUERY."""
    return entry_hash(
        journal_id=row["id"],
        created_at=row["created_at"],
        kind=row["kind"],
        memo=row["memo"],
        actor_id=row["actor_id"],
        guild_id=row["guild_id"],
        idempotency_key=row["idempotency_key"],
        legs=zip(row["accounts"], row["amounts"]),
    )


def _merge_legs(legs: Iterable[tuple[int, int]]) -> dict[int, int]:
    merged: dict[int, int] = defaultdict(int)
    for account_id, amount in legs:
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError("ledger amounts must be int units")
        merged[account_id] += amount
    return {acct: amt for acct, amt in merged.items() if amt != 0}


async def post(
    conn: asyncpg.Connection,
    *,
    kind: str,
    legs: Iterable[tuple[int, int]],
    memo: str | None = None,
    actor_id: int | None = None,
    guild_id: int | None = None,
    idempotency_key: str | None = None,
) -> PostResult:
    """Record one balanced journal entry and update cached balances."""
    if not conn.is_in_transaction():
        raise RuntimeError("ledger.post() must be called inside a transaction")

    merged = _merge_legs(legs)
    if not merged:
        raise ValueError("a journal entry needs at least one non-zero leg")
    if sum(merged.values()) != 0:
        raise ValueError("journal legs must sum to zero")

    # Id and timestamp are taken up front so the hash can be stored with the row itself.
    slot = await conn.fetchrow(
        "SELECT nextval(pg_get_serial_sequence('journal', 'id')) AS id, now() AS at"
    )
    digest = entry_hash(
        journal_id=slot["id"],
        created_at=slot["at"],
        kind=kind,
        memo=memo,
        actor_id=actor_id,
        guild_id=guild_id,
        idempotency_key=idempotency_key,
        legs=merged.items(),
    )
    journal_id = await conn.fetchval(
        """
        INSERT INTO journal (id, created_at, kind, idempotency_key, memo, actor_id, guild_id, hash)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        slot["id"], slot["at"], kind, idempotency_key, memo, actor_id, guild_id, digest,
    )
    if journal_id is None:  # idempotency key already used
        existing = await conn.fetchval(
            "SELECT id FROM journal WHERE idempotency_key = $1", idempotency_key
        )
        return PostResult(journal_id=existing, replayed=True)

    account_ids = sorted(merged)
    rows = await conn.fetch(
        """
        SELECT id, balance, allow_negative
        FROM accounts
        WHERE id = ANY($1::bigint[])
        ORDER BY id
        FOR UPDATE
        """,
        account_ids,
    )
    if len(rows) != len(account_ids):
        raise LookupError("journal references an unknown account")

    for row in rows:
        delta = merged[row["id"]]
        if delta < 0 and not row["allow_negative"] and row["balance"] + delta < 0:
            raise InsufficientFunds(available=row["balance"], required=-delta)

    deltas = [merged[a] for a in account_ids]
    await conn.executemany(
        "INSERT INTO postings (journal_id, account_id, amount) VALUES ($1, $2, $3)",
        [(journal_id, a, d) for a, d in zip(account_ids, deltas)],
    )
    await conn.execute(
        """
        UPDATE accounts AS a
        SET balance = a.balance + d.delta
        FROM unnest($1::bigint[], $2::bigint[]) AS d(id, delta)
        WHERE a.id = d.id
        """,
        account_ids, deltas,
    )
    return PostResult(journal_id=journal_id, replayed=False)
