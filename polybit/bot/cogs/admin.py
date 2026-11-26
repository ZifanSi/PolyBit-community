"""Operator commands (/pbadmin mint|burn|freeze|audit|treasury pay).

Access is controlled by PB_ADMIN_IDS, not by server roles, because PB wallets
are global across all servers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from polybit.bot import ui
from polybit.economy import Economy
from polybit.errors import PermissionDenied
from polybit.money import format_amount, parse_amount

if TYPE_CHECKING:
    from polybit.bot.client import PolyBitBot


@app_commands.default_permissions(administrator=True)
class AdminCog(commands.GroupCog, group_name="pbadmin", group_description="PolyBit operator tools."):
    def __init__(self, bot: PolyBitBot) -> None:
        self.bot = bot
        super().__init__()

    @property
    def economy(self) -> Economy:
        if self.bot.economy is None:
            raise RuntimeError("economy used before the bot finished starting")
        return self.bot.economy

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.bot.settings.is_admin(interaction.user.id):
            raise PermissionDenied()
        return True

    @app_commands.command(name="mint", description="Create new PB in a wallet.")
    @app_commands.describe(user="Recipient", amount="Amount to mint", reason="Why (recorded in the ledger)")
    async def mint(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: str,
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        units = parse_amount(amount)
        balance = await self.economy.grant(
            interaction.user.id, user.id, units, reason=reason, guild_id=interaction.guild_id
        )
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "Minted",
                f"Minted {ui.amount(units)} to {user.mention}.\nNew balance: {format_amount(balance)}",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="burn", description="Destroy PB from a wallet.")
    @app_commands.describe(user="Wallet owner", amount="Amount to burn", reason="Why (recorded in the ledger)")
    async def burn(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: str,
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        units = parse_amount(amount)
        balance = await self.economy.revoke(
            interaction.user.id, user.id, units, reason=reason, guild_id=interaction.guild_id
        )
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "Burned",
                f"Burned {ui.amount(units)} from {user.mention}.\nNew balance: {format_amount(balance)}",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="freeze", description="Freeze or unfreeze a wallet.")
    @app_commands.describe(
        user="Wallet owner",
        frozen="True = block sending and mining; False = restore",
        reason="Why (recorded in the audit log)",
    )
    async def freeze(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        frozen: bool,
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        await self.economy.set_frozen(interaction.user.id, user.id, frozen, reason=reason)
        if frozen:
            embed = ui.ok_embed("Wallet frozen", f"{user.mention} can no longer send or mine PB.")
        else:
            embed = ui.ok_embed("Wallet unfrozen", f"{user.mention} can use PB again.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="audit", description="Verify ledger integrity.")
    async def audit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.economy.audit()
        if report.ok:
            embed = ui.ok_embed(
                "Ledger OK",
                f"{report.journal_entries:,} transactions across {report.accounts:,} accounts. "
                "All invariants hold.",
            )
        else:
            embed = discord.Embed(
                title="Ledger problems found",
                description="\n".join(f"• {p}" for p in report.problems)[:4000],
                color=ui.COLOR_ERR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    treasury = app_commands.Group(name="treasury", description="Spend PB collected as fees.")

    @treasury.command(name="pay", description="Pay PB from the treasury to a wallet.")
    @app_commands.describe(user="Recipient", amount="Amount to pay", reason="Why (recorded in the ledger)")
    async def treasury_pay(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: str,
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        units = parse_amount(amount)
        left = await self.economy.treasury_pay(
            interaction.user.id,
            user.id,
            units,
            reason=reason,
            guild_id=interaction.guild_id,
            idempotency_key=f"treasury:{interaction.id}",
        )
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "Paid from treasury",
                f"{ui.amount(units)} to {user.mention}.\nTreasury left: {format_amount(left)}",
            ),
            ephemeral=True,
        )
