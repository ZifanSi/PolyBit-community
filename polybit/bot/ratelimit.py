"""Per-user command rate limit, checked before every slash command."""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable

import discord
from discord import app_commands


class SlowDown(app_commands.CheckFailure):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Slow down. Try again in {math.ceil(retry_after)}s.")
        self.retry_after = retry_after


class RateLimiter:
    """Sliding window: at most `limit` commands per user in any `window` seconds."""

    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[int, deque[float]] = {}

    def hit(self, user_id: int) -> float:
        """Record a command. Returns 0 if allowed, else seconds until the user may try again."""
        now = self.clock()
        hits = self._hits.setdefault(user_id, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return self.window - (now - hits[0])
        hits.append(now)
        if len(self._hits) > 10_000:
            self._forget_idle(now)
        return 0.0

    def _forget_idle(self, now: float) -> None:
        for uid in [u for u, h in self._hits.items() if not h or now - h[-1] >= self.window]:
            del self._hits[uid]


class PolyBitTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        limiter: RateLimiter | None = getattr(self.client, "rate_limiter", None)
        if limiter is None or self.client.settings.is_admin(interaction.user.id):
            return True
        wait = limiter.hit(interaction.user.id)
        if wait:
            raise SlowDown(wait)
        return True
