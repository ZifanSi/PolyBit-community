"""Block explorer commands, the block sealer and the anchorer.

    /block [height]    a block's header (latest if no height)
    /verify txid       prove a transaction is in the chain, untouched, and pinned on Base
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from polybit import anchor, chain
from polybit.bot import ui
from polybit.errors import NotFound

if TYPE_CHECKING:
    from polybit.bot.client import PolyBitBot

log = logging.getLogger(__name__)


def _hex(b: bytes) -> str:
    return f"`{b.hex()}`"


def block_embed(b: chain.Block, title: str | None = None) -> discord.Embed:
    embed = ui.info_embed(title or f"Block #{b.height}")
    embed.add_field(name="Hash", value=_hex(b.hash), inline=False)
    embed.add_field(name="Previous", value=_hex(b.prev_hash), inline=False)
    embed.add_field(name="Merkle root", value=_hex(b.merkle_root), inline=False)
    embed.add_field(name="Transactions", value=f"{b.tx_count:,}", inline=True)
    embed.add_field(name="Sealed", value=ui.ts(b.created_at, "f"), inline=True)
    return embed


def _anchor_line(settings, a: anchor.Anchor | None) -> str:
    if a is None:
        return "⏳ not anchored on Base yet"
    return f"⚓ anchored on Base in [block #{a.height}]({anchor.tx_url(settings, a)})"


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


class ChainCog(commands.Cog):
    def __init__(self, bot: PolyBitBot) -> None:
        self.bot = bot
        self.anchorer: anchor.Anchorer | None = None
        self._session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        settings = self.bot.settings
        if settings.block_minutes > 0:
            self.sealer.change_interval(minutes=settings.block_minutes)
            self.sealer.start()
        if settings.anchor_rpc_url and self.bot.pool is not None:
            self._session = aiohttp.ClientSession()
            rpc = anchor.Rpc(self._session, settings.anchor_rpc_url)
            self.anchorer = anchor.Anchorer(self.bot.pool, settings, rpc)
            log.info("Anchoring to chain %d from %s", settings.anchor_chain_id, self.anchorer.address)
            self.anchor_loop.start()

    async def cog_unload(self) -> None:
        self.sealer.cancel()
        self.anchor_loop.cancel()
        if self._session is not None:
            await self._session.close()

    async def on_block(self, block: chain.Block) -> None:
        """Post the new block to the announcement channel, if one is set."""
        channel_id = self.bot.settings.block_channel_id
        if not channel_id:
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                log.warning("PB_BLOCK_CHANNEL_ID %s isn't a channel the bot can post in", channel_id)
                return
            await channel.send(embed=block_embed(block, title=f"⛓️ Block #{block.height} sealed"))
        except Exception:
            # A missing channel or permission must never stop the sealer.
            log.exception("Couldn't announce block #%d in channel %s", block.height, channel_id)

    @tasks.loop(minutes=10)
    async def sealer(self) -> None:
        try:
            block = await chain.seal_block(self.bot.pool)
        except Exception:
            log.exception("Sealing a block failed")
            return
        if block is not None:
            log.info("Sealed block #%d (%d tx) %s", block.height, block.tx_count, block.hash.hex())
            await self.on_block(block)

    @sealer.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def anchor_loop(self) -> None:
        if self.anchorer is None:
            return
        try:
            await self.anchorer.tick()
        except anchor.RpcError as exc:
            log.warning("Anchoring skipped: %s", exc)
        except Exception:
            log.exception("Anchoring failed")

    # ------------------------------------------------------------------ block

    @app_commands.command(name="block", description="Show a block of the PB chain (latest by default).")
    @app_commands.describe(height="Block number. Leave empty for the latest block.")
    async def block(
        self, interaction: discord.Interaction, height: app_commands.Range[int, 1] | None = None
    ) -> None:
        b = await chain.get_block(self.bot.pool, height)
        if b is None:
            raise NotFound("No blocks yet." if height is None else f"Block #{height} doesn't exist yet.")
        embed = block_embed(b)
        if self.bot.settings.anchor_rpc_url:
            a = await anchor.covering(self.bot.pool, b.height)
            embed.add_field(name="Base", value=_anchor_line(self.bot.settings, a), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- verify

    @app_commands.command(name="verify", description="Prove a transaction is in the chain and untouched.")
    @app_commands.describe(txid="Transaction number, e.g. 12.")
    async def verify(self, interaction: discord.Interaction, txid: app_commands.Range[int, 1]) -> None:
        v = await chain.verify_entry(self.bot.pool, txid)
        if v is None:
            raise NotFound(f"Transaction #{txid} not found, or it's older than the chain.")
        lines = [f"{_mark(v.entry_ok)} content matches its hash"]
        if v.block is None:
            lines.append("⏳ not in a block yet, it goes into the next one")
        else:
            lines.append(f"{_mark(v.proof_ok)} Merkle proof into block #{v.block.height} (leaf {v.position})")
            lines.append(f"{_mark(v.block_ok)} block hash and link to block #{v.block.height - 1}")
            if self.bot.settings.anchor_rpc_url:
                lines.append(_anchor_line(self.bot.settings, await anchor.covering(self.bot.pool, v.block.height)))
        embed = (ui.ok_embed if v.ok else ui.info_embed)(f"Transaction #{txid}", "\n".join(lines))
        embed.add_field(name="Tx hash", value=_hex(v.entry_hash), inline=False)
        if v.block is not None:
            embed.add_field(name="Block hash", value=_hex(v.block.hash), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
