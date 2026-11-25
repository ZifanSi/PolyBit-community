"""PolyBit economy service: the business rules behind every bot command.

This module knows nothing about Discord. The bot is a thin interface on top of it,
which keeps the money logic testable and reusable by future components.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

import asyncpg

from polybit import chain, ledger
from polybit.config import Settings
from polybit.errors import (
    AccountFrozen,
    AmountError,
    CooldownActive,
    InsufficientFunds,
    InvalidTransfer,
    PermissionDenied,
    RewardsLocked,
)
from polybit.money import MAX_AMOUNT, format_amount
from polybit.policy import Policy

MEMO_MAX = 200
MAX_RECIPIENTS = 10


@dataclass(frozen=True)
class PayResult:
    journal_id: int
    amount: int
    fee: int
    sender_balance: int
    replayed: bool


@dataclass(frozen=True)
class DailyResult:
    amount: int
    streak: int
    balance: int
    next_claim_at: datetime


@dataclass(frozen=True)
class HistoryItem:
    journal_id: int
    kind: str
    amount: int  # signed, from the perspective of the queried wallet
    counterparty_id: int | None  # one wallet on the other side (lowest id if several)
    counterparty_count: int  # how many wallets are on the other side
    memo: str | None
    created_at: datetime


@dataclass(frozen=True)
class TxLeg:
    owner_id: int | None  # Discord user for wallet legs
    system_name: str | None  # 'mint' / 'treasury' / 'burn' for system legs
    amount: int


@dataclass(frozen=True)
class Transaction:
    id: int
    kind: str
    memo: str | None
    actor_id: int | None
    created_at: datetime
    legs: list[TxLeg]
    hash: bytes | None = None  # None for entries written before hashing existed


@dataclass(frozen=True)
class Supply:
    issued: int  # total ever minted
    burned: int
    treasury: int
    escrow: int  # held by extensions until settled
    circulating: int  # held in user wallets
    wallets: int


@dataclass(frozen=True)
class AuditReport:
    ok: bool
    problems: list[str]
    journal_entries: int
    accounts: int


def _validate_amount(amount: int) -> None:
    if amount <= 0:
        raise AmountError("Amount must be greater than zero.")
    if amount > MAX_AMOUNT:
        raise AmountError("Amount is too large.")


def _clean_memo(memo: str | None) -> str | None:
    if memo is None:
        return None
    memo = " ".join(memo.split())
    return memo[:MEMO_MAX] or None


class Economy:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, policy: Policy | None = None) -> None:
        self.pool = pool
        self.settings = settings
        self.policy = policy or Policy()
        self._system_ids: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers

    async def system_account(self, conn: asyncpg.Connection, name: str) -> int:
        if name not in self._system_ids:
            account_id = await conn.fetchval(
                "SELECT id FROM accounts WHERE kind = 'system' AND name = $1", name
            )
            if account_id is None:
                raise LookupError(f"missing system account {name!r}; run migrations")
            self._system_ids[name] = account_id
        return self._system_ids[name]

    async def ensure_wallet(self, conn: asyncpg.Connection, user_id: int) -> int:
        """Create the user and wallet if missing; return the wallet's account id."""
        await conn.execute(
            "INSERT INTO users (discord_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
        )
        account_id = await conn.fetchval(
            """
            INSERT INTO accounts (kind, owner_id) VALUES ('user', $1)
            ON CONFLICT (owner_id) DO NOTHING
            RETURNING id
            """,
            user_id,
        )
        if account_id is None:
            account_id = await conn.fetchval(
                "SELECT id FROM accounts WHERE owner_id = $1", user_id
            )
        return account_id

    async def require_not_frozen(self, conn: asyncpg.Connection, user_id: int) -> None:
        # FOR SHARE conflicts with a concurrent freeze, so the check cannot race it.
        frozen = await conn.fetchval(
            "SELECT frozen FROM users WHERE discord_id = $1 FOR SHARE", user_id
        )
        if frozen:
            raise AccountFrozen()

    def _require_admin(self, actor_id: int) -> None:
        if not self.settings.is_admin(actor_id):
            raise PermissionDenied()

    # ------------------------------------------------------------- registration

    async def register(self, user_id: int, guild_id: int | None = None) -> bool:
        """Called whenever a user interacts with PolyBit themselves.

        Creates their wallet, pays the one-time signup grant, and records the
        server they were seen in. Returns True if the welcome grant was paid now.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            wallet = await self.ensure_wallet(conn, user_id)
            if guild_id is not None:
                await conn.execute(
                    """
                    INSERT INTO user_guilds (user_id, guild_id) VALUES ($1, $2)
                    ON CONFLICT (user_id, guild_id) DO UPDATE SET last_seen_at = now()
                    """,
                    user_id, guild_id,
                )
            # Users the policy holds back keep their wallet and get the grant on a later use.
            eligible_at = await self.policy.reward_eligible_at(conn, user_id)
            if eligible_at is not None and await conn.fetchval("SELECT now()") < eligible_at:
                return False
            newly_welcomed = await conn.fetchval(
                """
                UPDATE users SET welcomed = TRUE
                WHERE discord_id = $1 AND NOT welcomed
                RETURNING TRUE
                """,
                user_id,
            )
            if not newly_welcomed:
                return False
            grant = self.settings.signup_grant
            if grant > 0:
                mint = await self.system_account(conn, ledger.SYSTEM_MINT)
                await ledger.post(
                    conn,
                    kind="signup",
                    legs=[(mint, -grant), (wallet, grant)],
                    memo="Welcome to PolyBit",
                    actor_id=user_id,
                    guild_id=guild_id,
                    idempotency_key=f"signup:{user_id}",
                )
            return True

    # ------------------------------------------------------------------- reads

    async def balance(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT balance FROM accounts WHERE owner_id = $1", user_id
            )
        return value or 0

    async def is_frozen(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return bool(
                await conn.fetchval("SELECT frozen FROM users WHERE discord_id = $1", user_id)
            )

    async def history(self, user_id: int, *, limit: int = 10, offset: int = 0) -> list[HistoryItem]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.id, j.kind, j.memo, j.created_at, p.amount,
                       cp.first_id AS counterparty_id, cp.n AS counterparty_count
                FROM accounts a
                JOIN postings p ON p.account_id = a.id
                JOIN journal  j ON j.id = p.journal_id
                CROSS JOIN LATERAL (
                    -- Other wallets on the opposite side of this entry.
                    SELECT MIN(a2.owner_id) AS first_id, COUNT(*) AS n
                    FROM postings p2
                    JOIN accounts a2 ON a2.id = p2.account_id
                    WHERE p2.journal_id = j.id
                      AND p2.account_id <> p.account_id
                      AND a2.kind = 'user'
                      AND sign(p2.amount) <> sign(p.amount)
                ) AS cp
                WHERE a.owner_id = $1
                ORDER BY j.id DESC
                LIMIT $2 OFFSET $3
                """,
                user_id, limit, offset,
            )
        return [
            HistoryItem(
                journal_id=r["id"],
                kind=r["kind"],
                amount=r["amount"],
                counterparty_id=r["counterparty_id"],
                counterparty_count=r["counterparty_count"],
                memo=r["memo"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def get_transaction(self, journal_id: int, *, viewer_id: int) -> Transaction | None:
        """Look up one transaction. Only its participants and admins may see it;
        for anyone else it is reported as not found."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, kind, memo, actor_id, created_at, hash FROM journal WHERE id = $1",
                journal_id,
            )
            if row is None:
                return None
            legs = await conn.fetch(
                """
                SELECT a.owner_id, a.name, p.amount
                FROM postings p JOIN accounts a ON a.id = p.account_id
                WHERE p.journal_id = $1
                ORDER BY p.amount ASC, p.id ASC
                """,
                journal_id,
            )
        participants = {leg["owner_id"] for leg in legs if leg["owner_id"] is not None}
        if viewer_id not in participants and not self.settings.is_admin(viewer_id):
            return None
        return Transaction(
            id=row["id"],
            kind=row["kind"],
            memo=row["memo"],
            actor_id=row["actor_id"],
            created_at=row["created_at"],
            legs=[TxLeg(owner_id=l["owner_id"], system_name=l["name"], amount=l["amount"]) for l in legs],
            hash=bytes(row["hash"]) if row["hash"] is not None else None,
        )

    async def leaderboard(self, guild_id: int, *, limit: int = 10) -> list[tuple[int, int]]:
        """Richest wallets among users seen in this server: [(user_id, balance)]."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.owner_id, a.balance
                FROM user_guilds g
                JOIN accounts a ON a.owner_id = g.user_id
                JOIN users u    ON u.discord_id = g.user_id
                WHERE g.guild_id = $1 AND a.balance > 0 AND NOT u.frozen
                ORDER BY a.balance DESC, a.id ASC
                LIMIT $2
                """,
                guild_id, limit,
            )
        return [(r["owner_id"], r["balance"]) for r in rows]

    async def supply(self) -> Supply:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  -COALESCE(SUM(balance) FILTER (WHERE name = 'mint'), 0)     AS issued,
                  COALESCE(SUM(balance) FILTER (WHERE name = 'burn'), 0)      AS burned,
                  COALESCE(SUM(balance) FILTER (WHERE name = 'treasury'), 0)  AS treasury,
                  COALESCE(SUM(balance) FILTER (WHERE name = 'escrow'), 0)    AS escrow,
                  COALESCE(SUM(balance) FILTER (WHERE kind = 'user'), 0)      AS circulating,
                  COUNT(*) FILTER (WHERE kind = 'user')                       AS wallets
                FROM accounts
                """
            )
        return Supply(
            issued=int(row["issued"]),
            burned=int(row["burned"]),
            treasury=int(row["treasury"]),
            escrow=int(row["escrow"]),
            circulating=int(row["circulating"]),
            wallets=int(row["wallets"]),
        )

    # ------------------------------------------------------------------ writes

    def transfer_fee(self, amount: int) -> int:
        return amount * self.settings.transfer_fee_bps // 10_000

    async def pay(
        self,
        sender_id: int,
        recipient_id: int,
        amount: int,
        *,
        memo: str | None = None,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> PayResult:
        """Move `amount` from sender to recipient. The fee (if any) is paid on top by the sender."""
        return await self.pay_many(
            sender_id,
            [(recipient_id, amount)],
            memo=memo,
            guild_id=guild_id,
            idempotency_key=idempotency_key,
        )

    async def pay_many(
        self,
        sender_id: int,
        payouts: Sequence[tuple[int, int]],
        *,
        memo: str | None = None,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> PayResult:
        """Pay several recipients in ONE atomic transaction (Bitcoin's `sendmany`).

        `payouts` is [(recipient_id, amount)]. Either every recipient is paid or none
        is. The fee (if any) is charged once on the total, on top, to the sender.
        `PayResult.amount` is the total sent, excluding the fee.
        """
        if not payouts:
            raise InvalidTransfer("Choose at least one recipient.")
        if len(payouts) > MAX_RECIPIENTS:
            raise InvalidTransfer(f"At most {MAX_RECIPIENTS} recipients per transaction.")
        recipient_ids = [r for r, _ in payouts]
        if len(set(recipient_ids)) != len(recipient_ids):
            raise InvalidTransfer("Each recipient may appear only once.")
        if sender_id in recipient_ids:
            raise InvalidTransfer("You can't send PB to yourself.")
        for _, amount in payouts:
            _validate_amount(amount)
        total = sum(amount for _, amount in payouts)
        _validate_amount(total)
        fee = self.transfer_fee(total)

        async with self.pool.acquire() as conn, conn.transaction():
            # Create wallets in a fixed (id) order so concurrent first-time inserts
            # of the same users cannot deadlock each other.
            wallets = {
                uid: await self.ensure_wallet(conn, uid)
                for uid in sorted({sender_id, *recipient_ids})
            }
            sender = wallets[sender_id]
            await self.require_not_frozen(conn, sender_id)
            legs = [(sender, -(total + fee))]
            legs += [(wallets[uid], amount) for uid, amount in payouts]
            if fee:
                legs.append((await self.system_account(conn, ledger.SYSTEM_TREASURY), fee))
            result = await ledger.post(
                conn,
                kind="transfer",
                legs=legs,
                memo=_clean_memo(memo),
                actor_id=sender_id,
                guild_id=guild_id,
                idempotency_key=idempotency_key,
            )
            balance = await conn.fetchval("SELECT balance FROM accounts WHERE id = $1", sender)
        return PayResult(
            journal_id=result.journal_id,
            amount=total,
            fee=fee,
            sender_balance=balance,
            replayed=result.replayed,
        )

    async def claim_daily(self, user_id: int, *, guild_id: int | None = None) -> DailyResult:
        s = self.settings
        cooldown = timedelta(hours=s.daily_cooldown_hours)
        streak_window = timedelta(hours=s.daily_streak_window_hours)

        async with self.pool.acquire() as conn, conn.transaction():
            wallet = await self.ensure_wallet(conn, user_id)
            await self.require_not_frozen(conn, user_id)
            await conn.execute(
                "INSERT INTO daily_claims (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
            )
            row = await conn.fetchrow(
                "SELECT last_claim_at, streak, now() AS now FROM daily_claims "
                "WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            now: datetime = row["now"]
            eligible_at = await self.policy.reward_eligible_at(conn, user_id)
            if eligible_at is not None and now < eligible_at:
                raise RewardsLocked(eligible_at)
            last: datetime | None = row["last_claim_at"]
            if last is not None and now - last < cooldown:
                raise CooldownActive(last + cooldown)

            streak = row["streak"] + 1 if last is not None and now - last <= streak_window else 1
            bonus_steps = min(streak - 1, s.daily_streak_max)
            amount = s.daily_amount + s.daily_streak_bonus * bonus_steps

            await conn.execute(
                "UPDATE daily_claims SET last_claim_at = $2, streak = $3 WHERE user_id = $1",
                user_id, now, streak,
            )
            if amount > 0:
                mint = await self.system_account(conn, ledger.SYSTEM_MINT)
                await ledger.post(
                    conn,
                    kind="daily",
                    legs=[(mint, -amount), (wallet, amount)],
                    memo=f"Daily reward (streak {streak})",
                    actor_id=user_id,
                    guild_id=guild_id,
                )
            balance = await conn.fetchval("SELECT balance FROM accounts WHERE id = $1", wallet)
        return DailyResult(amount=amount, streak=streak, balance=balance, next_claim_at=now + cooldown)

    # ------------------------------------------------------------------- admin

    async def grant(
        self, actor_id: int, user_id: int, amount: int, *, reason: str, guild_id: int | None = None
    ) -> int:
        """Mint new PB into a wallet. Returns the new balance."""
        self._require_admin(actor_id)
        _validate_amount(amount)
        async with self.pool.acquire() as conn, conn.transaction():
            await self.policy.check_mint(conn, actor_id, amount)
            wallet = await self.ensure_wallet(conn, user_id)
            mint = await self.system_account(conn, ledger.SYSTEM_MINT)
            await ledger.post(
                conn,
                kind="grant",
                legs=[(mint, -amount), (wallet, amount)],
                memo=_clean_memo(reason),
                actor_id=actor_id,
                guild_id=guild_id,
            )
            return await conn.fetchval("SELECT balance FROM accounts WHERE id = $1", wallet)

    async def revoke(
        self, actor_id: int, user_id: int, amount: int, *, reason: str, guild_id: int | None = None
    ) -> int:
        """Remove PB from a wallet into the burn account. Returns the new balance."""
        self._require_admin(actor_id)
        _validate_amount(amount)
        async with self.pool.acquire() as conn, conn.transaction():
            wallet = await self.ensure_wallet(conn, user_id)
            burn = await self.system_account(conn, ledger.SYSTEM_BURN)
            await ledger.post(
                conn,
                kind="revoke",
                legs=[(wallet, -amount), (burn, amount)],
                memo=_clean_memo(reason),
                actor_id=actor_id,
                guild_id=guild_id,
            )
            return await conn.fetchval("SELECT balance FROM accounts WHERE id = $1", wallet)

    async def treasury_pay(
        self,
        actor_id: int,
        user_id: int,
        amount: int,
        *,
        reason: str,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """Pay PB out of the treasury (collected fees) into a wallet. Returns what's left in the treasury."""
        self._require_admin(actor_id)
        _validate_amount(amount)
        async with self.pool.acquire() as conn, conn.transaction():
            wallet = await self.ensure_wallet(conn, user_id)
            treasury = await self.system_account(conn, ledger.SYSTEM_TREASURY)
            try:
                await ledger.post(
                    conn,
                    kind="treasury",
                    legs=[(treasury, -amount), (wallet, amount)],
                    memo=_clean_memo(reason),
                    actor_id=actor_id,
                    guild_id=guild_id,
                    idempotency_key=idempotency_key,
                )
            except InsufficientFunds as exc:
                raise InvalidTransfer(f"The treasury only holds {format_amount(exc.available)}.") from None
            return await conn.fetchval("SELECT balance FROM accounts WHERE id = $1", treasury)

    async def set_frozen(self, actor_id: int, user_id: int, frozen: bool, *, reason: str) -> None:
        self._require_admin(actor_id)
        async with self.pool.acquire() as conn, conn.transaction():
            await self.ensure_wallet(conn, user_id)
            await conn.execute(
                "UPDATE users SET frozen = $2 WHERE discord_id = $1", user_id, frozen
            )
            await conn.execute(
                """
                INSERT INTO audit_log (actor_id, action, target_id, details)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                actor_id,
                "freeze" if frozen else "unfreeze",
                user_id,
                json.dumps({"reason": _clean_memo(reason)}),
            )

    async def audit(self) -> AuditReport:
        """Verify ledger invariants. Read-only; safe to run at any time."""
        problems: list[str] = []
        async with self.pool.acquire() as conn, conn.transaction(
            isolation="repeatable_read", readonly=True
        ):
            total = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM accounts")
            if total != 0:
                problems.append(f"Sum of all account balances is {total}, expected 0.")

            unbalanced = await conn.fetch(
                "SELECT journal_id, SUM(amount) AS s FROM postings "
                "GROUP BY journal_id HAVING SUM(amount) <> 0 LIMIT 20"
            )
            for r in unbalanced:
                problems.append(f"Journal {r['journal_id']} sums to {r['s']}.")

            drift = await conn.fetch(
                """
                SELECT a.id, a.balance, COALESCE(SUM(p.amount), 0) AS computed
                FROM accounts a LEFT JOIN postings p ON p.account_id = a.id
                GROUP BY a.id
                HAVING a.balance <> COALESCE(SUM(p.amount), 0)
                LIMIT 20
                """
            )
            for r in drift:
                problems.append(
                    f"Account {r['id']} cached balance {r['balance']} != ledger {r['computed']}."
                )

            negative = await conn.fetch(
                "SELECT id, balance FROM accounts WHERE NOT allow_negative AND balance < 0 LIMIT 20"
            )
            for r in negative:
                problems.append(f"Account {r['id']} is negative ({r['balance']}).")

            # Recompute every entry hash: catches edits made behind the app's back.
            tampered = 0
            async for r in conn.cursor(ledger.ENTRY_QUERY + " WHERE j.hash IS NOT NULL GROUP BY j.id ORDER BY j.id"):
                if ledger.recompute(r) != bytes(r["hash"]):
                    tampered += 1
                    if tampered <= 20:
                        problems.append(f"Journal {r['id']} doesn't match its hash (edited after the fact).")

            problems += await chain.audit_blocks(conn)

            entries = await conn.fetchval("SELECT COUNT(*) FROM journal")
            accounts = await conn.fetchval("SELECT COUNT(*) FROM accounts")
        return AuditReport(ok=not problems, problems=problems, journal_entries=entries, accounts=accounts)
