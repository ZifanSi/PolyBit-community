"""PostgreSQL connection pool and a minimal forward-only migration runner."""

from __future__ import annotations

import logging
from importlib import resources

import asyncpg

log = logging.getLogger(__name__)

# Arbitrary constant used to serialize concurrent migration runs.
_MIGRATION_LOCK_ID = 0x4254_5800


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    if pool is None:
        raise RuntimeError("could not create a database pool")
    return pool


def _migration_files(source: str | None) -> list[tuple[str, str]]:
    folder = resources.files("polybit") / "migrations" if source is None else resources.files(source)
    files = sorted(
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in folder.iterdir()
        if entry.name.endswith(".sql")
    )
    return files


async def migrate(pool: asyncpg.Pool, source: str | None = None) -> list[str]:
    """Apply pending migrations in filename order. Returns the versions applied.

    source=None applies the framework's own migrations. An extension passes the
    package holding its .sql files; its versions are recorded as "<package>:<name>"
    so they never collide with the framework's or another extension's.
    """
    applied_now: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     TEXT PRIMARY KEY,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for name, sql in _migration_files(source):
                version = name.removesuffix(".sql")
                if source is not None:
                    version = f"{source}:{version}"
                if version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
                log.info("Applied migration %s", version)
                applied_now.append(version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)
    return applied_now
