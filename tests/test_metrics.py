from aiohttp.test_utils import TestClient, TestServer

from polybit import chain
from polybit.bot.client import PolyBitBot
from polybit.config import Settings
from polybit.money import pb

ALICE, BOB = 101, 102


async def _client(bot):
    client = TestClient(TestServer(bot.http_app))
    await client.start_server()
    return client


async def test_metrics_before_startup_is_503():
    client = await _client(PolyBitBot(Settings()))
    try:
        assert (await client.get("/metrics")).status == 503
    finally:
        await client.close()


async def test_metrics_report_supply_and_chain(economy, pool):
    await economy.register(ALICE)
    await economy.pay(ALICE, BOB, pb("2.5"))
    await chain.seal_block(pool)
    await economy.pay(ALICE, BOB, pb(1))

    bot = PolyBitBot(Settings())
    bot.economy, bot.pool = economy, pool
    client = await _client(bot)
    try:
        resp = await client.get("/metrics")
        assert resp.status == 200 and resp.content_type == "text/plain"
        body = await resp.text()
    finally:
        await client.close()
    assert 'polybit_supply_pb{bucket="issued"} 100\n' in body
    assert 'polybit_supply_pb{bucket="circulating"} 100\n' in body
    assert "polybit_journal_entries_total 3\n" in body
    assert "polybit_block_height 1\n" in body
    assert "polybit_unsealed_entries 1\n" in body
    assert "# TYPE polybit_wallets gauge" in body
