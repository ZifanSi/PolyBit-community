from types import SimpleNamespace

import pytest

from polybit.bot.client import PolyBitBot
from polybit.bot.ratelimit import RateLimiter, SlowDown
from polybit.config import Settings
from tests.conftest import ADMIN_ID


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_sliding_window():
    clock = _Clock()
    rl = RateLimiter(3, window=60, clock=clock)
    assert [rl.hit(1) for _ in range(3)] == [0, 0, 0]
    assert rl.hit(1) == 60  # 4th in the same instant: wait a full window
    assert rl.hit(2) == 0  # other users unaffected
    clock.t += 30
    assert rl.hit(1) == 30
    clock.t += 30
    assert rl.hit(1) == 0  # oldest hit fell out of the window


def test_idle_users_are_forgotten():
    clock = _Clock()
    rl = RateLimiter(1, window=10, clock=clock)
    for uid in range(10_001):
        rl.hit(uid)
    clock.t += 11
    rl.hit(99_999)
    assert len(rl._hits) == 1


def _interaction(user_id):
    return SimpleNamespace(user=SimpleNamespace(id=user_id))


async def test_tree_check_blocks_spam_but_not_admins():
    bot = PolyBitBot(Settings(rate_limit_per_minute=2, admin_ids=frozenset({ADMIN_ID})))
    assert await bot.tree.interaction_check(_interaction(5))
    assert await bot.tree.interaction_check(_interaction(5))
    with pytest.raises(SlowDown, match="Slow down"):
        await bot.tree.interaction_check(_interaction(5))
    for _ in range(10):
        assert await bot.tree.interaction_check(_interaction(ADMIN_ID))


async def test_zero_turns_it_off():
    bot = PolyBitBot(Settings(rate_limit_per_minute=0))
    assert bot.rate_limiter is None
    for _ in range(100):
        assert await bot.tree.interaction_check(_interaction(5))


async def test_slow_down_reply_is_shown():
    bot = PolyBitBot(Settings())
    sent = {}

    async def send_message(**kwargs):
        sent.update(kwargs)

    interaction = SimpleNamespace(
        response=SimpleNamespace(is_done=lambda: False, send_message=send_message), command=None
    )
    await bot.on_app_command_error(interaction, SlowDown(4.2))
    assert "Try again in 5s" in sent["embed"].description and sent["ephemeral"]
