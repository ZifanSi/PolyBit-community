import types

import pytest

from polybit import extensions
from polybit.bot.client import PolyBitBot
from polybit.config import Settings
from polybit.policy import Policy

DUMMY = "tests.ext_dummy"


def test_unknown_extension_fails_loudly():
    with pytest.raises(extensions.ExtensionError, match="no_such_ext"):
        extensions.load(("no_such_ext",))


async def test_extension_migrations_run_once_and_namespaced(pool):
    modules = extensions.load((DUMMY,))
    applied = await extensions.migrate_all(pool, modules)
    assert f"{DUMMY}.migrations:0001_hello" in applied
    assert await extensions.migrate_all(pool, modules) == []
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('ext_hello') IS NOT NULL")


def test_policy_comes_from_extension():
    [mod] = extensions.load((DUMMY,))
    assert type(extensions.build_policy([mod], Settings())).__name__ == "DummyPolicy"
    assert type(extensions.build_policy([], Settings())) is Policy


def test_only_one_policy_provider_allowed():
    a = types.ModuleType("a")
    b = types.ModuleType("b")
    a.policy = b.policy = lambda settings: Policy()
    with pytest.raises(extensions.ExtensionError, match="more than one"):
        extensions.build_policy([a, b], Settings())


def test_policy_must_be_a_policy():
    bad = types.ModuleType("bad")
    bad.policy = lambda settings: object()
    with pytest.raises(extensions.ExtensionError, match="must return"):
        extensions.build_policy([bad], Settings())


async def test_setup_adds_commands_and_routes():
    bot = PolyBitBot(Settings())
    await extensions.setup_all(bot, extensions.load((DUMMY,)))
    assert "hello" in {c.name for c in bot.tree.get_commands()}
    assert any(r.resource.canonical == "/hello" for r in bot.http_app.router.routes())
