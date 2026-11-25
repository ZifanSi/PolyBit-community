"""Policy hooks: where deployments plug in their own rules.

The framework allows everything by default. Subclass Policy in a private extension
to add anti-abuse rules (who can get rewards, how much operators may mint, ...)
without putting those rules or their thresholds in this repository.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg


class Policy:
    async def reward_eligible_at(self, conn: asyncpg.Connection, user_id: int) -> datetime | None:
        """When this user may receive the welcome grant and /mine rewards.
        None means now. Called inside the reward's transaction."""
        return None

    async def check_mint(self, conn: asyncpg.Connection, actor_id: int, amount: int) -> None:
        """Raise PolicyDenied to block a mint. Called inside the mint's transaction,
        before any PB moves, so a policy can lock and count safely."""
