"""Low-level double-entry posting.

`post()` is the only code path that moves money. It must run inside a transaction
and guarantees that:

* the legs of an entry sum to exactly zero (also enforced by a DB trigger);
* no account without `allow_negative` goes below zero (also a DB CHECK);
* accounts are locked in ascending id order, so concurrent postings cannot deadlock;
* an entry with an idempotency key is applied at most once.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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

    if idempotency_key is not None:
        journal_id = await conn.fetchval(
            """
            INSERT INTO journal (kind, idempotency_key, memo, actor_id, guild_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            kind, idempotency_key, memo, actor_id, guild_id,
        )
        if journal_id is None:
            existing = await conn.fetchval(
                "SELECT id FROM journal WHERE idempotency_key = $1", idempotency_key
            )
            return PostResult(journal_id=existing, replayed=True)
    else:
        journal_id = await conn.fetchval(
            """
            INSERT INTO journal (kind, memo, actor_id, guild_id)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            kind, memo, actor_id, guild_id,
        )

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
