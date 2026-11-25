from datetime import datetime, timezone

from polybit import chain
from polybit.bot.client import PolyBitBot
from polybit.bot.cogs.chain import ChainCog
from polybit.config import Settings

BLOCK = chain.Block(7, b"\x01" * 32, b"\x02" * 32, b"\x03" * 32, 12, datetime(2026, 1, 1, tzinfo=timezone.utc))


class _Channel:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, **kwargs):
        if self.fail:
            raise RuntimeError("missing permissions")
        self.sent.append(kwargs["embed"])


def _cog(channel_id, channel):
    bot = PolyBitBot(Settings(block_minutes=0, block_channel_id=channel_id))
    bot.get_channel = lambda cid: channel if cid == channel_id else None
    return ChainCog(bot)


async def test_new_block_is_posted_to_channel():
    ch = _Channel()
    await _cog(555, ch).on_block(BLOCK)
    [embed] = ch.sent
    assert "Block #7" in embed.title
    assert BLOCK.hash.hex() in str(embed.fields)


async def test_no_channel_set_posts_nothing():
    ch = _Channel()
    await _cog(None, ch).on_block(BLOCK)
    assert ch.sent == []


async def test_broken_channel_never_raises():
    await _cog(555, _Channel(fail=True)).on_block(BLOCK)  # logged, not raised


def test_channel_env(monkeypatch):
    monkeypatch.setenv("PB_BLOCK_CHANNEL_ID", "123")
    monkeypatch.setenv("PB_BLOCK_MINUTES", "2.5")
    s = Settings.from_env()
    assert (s.block_channel_id, s.block_minutes) == (123, 2.5)
