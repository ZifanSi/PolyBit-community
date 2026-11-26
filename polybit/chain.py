"""Blocks: ledger entries sealed into a hash-linked chain.

Every entry hashes itself when it's written (ledger.py). Every few minutes one sealer
takes all hashed entries that aren't in a block yet, builds a Merkle tree over their
hashes, and links the new block to the previous block's hash.

Edit an old entry and its block's Merkle root stops matching. Edit a block and every
block after it stops matching. `polybit audit` checks all of it.

Entries are picked by "not sealed yet", not by id range, so a transaction that commits
late (lower id, later commit) still lands in the next block instead of being skipped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from polybit import ledger

GENESIS = bytes(32)
SEAL_LOCK = 0x50424C4B  # advisory lock key: only one sealer at a time
MAX_BLOCK_TX = 5000


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def merkle_root(leaves: list[bytes]) -> bytes:
    """Bitcoin-style: pair up, hash, repeat; an odd one out is paired with itself."""
    if not leaves:
        return GENESIS
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_sha(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(leaves: list[bytes], index: int) -> list[tuple[bytes, bool]]:
    """Sibling hashes from leaf to root. The bool says the sibling sits on the right."""
    proof: list[tuple[bytes, bool]] = []
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = index ^ 1
        proof.append((level[sibling], sibling > index))
        level = [_sha(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
        index //= 2
    return proof


def verify_proof(leaf: bytes, proof: list[tuple[bytes, bool]], root: bytes) -> bool:
    node = leaf
    for sibling, on_right in proof:
        node = _sha(node + sibling) if on_right else _sha(sibling + node)
    return node == root


def block_hash(*, height: int, prev_hash: bytes, merkle: bytes, tx_count: int, created_at: datetime) -> bytes:
    body = {
        "v": 1,
        "height": height,
        "prev": prev_hash.hex(),
        "merkle": merkle.hex(),
        "count": tx_count,
        "at": created_at.astimezone(UTC).isoformat(timespec="microseconds"),
    }
    return _sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


@dataclass(frozen=True)
class Block:
    height: int
    hash: bytes
    prev_hash: bytes
    merkle_root: bytes
    tx_count: int
    created_at: datetime


def _block(row: asyncpg.Record) -> Block:
    return Block(
        height=row["height"],
        hash=bytes(row["hash"]),
        prev_hash=bytes(row["prev_hash"]),
        merkle_root=bytes(row["merkle_root"]),
        tx_count=row["tx_count"],
        created_at=row["created_at"],
    )


def _recompute(b: Block) -> bytes:
    return block_hash(
        height=b.height, prev_hash=b.prev_hash, merkle=b.merkle_root, tx_count=b.tx_count, created_at=b.created_at
    )


async def seal_block(pool: asyncpg.Pool, *, max_tx: int = MAX_BLOCK_TX) -> Block | None:
    """Seal every unsealed entry (up to max_tx) into a new block. None if there was nothing to seal."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SEAL_LOCK)
        last = await conn.fetchrow("SELECT height, hash FROM blocks ORDER BY height DESC LIMIT 1")
        rows = await conn.fetch(
            """
            SELECT j.id, j.hash FROM journal j
            WHERE j.hash IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM block_entries b WHERE b.journal_id = j.id)
            ORDER BY j.id
            LIMIT $1
            """,
            max_tx,
        )
        if not rows:
            return None
        height = last["height"] + 1 if last else 1
        prev = bytes(last["hash"]) if last else GENESIS
        root = merkle_root([bytes(r["hash"]) for r in rows])
        created_at = await conn.fetchval("SELECT now()")
        digest = block_hash(height=height, prev_hash=prev, merkle=root, tx_count=len(rows), created_at=created_at)
        await conn.execute(
            """
            INSERT INTO blocks (height, prev_hash, merkle_root, tx_count, hash, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            height, prev, root, len(rows), digest, created_at,
        )
        await conn.execute(
            """
            INSERT INTO block_entries (journal_id, height, position)
            SELECT id, $2, pos - 1 FROM unnest($1::bigint[]) WITH ORDINALITY AS t(id, pos)
            """,
            [r["id"] for r in rows], height,
        )
    return Block(height, digest, prev, root, len(rows), created_at)


async def get_block(pool: asyncpg.Pool, height: int | None = None) -> Block | None:
    """A block by height, or the latest one."""
    async with pool.acquire() as conn:
        if height is None:
            row = await conn.fetchrow("SELECT * FROM blocks ORDER BY height DESC LIMIT 1")
        else:
            row = await conn.fetchrow("SELECT * FROM blocks WHERE height = $1", height)
    return _block(row) if row else None


async def _leaves(conn: asyncpg.Connection, height: int) -> list[bytes]:
    rows = await conn.fetch(
        """
        SELECT j.hash FROM block_entries b JOIN journal j ON j.id = b.journal_id
        WHERE b.height = $1 ORDER BY b.position
        """,
        height,
    )
    return [bytes(r["hash"]) for r in rows]


@dataclass(frozen=True)
class Verification:
    journal_id: int
    entry_hash: bytes
    entry_ok: bool  # stored hash matches the entry's content
    block: Block | None  # None: not sealed yet
    position: int | None = None
    proof_ok: bool = False  # Merkle path from the entry reaches the block's root
    block_ok: bool = False  # block hash and link to the previous block hold

    @property
    def ok(self) -> bool:
        return self.entry_ok and self.block is not None and self.proof_ok and self.block_ok


async def verify_entry(pool: asyncpg.Pool, journal_id: int) -> Verification | None:
    """Check one entry end to end. None if it doesn't exist or predates hashing."""
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        row = await conn.fetchrow(ledger.ENTRY_QUERY + " WHERE j.id = $1 GROUP BY j.id", journal_id)
        if row is None or row["hash"] is None:
            return None
        stored = bytes(row["hash"])
        entry_ok = ledger.recompute(row) == stored
        placed = await conn.fetchrow("SELECT height, position FROM block_entries WHERE journal_id = $1", journal_id)
        if placed is None:
            return Verification(journal_id, stored, entry_ok, None)
        block = _block(await conn.fetchrow("SELECT * FROM blocks WHERE height = $1", placed["height"]))
        leaves = await _leaves(conn, block.height)
        proof_ok = verify_proof(stored, merkle_proof(leaves, placed["position"]), block.merkle_root)
        prev = await conn.fetchval("SELECT hash FROM blocks WHERE height = $1", block.height - 1)
        link_ok = block.prev_hash == (bytes(prev) if prev is not None else GENESIS)
        block_ok = _recompute(block) == block.hash and link_ok
    return Verification(journal_id, stored, entry_ok, block, placed["position"], proof_ok, block_ok)


async def audit_blocks(conn: asyncpg.Connection, limit: int = 20) -> list[str]:
    """Walk the whole chain: links, Merkle roots, block hashes. Runs inside the caller's transaction."""
    problems: list[str] = []
    prev = GENESIS
    expected_height = 1
    async for row in conn.cursor("SELECT * FROM blocks ORDER BY height"):
        b = _block(row)
        if b.height != expected_height:
            problems.append(f"Block #{expected_height} is missing.")
        if b.prev_hash != prev:
            problems.append(f"Block #{b.height} doesn't link to the block before it.")
        if _recompute(b) != b.hash:
            problems.append(f"Block #{b.height} hash doesn't match its header.")
        leaves = await _leaves(conn, b.height)
        if len(leaves) != b.tx_count or merkle_root(leaves) != b.merkle_root:
            problems.append(f"Block #{b.height} Merkle root doesn't match its entries.")
        prev = b.hash
        expected_height = b.height + 1
        if len(problems) >= limit:
            break
    return problems
