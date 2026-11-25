"""GET /metrics in Prometheus text format. Served with the rest of bot.http_app,
so only when PB_API_PORT is set, and on localhost by default."""

from __future__ import annotations

from aiohttp import web

from polybit.money import UNIT

BOT = web.AppKey("polybit_bot")


def _pb(units: int) -> str:
    return f"{units / UNIT:.6f}".rstrip("0").rstrip(".")


async def render(bot) -> str:
    supply = await bot.economy.supply()
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM journal) AS entries,
              (SELECT COUNT(*) FROM journal WHERE created_at > now() - interval '24 hours') AS day,
              (SELECT COALESCE(MAX(height), 0) FROM blocks) AS height,
              (SELECT COUNT(*) FROM journal j WHERE j.hash IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM block_entries b WHERE b.journal_id = j.id)) AS unsealed
            """
        )
    lines = [
        "# HELP polybit_supply_pb PB by where it sits.",
        "# TYPE polybit_supply_pb gauge",
    ]
    for bucket in ("issued", "burned", "circulating", "treasury", "escrow"):
        lines.append(f'polybit_supply_pb{{bucket="{bucket}"}} {_pb(getattr(supply, bucket))}')
    for name, kind, help_, value in [
        ("polybit_wallets", "gauge", "Wallets holding PB.", supply.wallets),
        ("polybit_journal_entries_total", "counter", "Ledger entries ever written.", row["entries"]),
        ("polybit_transactions_24h", "gauge", "Ledger entries in the last 24 hours.", row["day"]),
        ("polybit_block_height", "gauge", "Latest sealed block.", row["height"]),
        ("polybit_unsealed_entries", "gauge", "Entries waiting for the next block.", row["unsealed"]),
        ("polybit_guilds", "gauge", "Discord servers the bot is in.", len(bot.guilds)),
    ]:
        lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}", f"{name} {value}"]
    return "\n".join(lines) + "\n"


async def handle(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    if bot.economy is None:
        return web.Response(status=503, text="starting\n")
    body = await render(bot)
    return web.Response(text=body, content_type="text/plain", charset="utf-8",
                        headers={"Cache-Control": "no-store"})
