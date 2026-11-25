<p align="center">
  <img src=".github/banner.svg" alt="polybit" width="720">
</p>

<p align="center">
  <a href="https://discord.com/developers/docs/intro"><img src="https://img.shields.io/badge/Discord-bot-5865F2?style=flat-square&logo=discord&logoColor=white&labelColor=0d1117" alt="Discord bot"></a>
  <img src="https://img.shields.io/badge/ledger-double--entry-76B900?style=flat-square&labelColor=0d1117" alt="double-entry ledger">
  <img src="https://img.shields.io/badge/Postgres-16-76B900?style=flat-square&logo=postgresql&logoColor=white&labelColor=0d1117" alt="Postgres 16">
  <img src="https://img.shields.io/badge/Python-3.11+-76B900?style=flat-square&logo=python&logoColor=white&labelColor=0d1117" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-5865F2?style=flat-square&labelColor=0d1117" alt="MIT">
</p>

Discord bot with its own coin, PB. Every transfer is a balanced journal entry,
append-only, checked by the database. Plug the rest in as extensions.

```
$ polybit audit
Journal entries: 18,204   Accounts: 1,311
Issued: 412,900   Burned: 1,250   In wallets: 398,410   Treasury: 13,240   Escrow: 0
OK: all ledger invariants hold.
```

## Run

```sh
cp .env.example .env    # bot token, admin IDs
docker compose up -d
```

## Extend

```sh
PB_EXTENSIONS=my_ext
```

```python
MIGRATIONS = "my_ext.migrations"   # .sql files, run once

def policy(settings):              # your rules, see polybit/policy.py
    return MyPolicy()

async def setup(bot):              # cogs, routes on bot.http_app
    await bot.add_cog(MyCog(bot))
```

## Commands

`/getbalance` `/send` `/sendmany` `/mine` `/leaderboard` `/getinfo` `/pbadmin`

## Dev

```sh
pip install -e ".[dev]"
sh scripts/security/install-hooks.sh
pytest
```

[SECURITY.md](SECURITY.md)
