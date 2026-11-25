# PolyBit Framework

[![Discord bot](https://img.shields.io/badge/Discord-bot-5865F2?logo=discord&logoColor=white)](https://discord.com/developers/docs/intro)

Discord bot frame with a double-entry ledger on Postgres. Wallets, transfers, daily
rewards, admin tools. Everything else plugs in as an extension.

## Run

```sh
cp .env.example .env    # bot token, admin IDs
docker compose up -d
```

## Extensions

```sh
PB_EXTENSIONS=my_ext,other_ext
```

An extension is a Python module. All parts are optional:

```python
MIGRATIONS = "my_ext.migrations"   # package of .sql files, run once each

def policy(settings):              # one Policy subclass, see polybit/policy.py
    return MyPolicy()

async def setup(bot):              # cogs, routes on bot.http_app, tasks
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

[SECURITY.md](SECURITY.md) · MIT
