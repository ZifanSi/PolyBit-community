"""Audit of self-custody wallets, from the ledger alone.

The users table says which wallet has which key, but it's an ordinary table. The
ledger is the source of truth: every key is bound by a signed `bind-key` entry, and
those entries are hashed and sealed into blocks like any other. From those, check:

* every signed entry's signature is valid for its stored bytes;
* a bind is signed by the key it binds, for the wallet that sent it, once per wallet;
* a signed transfer's legs are exactly what was signed, by the sender's bound key,
  under the idempotency key `tx:<sender>:<nonce>` (so it can't be applied twice);
* after a wallet's bind, every debit from it is signed by its key;
* the users table agrees with the binds.
"""

from __future__ import annotations

import json
from collections import Counter

import asyncpg

from polybit import signing

SIGNED_QUERY = """
    SELECT j.id, j.kind, j.actor_id, j.idempotency_key, j.signer, j.signature, j.payload,
           COALESCE(array_agg(a.owner_id) FILTER (WHERE p.id IS NOT NULL), '{}') AS owners,
           COALESCE(array_agg(a.name) FILTER (WHERE p.id IS NOT NULL), '{}') AS names,
           COALESCE(array_agg(p.amount) FILTER (WHERE p.id IS NOT NULL), '{}') AS amounts
    FROM journal j
    LEFT JOIN postings p ON p.journal_id = j.id
    LEFT JOIN accounts a ON a.id = p.account_id
    WHERE j.signature IS NOT NULL
    GROUP BY j.id
    ORDER BY j.id
"""


def _legs(row: asyncpg.Record) -> Counter:
    legs: Counter = Counter()
    for owner, name, amount in zip(row["owners"], row["names"], row["amounts"], strict=True):
        legs[("user", owner) if owner is not None else ("system", name)] += amount
    return legs


def expected_legs(tx: signing.Transfer) -> Counter:
    legs: Counter = Counter()
    legs[("user", tx.sender_id)] -= sum(a for _, a in tx.payouts) + tx.fee
    for uid, amount in tx.payouts:
        legs[("user", uid)] += amount
    if tx.fee:
        legs[("system", "treasury")] += tx.fee
    return legs


def _nonzero(legs: Counter) -> dict:
    return {k: v for k, v in legs.items() if v}


def _check(row: asyncpg.Record, binds: dict[int, tuple[int, bytes]]) -> str | None:
    """One signed entry. Returns what's wrong with it, or None."""
    jid, signer = row["id"], bytes(row["signer"])
    raw = row["payload"].encode("utf-8")
    if not signing.verify(signer, raw, bytes(row["signature"])):
        return f"Journal {jid} has a signature that doesn't verify."
    try:
        signed = signing.Signed(json.loads(raw), raw, bytes(row["signature"]))
    except ValueError:
        return f"Journal {jid} has a signed payload that isn't JSON."
    chain = signed.payload.get("chain")
    if not isinstance(chain, str):
        return f"Journal {jid} has a signed payload with no network name."
    try:
        if row["kind"] == "bind-key":
            user_id, pubkey = signing.check_bind(signed, chain=chain)
            if pubkey != signer or user_id != row["actor_id"] or row["idempotency_key"] != f"bind:{user_id}":
                return f"Journal {jid} binds a key it wasn't signed for."
            if row["owners"]:
                return f"Journal {jid} is a key binding that moves PB."
            if user_id in binds:
                return f"Journal {jid} binds a second key to wallet {user_id}."
            binds[user_id] = (jid, pubkey)
            return None
        if row["kind"] == "transfer":
            tx = signing.parse_transfer(signed, chain=chain, now=0)
            bound = binds.get(tx.sender_id)
            if bound is None or bound[1] != signer:
                return f"Journal {jid} is signed by a key that isn't bound to wallet {tx.sender_id}."
            if row["idempotency_key"] != f"tx:{tx.sender_id}:{tx.nonce}":
                return f"Journal {jid} doesn't carry its nonce key (replayable)."
            if _nonzero(_legs(row)) != _nonzero(expected_legs(tx)):
                return f"Journal {jid} moves different amounts than were signed."
            return None
    except signing.SignatureError as exc:
        return f"Journal {jid}: {exc.message}"
    return f"Journal {jid} is signed but is a {row['kind']!r} entry."


async def audit(conn: asyncpg.Connection, limit: int = 20) -> list[str]:
    """Runs inside the caller's (read-only, repeatable read) transaction."""
    problems: list[str] = []
    binds: dict[int, tuple[int, bytes]] = {}
    async for row in conn.cursor(SIGNED_QUERY):
        problem = _check(row, binds)
        if problem:
            problems.append(problem)
            if len(problems) >= limit:
                return problems

    if binds:
        async for row in conn.cursor(
            """
            SELECT j.id, j.signer, a.owner_id
            FROM postings p
            JOIN accounts a ON a.id = p.account_id
            JOIN journal j ON j.id = p.journal_id
            WHERE a.kind = 'user' AND p.amount < 0 AND a.owner_id = ANY($1::bigint[])
            ORDER BY j.id
            """,
            list(binds),
        ):
            bind_id, key = binds[row["owner_id"]]
            if row["id"] > bind_id and (row["signer"] is None or bytes(row["signer"]) != key):
                problems.append(
                    f"Journal {row['id']} takes PB from self-custody wallet {row['owner_id']} without its signature."
                )
                if len(problems) >= limit:
                    return problems

    keyed = {r["discord_id"]: bytes(r["pubkey"])
             for r in await conn.fetch("SELECT discord_id, pubkey FROM users WHERE pubkey IS NOT NULL")}
    for user_id in sorted(keyed.keys() | binds.keys()):
        if keyed.get(user_id) != (binds[user_id][1] if user_id in binds else None):
            problems.append(f"Wallet {user_id}'s key doesn't match its binding entry.")
    return problems[:limit]
