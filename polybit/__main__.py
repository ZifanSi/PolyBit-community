"""Command-line entry point.

    polybit run       start the Discord bot (default)
    polybit migrate   apply database migrations and exit
    polybit audit     verify ledger integrity and exit (non-zero status on problems)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

from polybit.config import ConfigError, Settings
from polybit.money import format_amount


async def _migrate(settings: Settings) -> int:
    from polybit import extensions
    from polybit.db import create_pool

    modules = extensions.load(settings.extensions)
    pool = await create_pool(settings.database_url, max_size=2)
    try:
        applied = await extensions.migrate_all(pool, modules)
        print("Applied: " + ", ".join(applied) if applied else "Database is up to date.")
    finally:
        await pool.close()
    return 0


async def _audit(settings: Settings) -> int:
    from polybit.db import create_pool
    from polybit.economy import Economy

    pool = await create_pool(settings.database_url, max_size=2)
    try:
        economy = Economy(pool, settings)
        report = await economy.audit()
        supply = await economy.supply()
    finally:
        await pool.close()
    print(f"Journal entries: {report.journal_entries:,}   Accounts: {report.accounts:,}")
    print(f"Issued: {format_amount(supply.issued)}   Burned: {format_amount(supply.burned)}   "
          f"In wallets: {format_amount(supply.circulating)}   Treasury: {format_amount(supply.treasury)}   "
          f"Escrow: {format_amount(supply.escrow)}")
    if report.ok:
        print("OK: all ledger invariants hold.")
        return 0
    for problem in report.problems:
        print(f"PROBLEM: {problem}")
    return 1


def _run(settings: Settings) -> int:
    from polybit.bot.client import PolyBitBot

    bot = PolyBitBot(settings)
    bot.run(settings.discord_token, log_handler=None)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polybit", description="PolyBit: Discord-native PB currency")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "migrate", "audit"])
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        settings.validate(require_token=args.command == "run")
    except ConfigError as exc:
        print(f"Bad config: {exc} (see polybit/config.py)", file=sys.stderr)
        return 2

    if args.command == "migrate":
        return asyncio.run(_migrate(settings))
    if args.command == "audit":
        return asyncio.run(_audit(settings))
    return _run(settings)


if __name__ == "__main__":
    sys.exit(main())
