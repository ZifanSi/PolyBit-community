"""Extensions: separate (usually private) modules that build a product on the framework.

List them in PB_EXTENSIONS. An extension module may define any of:

    MIGRATIONS = "mypkg.migrations"          # package holding NNNN_name.sql files
    def policy(settings) -> Policy           # at most one extension may provide this
    async def setup(bot) -> None             # add cogs, HTTP routes, background tasks

Load order: modules are imported, their migrations run, the policy is built, the
Economy is created with it, then each setup(bot) is awaited.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import asyncpg

from polybit.config import Settings
from polybit.db import migrate
from polybit.policy import Policy


class ExtensionError(RuntimeError):
    pass


def load(names: tuple[str, ...]) -> list[ModuleType]:
    modules = []
    for name in names:
        try:
            modules.append(importlib.import_module(name))
        except ImportError as exc:
            raise ExtensionError(f"can't import extension {name!r}: {exc}") from exc
    return modules


async def migrate_all(pool: asyncpg.Pool, modules: list[ModuleType]) -> list[str]:
    applied = await migrate(pool)
    for module in modules:
        source = getattr(module, "MIGRATIONS", None)
        if source:
            applied += await migrate(pool, source)
    return applied


def build_policy(modules: list[ModuleType], settings: Settings) -> Policy:
    for m in modules:
        # A submodule named "policy" replaces the function once imported. Fail closed, not open.
        if hasattr(m, "policy") and not callable(m.policy):
            raise ExtensionError(f"{m.__name__}.policy must be a function (is a submodule shadowing it?)")
    providers = [m for m in modules if callable(getattr(m, "policy", None))]
    if len(providers) > 1:
        names = ", ".join(m.__name__ for m in providers)
        raise ExtensionError(f"more than one extension provides a policy: {names}")
    if not providers:
        return Policy()
    policy = providers[0].policy(settings)
    if not isinstance(policy, Policy):
        raise ExtensionError(f"{providers[0].__name__}.policy() must return a polybit.policy.Policy")
    return policy


async def setup_all(bot, modules: list[ModuleType]) -> None:
    for module in modules:
        setup = getattr(module, "setup", None)
        if setup is not None:
            await setup(bot)
