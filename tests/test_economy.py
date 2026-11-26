import asyncio
import random
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from polybit import ledger
from polybit.config import Settings
from polybit.economy import Economy
from polybit.errors import (
    AccountFrozen,
    AmountError,
    CooldownActive,
    InsufficientFunds,
    InvalidTransfer,
    PermissionDenied,
    PolicyDenied,
    RewardsLocked,
)
from polybit.money import pb
from polybit.policy import Policy
from tests.conftest import ADMIN_ID

ALICE, BOB, CAROL = 101, 102, 103
GUILD = 9000


# ------------------------------------------------------------------ registration

async def test_register_pays_signup_grant_once(economy):
    assert await economy.register(ALICE, GUILD) is True
    assert await economy.register(ALICE, GUILD) is False
    assert await economy.balance(ALICE) == pb(100)


async def test_receiving_before_registering_still_gets_grant_later(economy):
    await economy.register(ALICE)
    await economy.pay(ALICE, BOB, pb(10))
    assert await economy.balance(BOB) == pb(10)
    assert await economy.register(BOB) is True
    assert await economy.balance(BOB) == pb(110)


async def test_concurrent_register_grants_once(economy):
    results = await asyncio.gather(*(economy.register(ALICE) for _ in range(20)))
    assert results.count(True) == 1
    assert await economy.balance(ALICE) == pb(100)


# --------------------------------------------------------------------- transfers

async def test_pay_moves_funds(economy):
    await economy.register(ALICE)
    result = await economy.pay(ALICE, BOB, pb("12.5"), memo="lunch")
    assert result.sender_balance == pb("87.5")
    assert await economy.balance(BOB) == pb("12.5")


async def test_pay_insufficient_funds(economy):
    await economy.register(ALICE)
    with pytest.raises(InsufficientFunds):
        await economy.pay(ALICE, BOB, pb(101))
    assert await economy.balance(ALICE) == pb(100)
    assert await economy.balance(BOB) == 0


async def test_pay_self_rejected(economy):
    with pytest.raises(InvalidTransfer):
        await economy.pay(ALICE, ALICE, pb(1))


async def test_pay_is_idempotent(economy):
    await economy.register(ALICE)
    first = await economy.pay(ALICE, BOB, pb(10), idempotency_key="pay:1")
    second = await economy.pay(ALICE, BOB, pb(10), idempotency_key="pay:1")
    assert not first.replayed and second.replayed
    assert first.journal_id == second.journal_id
    assert await economy.balance(BOB) == pb(10)


async def test_pay_fee_goes_to_treasury(pool):
    eco = Economy(pool, Settings(signup_grant=pb(100), transfer_fee_bps=250))  # 2.5%
    await eco.register(ALICE)
    result = await eco.pay(ALICE, BOB, pb(40))
    assert result.fee == pb(1)
    assert result.sender_balance == pb(59)
    assert await eco.balance(BOB) == pb(40)
    assert (await eco.supply()).treasury == pb(1)


async def test_fee_counts_toward_insufficient_funds(pool):
    eco = Economy(pool, Settings(signup_grant=pb(100), transfer_fee_bps=100))
    await eco.register(ALICE)
    with pytest.raises(InsufficientFunds):
        await eco.pay(ALICE, BOB, pb(100))  # needs 101 with fee


async def test_concurrent_spending_never_overdraws(economy):
    await economy.register(ALICE)  # 100 PB

    async def attempt(i):
        try:
            await economy.pay(ALICE, BOB if i % 2 else CAROL, pb(10))
            return True
        except InsufficientFunds:
            return False

    results = await asyncio.gather(*(attempt(i) for i in range(50)))
    assert results.count(True) == 10
    assert await economy.balance(ALICE) == 0
    assert await economy.balance(BOB) + await economy.balance(CAROL) == pb(100)
    assert (await economy.audit()).ok


async def test_opposite_direction_payments_do_not_deadlock(economy):
    await economy.register(ALICE)
    await economy.register(BOB)
    await asyncio.wait_for(
        asyncio.gather(
            *(economy.pay(ALICE, BOB, pb(1)) for _ in range(25)),
            *(economy.pay(BOB, ALICE, pb(1)) for _ in range(25)),
        ),
        timeout=30,
    )
    assert await economy.balance(ALICE) == pb(100)
    assert await economy.balance(BOB) == pb(100)


# ---------------------------------------------------------------------- sendmany

async def test_pay_many_is_one_atomic_transaction(economy):
    await economy.register(ALICE)
    result = await economy.pay_many(ALICE, [(BOB, pb(10)), (CAROL, pb(15))], memo="red packet")
    assert result.amount == pb(25)
    assert result.sender_balance == pb(75)
    assert await economy.balance(BOB) == pb(10)
    assert await economy.balance(CAROL) == pb(15)
    tx = await economy.get_transaction(result.journal_id, viewer_id=CAROL)
    assert len(tx.legs) == 3


async def test_pay_many_all_or_nothing(economy):
    await economy.register(ALICE)  # 100 PB
    with pytest.raises(InsufficientFunds):
        await economy.pay_many(ALICE, [(BOB, pb(60)), (CAROL, pb(60))])
    assert await economy.balance(ALICE) == pb(100)
    assert await economy.balance(BOB) == 0
    assert await economy.balance(CAROL) == 0


@pytest.mark.parametrize(
    "payouts",
    [
        [],
        [(BOB, pb(1)), (BOB, pb(1))],
        [(BOB, pb(1)), (ALICE, pb(1))],
        [(200 + i, pb(1)) for i in range(11)],
        [(BOB, 0)],
    ],
    ids=["empty", "duplicate", "self", "too-many", "zero"],
)
async def test_pay_many_rejects_bad_payouts(economy, payouts):
    await economy.register(ALICE)
    with pytest.raises((InvalidTransfer, AmountError)):
        await economy.pay_many(ALICE, payouts)
    assert await economy.balance(ALICE) == pb(100)


async def test_pay_many_history_labels(economy):
    await economy.register(ALICE)
    await economy.pay_many(ALICE, [(BOB, pb(1)), (CAROL, pb(1))])
    [sent, _] = await economy.history(ALICE)
    assert sent.amount == -pb(2) and sent.counterparty_count == 2
    [received] = await economy.history(BOB)
    assert received.counterparty_id == ALICE and received.counterparty_count == 1


# ------------------------------------------------------------------------ frozen

async def test_frozen_wallet_cannot_send_or_claim_but_can_receive(economy):
    await economy.register(ALICE)
    await economy.register(BOB)
    await economy.set_frozen(ADMIN_ID, ALICE, True, reason="investigation")
    with pytest.raises(AccountFrozen):
        await economy.pay(ALICE, BOB, pb(1))
    with pytest.raises(AccountFrozen):
        await economy.claim_daily(ALICE)
    await economy.pay(BOB, ALICE, pb(5))
    assert await economy.balance(ALICE) == pb(105)

    await economy.set_frozen(ADMIN_ID, ALICE, False, reason="cleared")
    await economy.pay(ALICE, BOB, pb(1))


# ------------------------------------------------------------------------- daily

async def _rewind_daily(pool, user_id, hours):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE daily_claims SET last_claim_at = last_claim_at - $2::interval WHERE user_id = $1",
            user_id, timedelta(hours=hours),
        )


async def test_daily_cooldown(economy):
    first = await economy.claim_daily(ALICE)
    assert first.amount == pb(25) and first.streak == 1
    with pytest.raises(CooldownActive):
        await economy.claim_daily(ALICE)


async def test_daily_streak_grows_caps_and_resets(economy, pool):
    amounts = []
    for _ in range(6):
        amounts.append((await economy.claim_daily(ALICE)).amount)
        await _rewind_daily(pool, ALICE, 24)
    # base 25, +5 per extra day, capped at 3 bonus steps
    assert amounts == [pb(25), pb(30), pb(35), pb(40), pb(40), pb(40)]

    await _rewind_daily(pool, ALICE, 72)  # streak window (48h) missed
    result = await economy.claim_daily(ALICE)
    assert result.streak == 1 and result.amount == pb(25)


async def test_concurrent_daily_claims_pay_once(economy):
    results = await asyncio.gather(
        *(economy.claim_daily(ALICE) for _ in range(10)), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(isinstance(r, CooldownActive) for r in results if isinstance(r, Exception))
    assert await economy.balance(ALICE) == pb(25)


# ------------------------------------------------------------------------- admin

async def test_grant_and_revoke(economy):
    assert await economy.grant(ADMIN_ID, ALICE, pb(500), reason="event prize") == pb(500)
    assert await economy.revoke(ADMIN_ID, ALICE, pb(200), reason="refund") == pb(300)
    supply = await economy.supply()
    assert supply.issued == pb(500)
    assert supply.burned == pb(200)
    assert supply.circulating == pb(300)


async def test_revoke_cannot_overdraw(economy):
    await economy.grant(ADMIN_ID, ALICE, pb(5), reason="x")
    with pytest.raises(InsufficientFunds):
        await economy.revoke(ADMIN_ID, ALICE, pb(6), reason="x")


async def test_non_admin_rejected(economy):
    with pytest.raises(PermissionDenied):
        await economy.grant(ALICE, ALICE, pb(1), reason="free money")
    with pytest.raises(PermissionDenied):
        await economy.set_frozen(ALICE, BOB, True, reason="x")


# ---------------------------------------------------------------- history/board

async def test_history_shows_signed_amounts_and_counterparty(economy):
    await economy.register(ALICE)
    await economy.pay(ALICE, BOB, pb(7), memo="thanks")
    items = await economy.history(ALICE)
    assert [(i.kind, i.amount, i.counterparty_id) for i in items] == [
        ("transfer", -pb(7), BOB),
        ("signup", pb(100), None),
    ]
    assert items[0].memo == "thanks"
    bob_items = await economy.history(BOB)
    assert bob_items[0].amount == pb(7) and bob_items[0].counterparty_id == ALICE


async def test_get_transaction_visible_to_participants_and_admin_only(economy):
    await economy.register(ALICE)
    result = await economy.pay(ALICE, BOB, pb(7), memo="thanks")

    tx = await economy.get_transaction(result.journal_id, viewer_id=BOB)
    assert tx is not None and tx.kind == "transfer" and tx.memo == "thanks"
    assert [(leg.owner_id, leg.amount) for leg in tx.legs] == [(ALICE, -pb(7)), (BOB, pb(7))]

    assert await economy.get_transaction(result.journal_id, viewer_id=ALICE) is not None
    assert await economy.get_transaction(result.journal_id, viewer_id=ADMIN_ID) is not None
    assert await economy.get_transaction(result.journal_id, viewer_id=CAROL) is None
    assert await economy.get_transaction(999_999, viewer_id=ADMIN_ID) is None


async def test_get_transaction_shows_system_legs(economy):
    await economy.register(ALICE)
    [signup] = await economy.history(ALICE)
    tx = await economy.get_transaction(signup.journal_id, viewer_id=ALICE)
    assert [(leg.system_name, leg.owner_id, leg.amount) for leg in tx.legs] == [
        ("mint", None, -pb(100)),
        (None, ALICE, pb(100)),
    ]


async def test_leaderboard_is_per_guild(economy):
    await economy.register(ALICE, GUILD)
    await economy.register(BOB, GUILD)
    await economy.register(CAROL, GUILD + 1)
    await economy.pay(BOB, ALICE, pb(30))
    assert await economy.leaderboard(GUILD) == [(ALICE, pb(130)), (BOB, pb(70))]
    assert await economy.leaderboard(GUILD + 1) == [(CAROL, pb(100))]


# ------------------------------------------------------------ ledger invariants

async def test_db_rejects_unbalanced_entry(pool):
    async with pool.acquire() as conn:
        mint = await conn.fetchval("SELECT id FROM accounts WHERE name = 'mint'")
        with pytest.raises(asyncpg.RaiseError, match="unbalanced"):
            async with conn.transaction():
                jid = await conn.fetchval(
                    "INSERT INTO journal (kind, hash) VALUES ('hack', $1) RETURNING id", bytes(32)
                )
                await conn.execute(
                    "INSERT INTO postings (journal_id, account_id, amount) VALUES ($1, $2, -5)",
                    jid, mint,
                )


async def test_ledger_is_append_only(economy, pool):
    await economy.register(ALICE)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("UPDATE postings SET amount = amount * 2")
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute("DELETE FROM journal")


async def test_db_rejects_negative_user_balance(economy, pool):
    await economy.register(ALICE)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE accounts SET balance = -1 WHERE owner_id = $1", ALICE)


async def test_post_requires_transaction_and_balance(pool):
    async with pool.acquire() as conn:
        with pytest.raises(RuntimeError):
            await ledger.post(conn, kind="x", legs=[(1, -1), (2, 1)])
        async with conn.transaction():
            with pytest.raises(ValueError):
                await ledger.post(conn, kind="x", legs=[(1, -1), (2, 2)])


async def test_audit_detects_tampered_balance(economy, pool):
    await economy.register(ALICE)
    assert (await economy.audit()).ok
    async with pool.acquire() as conn:
        await conn.execute("UPDATE accounts SET balance = balance + 1 WHERE owner_id = $1", ALICE)
    report = await economy.audit()
    assert not report.ok
    assert any("cached balance" in p for p in report.problems)


async def test_random_activity_conserves_money(economy):
    rng = random.Random(1234)
    users = list(range(200, 212))
    for u in users:
        await economy.register(u)

    async def random_op():
        a, b = rng.sample(users, 2)
        roll = rng.random()
        try:
            if roll < 0.7:
                await economy.pay(a, b, rng.randint(1, pb(60)))
            elif roll < 0.85:
                await economy.claim_daily(a)
            elif roll < 0.95:
                await economy.grant(ADMIN_ID, a, rng.randint(1, pb(50)), reason="r")
            else:
                await economy.revoke(ADMIN_ID, a, rng.randint(1, pb(50)), reason="r")
        except (InsufficientFunds, CooldownActive):
            pass

    for _ in range(10):
        await asyncio.gather(*(random_op() for _ in range(30)))

    report = await economy.audit()
    assert report.ok, report.problems
    supply = await economy.supply()
    total_wallets = sum([await economy.balance(u) for u in users])
    assert supply.circulating == total_wallets
    assert supply.issued == supply.circulating + supply.burned + supply.treasury + supply.escrow


# ------------------------------------------------------------------------ policy

class _LockedUntil(Policy):
    def __init__(self, until):
        self.until = until

    async def reward_eligible_at(self, conn, user_id):
        return self.until if user_id == ALICE else None


class _SmallMintsOnly(Policy):
    async def check_mint(self, conn, actor_id, amount):
        if amount > pb(10):
            raise PolicyDenied("too much")


async def test_policy_can_hold_back_rewards(pool, settings):
    later = datetime.now(UTC) + timedelta(days=1)
    eco = Economy(pool, settings, _LockedUntil(later))
    assert await eco.register(ALICE) is False  # wallet created, grant held back
    assert await eco.balance(ALICE) == 0
    with pytest.raises(RewardsLocked):
        await eco.claim_daily(ALICE)
    assert await eco.register(BOB) is True
    assert await Economy(pool, settings).register(ALICE) is True  # paid once allowed


async def test_policy_can_block_mints(pool, settings):
    eco = Economy(pool, settings, _SmallMintsOnly())
    await eco.grant(ADMIN_ID, ALICE, pb(10), reason="ok")
    with pytest.raises(PolicyDenied):
        await eco.grant(ADMIN_ID, ALICE, pb(11), reason="no")
    assert await eco.balance(ALICE) == pb(10)


# ---------------------------------------------------------------------- treasury

async def test_treasury_pay_spends_collected_fees(pool, settings):
    eco = Economy(pool, Settings(**{**settings.__dict__, "transfer_fee_bps": 1000}))
    await eco.register(ALICE)
    await eco.pay(ALICE, BOB, pb(50))  # 10% fee -> 5 PB to treasury
    assert (await eco.supply()).treasury == pb(5)

    left = await eco.treasury_pay(ADMIN_ID, CAROL, pb(3), reason="bounty")
    assert left == pb(2)
    assert await eco.balance(CAROL) == pb(3)
    with pytest.raises(InvalidTransfer, match="treasury only holds"):
        await eco.treasury_pay(ADMIN_ID, CAROL, pb(5), reason="too much")
    assert (await eco.audit()).ok


async def test_treasury_pay_admin_only_and_idempotent(pool, settings):
    eco = Economy(pool, Settings(**{**settings.__dict__, "transfer_fee_bps": 1000}))
    await eco.register(ALICE)
    await eco.pay(ALICE, BOB, pb(50))
    with pytest.raises(PermissionDenied):
        await eco.treasury_pay(ALICE, ALICE, pb(1), reason="me")
    await eco.treasury_pay(ADMIN_ID, CAROL, pb(1), reason="x", idempotency_key="treasury:1")
    await eco.treasury_pay(ADMIN_ID, CAROL, pb(1), reason="x", idempotency_key="treasury:1")
    assert await eco.balance(CAROL) == pb(1)


# ------------------------------------------------------------------------ escrow

async def test_escrow_account_counts_in_supply(economy, pool):
    await economy.register(ALICE)
    assert (await economy.supply()).escrow == 0
    async with pool.acquire() as conn, conn.transaction():
        wallet = await conn.fetchval("SELECT id FROM accounts WHERE owner_id = $1", ALICE)
        escrow = await conn.fetchval("SELECT id FROM accounts WHERE name = 'escrow'")
        await ledger.post(conn, kind="test-hold", legs=[(wallet, -pb(30)), (escrow, pb(30))])
    supply = await economy.supply()
    assert supply.escrow == pb(30)
    assert supply.issued == supply.circulating + supply.burned + supply.treasury + supply.escrow
    assert (await economy.audit()).ok
