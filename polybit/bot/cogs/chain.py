"""Block explorer commands and the block sealer.

    /block [height]    a block's header (latest if no height)
    /verify txid       prove a transaction is in the chain, untouched
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from polybit import chain
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


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


class ChainCog(commands.Cog):
    def __init__(self, bot: PolyBitBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        minutes = self.bot.settings.block_minutes
        if minutes > 0:
            self.sealer.change_interval(minutes=minutes)
            self.sealer.start()

    async def cog_unload(self) -> None:
        self.sealer.cancel()

    async def on_block(self, block: chain.Block) -> None:
        """Called after each new block. Hook for announcements."""

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

    # ------------------------------------------------------------------ block

    @app_commands.command(name="block", description="Show a block of the PB chain (latest by default).")
    @app_commands.describe(height="Block number. Leave empty for the latest block.")
    async def block(
        self, interaction: discord.Interaction, height: app_commands.Range[int, 1] | None = None
    ) -> None:
        b = await chain.get_block(self.bot.pool, height)
        if b is None:
            raise NotFound("No blocks yet." if height is None else f"Block #{height} doesn't exist yet.")
        await interaction.response.send_message(embed=block_embed(b), ephemeral=True)

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
        embed = (ui.ok_embed if v.ok else ui.info_embed)(f"Transaction #{txid}", "\n".join(lines))
        embed.add_field(name="Tx hash", value=_hex(v.entry_hash), inline=False)
        if v.block is not None:
            embed.add_field(name="Block hash", value=_hex(v.block.hash), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
