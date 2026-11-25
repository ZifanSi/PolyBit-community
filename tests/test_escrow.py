import pytest

from polybit.errors import AccountFrozen, AmountError, InsufficientFunds
from polybit.money import pb
from tests.conftest import ADMIN_ID

BUYER, SELLER = 101, 102


async def _setup(economy):
    await economy.register(BUYER)
    await economy.register(SELLER)


async def test_hold_then_release_with_fee(economy, pool):
    await _setup(economy)
    async with pool.acquire() as conn, conn.transaction():
        await economy.hold(conn, BUYER, pb(30), memo="job")
    assert await economy.balance(BUYER) == pb(70)
    assert (await economy.supply()).escrow == pb(30)

    async with pool.acquire() as conn, conn.transaction():
        await economy.release(conn, SELLER, pb(30), fee=pb(3))
    assert await economy.balance(SELLER) == pb(127)
    supply = await economy.supply()
    assert (supply.escrow, supply.treasury) == (0, pb(3))
    assert (await economy.audit()).ok


async def test_refund_returns_everything(economy, pool):
    await _setup(economy)
    async with pool.acquire() as conn, conn.transaction():
        await economy.hold(conn, BUYER, pb(30))
        await economy.refund(conn, BUYER, pb(30))
    assert await economy.balance(BUYER) == pb(100)
    assert (await economy.supply()).escrow == 0


async def test_rollback_undoes_the_hold(economy, pool):
    await _setup(economy)
    with pytest.raises(RuntimeError):
        async with pool.acquire() as conn, conn.transaction():
            await economy.hold(conn, BUYER, pb(30))
            raise RuntimeError("extension failed after holding")
    assert await economy.balance(BUYER) == pb(100)


async def test_cannot_release_more_than_held(economy, pool):
    await _setup(economy)
    with pytest.raises(InsufficientFunds):
        async with pool.acquire() as conn, conn.transaction():
            await economy.hold(conn, BUYER, pb(10))
            await economy.release(conn, SELLER, pb(11))


async def test_cannot_hold_more_than_balance(economy, pool):
    await _setup(economy)
    with pytest.raises(InsufficientFunds):
        async with pool.acquire() as conn, conn.transaction():
            await economy.hold(conn, BUYER, pb(101))


async def test_frozen_user_cannot_hold(economy, pool):
    await _setup(economy)
    await economy.set_frozen(ADMIN_ID, BUYER, True, reason="test")
    with pytest.raises(AccountFrozen):
        async with pool.acquire() as conn, conn.transaction():
            await economy.hold(conn, BUYER, pb(1))


@pytest.mark.parametrize("fee", [-1, pb(11)])
async def test_bad_fee_rejected(economy, pool, fee):
    await _setup(economy)
    with pytest.raises(AmountError):
        async with pool.acquire() as conn, conn.transaction():
            await economy.release(conn, SELLER, pb(10), fee=fee)


async def test_hold_is_idempotent(economy, pool):
    await _setup(economy)
    for _ in range(2):
        async with pool.acquire() as conn, conn.transaction():
            await economy.hold(conn, BUYER, pb(10), idempotency_key="order-1")
    assert await economy.balance(BUYER) == pb(90)


async def test_needs_a_transaction(economy, pool):
    await _setup(economy)
    async with pool.acquire() as conn:
        with pytest.raises(RuntimeError, match="inside a transaction"):
            await economy.hold(conn, BUYER, pb(1))
