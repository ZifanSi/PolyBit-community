"""Runtime configuration loaded from environment variables (and an optional .env file).

Extensions read their own settings from the environment; this file only covers the framework.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from polybit.errors import AmountError
from polybit.money import pb

T = TypeVar("T")

# Dotted Python module path, e.g. "mycompany.polybit_ext".
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ConfigError(ValueError):
    """Invalid configuration. The message lists every problem found."""


def _int_set(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part)


def _opt_int(raw: str) -> int | None:
    return int(raw) if raw.strip() else None


def _str_tuple(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Bot and ledger settings. Amounts are in units (see polybit.money)."""

    discord_token: str = ""
    database_url: str = "postgresql://polybit:polybit@localhost:5432/polybit"
    # Discord user IDs allowed to mint, burn and freeze. Wallets are global, so
    # this is deliberately NOT tied to per-server administrator permissions.
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    # If set, slash commands are synced to this guild only (instant updates while developing).
    dev_guild_id: int | None = None

    signup_grant: int = pb(100)
    daily_amount: int = pb(25)
    daily_streak_bonus: int = pb(5)
    daily_streak_max: int = 7
    daily_cooldown_hours: float = 20.0
    # A streak survives if the next claim happens within this many hours of the last one.
    daily_streak_window_hours: float = 48.0
    # Fee charged to the sender on transfers, in basis points (100 = 1%). Goes to the treasury.
    transfer_fee_bps: int = 0
    # Transfers at or above this amount ask the sender to confirm.
    pay_confirm_threshold: int = pb(1000)

    # Python modules to load as extensions (see polybit/extensions.py).
    extensions: tuple[str, ...] = ()
    # HTTP server for extensions that add routes. Localhost only; put HTTPS in front. 0 = off.
    api_host: str = "127.0.0.1"
    api_port: int = 0
    # Seal new ledger entries into a block this often. 0 = never (no blocks).
    block_minutes: float = 10.0
    # Post every new block's hash here: a public record nobody can quietly rewrite. Empty = off.
    block_channel_id: int | None = None

    @classmethod
    def from_env(cls) -> Settings:
        """Read settings from the environment. Raises ConfigError naming any unparsable variable."""
        env = os.environ
        d = cls()
        problems: list[str] = []

        def read(name: str, parse: Callable[[str], T], default: T) -> T:
            raw = env.get(name)
            if raw is None:
                return default
            try:
                return parse(raw)
            except (ValueError, AmountError, ArithmeticError):
                problems.append(f"{name}={raw!r} is not valid")
                return default

        settings = cls(
            discord_token=env.get("PB_DISCORD_TOKEN", d.discord_token),
            database_url=env.get("PB_DATABASE_URL", d.database_url),
            admin_ids=read("PB_ADMIN_IDS", _int_set, d.admin_ids),
            dev_guild_id=read("PB_DEV_GUILD_ID", _opt_int, d.dev_guild_id),
            signup_grant=read("PB_SIGNUP_GRANT", pb, d.signup_grant),
            daily_amount=read("PB_DAILY_AMOUNT", pb, d.daily_amount),
            daily_streak_bonus=read("PB_DAILY_STREAK_BONUS", pb, d.daily_streak_bonus),
            daily_streak_max=read("PB_DAILY_STREAK_MAX", int, d.daily_streak_max),
            daily_cooldown_hours=read("PB_DAILY_COOLDOWN_HOURS", float, d.daily_cooldown_hours),
            daily_streak_window_hours=read(
                "PB_DAILY_STREAK_WINDOW_HOURS", float, d.daily_streak_window_hours
            ),
            transfer_fee_bps=read("PB_TRANSFER_FEE_BPS", int, d.transfer_fee_bps),
            pay_confirm_threshold=read("PB_PAY_CONFIRM_THRESHOLD", pb, d.pay_confirm_threshold),
            extensions=read("PB_EXTENSIONS", _str_tuple, d.extensions),
            api_host=env.get("PB_API_HOST", d.api_host),
            api_port=read("PB_API_PORT", int, d.api_port),
            block_minutes=read("PB_BLOCK_MINUTES", float, d.block_minutes),
            block_channel_id=read("PB_BLOCK_CHANNEL_ID", _opt_int, d.block_channel_id),
        )
        if problems:
            raise ConfigError("; ".join(problems))
        return settings

    def validate(self, *, require_token: bool = False) -> None:
        """Raise ConfigError if any setting is out of range."""
        problems: list[str] = []

        def check(ok: bool, message: str) -> None:
            if not ok:
                problems.append(message)

        check(not require_token or bool(self.discord_token), "PB_DISCORD_TOKEN is not set")
        check(all(i > 0 for i in self.admin_ids), "PB_ADMIN_IDS must be positive Discord IDs")
        check(self.signup_grant >= 0, "PB_SIGNUP_GRANT can't be negative")
        check(self.daily_amount >= 0, "PB_DAILY_AMOUNT can't be negative")
        check(self.daily_streak_bonus >= 0, "PB_DAILY_STREAK_BONUS can't be negative")
        check(self.daily_streak_max >= 0, "PB_DAILY_STREAK_MAX can't be negative")
        check(self.daily_cooldown_hours > 0, "PB_DAILY_COOLDOWN_HOURS must be above 0")
        check(
            self.daily_streak_window_hours >= self.daily_cooldown_hours,
            "PB_DAILY_STREAK_WINDOW_HOURS must be at least PB_DAILY_COOLDOWN_HOURS",
        )
        check(0 <= self.transfer_fee_bps <= 10_000, "PB_TRANSFER_FEE_BPS must be 0-10000")
        check(self.pay_confirm_threshold > 0, "PB_PAY_CONFIRM_THRESHOLD must be above 0")
        for name in self.extensions:
            check(bool(MODULE_RE.match(name)), f"PB_EXTENSIONS: {name!r} isn't a module path")
        check(0 <= self.api_port <= 65535, "PB_API_PORT must be 0-65535")
        check(self.block_minutes >= 0, "PB_BLOCK_MINUTES can't be negative")

        if problems:
            raise ConfigError("; ".join(problems))

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids
