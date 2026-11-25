"""Test fixtures.

Database tests run against a real PostgreSQL. Set PB_TEST_DATABASE_URL to use an
existing server (the database will be wiped!). Otherwise an embedded server is
started with the `pgserver` package. If neither is available, DB tests are skipped.
"""

from __future__ import annotations

import os
import tempfile

import asyncpg
import pytest

from polybit.config import Settings
from polybit.db import create_pool, migrate
from polybit.economy import Economy
from polybit.money import pb


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("PB_TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("No PB_TEST_DATABASE_URL and pgserver is not installed")
    data_dir = tempfile.mkdtemp(prefix="polybit-test-pg-")
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
async def pool(database_url):
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    pool = await create_pool(database_url, max_size=20)
    await migrate(pool)
    try:
        yield pool
    finally:
        await pool.close()


ADMIN_ID = 1


@pytest.fixture
def settings() -> Settings:
    return Settings(
        admin_ids=frozenset({ADMIN_ID}),
        signup_grant=pb(100),
        daily_amount=pb(25),
        daily_streak_bonus=pb(5),
        daily_streak_max=3,
        transfer_fee_bps=0,
    )


@pytest.fixture
def economy(pool, settings) -> Economy:
    return Economy(pool, settings)
