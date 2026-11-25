"""Member-facing commands, named after their Bitcoin Core RPC counterparts:

    /getbalance        wallet balance
    /send              transfer PB to another member
    /sendmany          pay several members in one atomic transaction
    /listtransactions  recent wallet activity
    /gettransaction    details of one transaction
    /mine              claim the periodic reward
    /getinfo           money-supply statistics
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from polybit.bot import ui
from polybit.economy import MAX_RECIPIENTS
from polybit.errors import PolyBitError, InvalidTransfer, NotFound
from polybit.money import format_amount, parse_amount

if TYPE_CHECKING:
    from polybit.bot.client import PolyBitBot

log = logging.getLogger(__name__)

PAGE_SIZE = 10
MENTION_RE = re.compile(r"<@!?(\d{15,25})>")


class WalletCog(commands.Cog):
    def __init__(self, bot: PolyBitBot) -> None:
        self.bot = bot

    @property
    def economy(self):
        return self.bot.economy

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Runs before every command in this cog: make sure the caller has a wallet."""
        welcomed = await self.economy.register(interaction.user.id, interaction.guild_id)
        interaction.extras["pb_welcomed"] = welcomed
        return True

    def _add_welcome(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        grant = self.bot.settings.signup_grant
        if interaction.extras.get("pb_welcomed") and grant > 0:
            embed.add_field(
                name="Welcome to PolyBit!",
                value=f"Your PolyBit wallet was just created with a welcome grant of {ui.amount(grant)}.",
                inline=False,
            )

    # ------------------------------------------------------------- getbalance

    @app_commands.command(name="getbalance", description="Show a PB wallet balance.")
    @app_commands.describe(user="Whose wallet (defaults to you).", public="Show the result to everyone.")
    async def getbalance(
        self, interaction: discord.Interaction, user: discord.User | None = None, public: bool = False
    ) -> None:
        target = user or interaction.user
        if target.bot:
            raise InvalidTransfer("Bots don't have PolyBit wallets.")
        units = await self.economy.balance(target.id)
        embed = ui.info_embed(f"{target.display_name}'s wallet", ui.amount(units))
        embed.set_thumbnail(url=target.display_avatar.url)
        if await self.economy.is_frozen(target.id):
            embed.add_field(name="Status", value="Frozen", inline=False)
        self._add_welcome(interaction, embed)
        await interaction.response.send_message(embed=embed, ephemeral=not public)

    # ------------------------------------------------------------------- send

    @app_commands.command(name="send", description="Send PB to another member.")
    @app_commands.describe(
        user="Who receives the PB.",
        amount="How much to send, e.g. 10 or 2.5",
        note="Optional note shown with the transaction.",
    )
    async def send(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: str,
        note: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        if user.bot:
            raise InvalidTransfer("Bots can't hold PB.")
        if user.id == interaction.user.id:
            raise InvalidTransfer("You can't send PB to yourself.")
        units = parse_amount(amount)
        fee = self.economy.transfer_fee(units)

        if units >= self.bot.settings.pay_confirm_threshold:
            view = ConfirmView(
                interaction,
                lambda i: self.execute_send(i, interaction.id, user, units, note),
            )
            desc = f"Send {ui.amount(units)} to {user.mention}?"
            if fee:
                desc += f"\nFee: {ui.amount(fee)} (total {ui.amount(units + fee)})"
            await interaction.response.send_message(
                embed=discord.Embed(title="Confirm transaction", description=desc, color=ui.COLOR_WARN),
                view=view,
                ephemeral=True,
            )
            return

        # No defer: a failed send must reply ephemerally, and a public defer would
        # force the error message to be public. A transfer completes well within 3s.
        await self.execute_send(interaction, interaction.id, user, units, note)

    async def execute_send(
        self,
        interaction: discord.Interaction,
        origin_id: int,
        recipient: discord.User,
        units: int,
        note: str | None,
    ) -> None:
        """Perform the transfer and post a receipt. `origin_id` is the slash command's
        interaction id, used as the idempotency key so a retried click never pays twice."""
        result = await self.economy.pay(
            interaction.user.id,
            recipient.id,
            units,
            memo=note,
            guild_id=interaction.guild_id,
            idempotency_key=f"pay:{origin_id}",
        )
        if result.replayed:
            await _send(
                interaction,
                embed=ui.info_embed("Already sent", f"Transaction #{result.journal_id} was already processed."),
                ephemeral=True,
            )
            return

        embed = ui.ok_embed(
            "Transaction sent",
            f"{interaction.user.mention} → {recipient.mention}\n{ui.amount(result.amount)}",
        )
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        if result.fee:
            embed.add_field(name="Fee", value=format_amount(result.fee), inline=True)
        embed.set_footer(text=f"txid #{result.journal_id}")
        await _send(
            interaction,
            content=recipient.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=[recipient]),
        )
        await interaction.followup.send(
            f"Your new balance: {ui.amount(result.sender_balance)}", ephemeral=True
        )

    # --------------------------------------------------------------- sendmany

    @app_commands.command(name="sendmany", description="Send the same amount of PB to several members at once.")
    @app_commands.describe(
        users="Mention the recipients, e.g. @alice @bob @carol (max 10).",
        amount="Amount EACH recipient gets, e.g. 10",
        note="Optional note shown with the transaction.",
    )
    async def sendmany(
        self,
        interaction: discord.Interaction,
        users: str,
        amount: str,
        note: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        each = parse_amount(amount)
        recipients = await self._resolve_mentions(users)
        if any(u.id == interaction.user.id for u in recipients):
            raise InvalidTransfer("You can't send PB to yourself.")
        total = each * len(recipients)
        fee = self.economy.transfer_fee(total)

        if total >= self.bot.settings.pay_confirm_threshold:
            view = ConfirmView(
                interaction,
                lambda i: self.execute_sendmany(i, interaction.id, recipients, each, note),
            )
            desc = (
                f"Send {ui.amount(each)} each to {', '.join(u.mention for u in recipients)}?\n"
                f"Total: {ui.amount(total)}"
            )
            if fee:
                desc += f" + fee {ui.amount(fee)}"
            await interaction.response.send_message(
                embed=discord.Embed(title="Confirm transaction", description=desc, color=ui.COLOR_WARN),
                view=view,
                ephemeral=True,
            )
            return
        await self.execute_sendmany(interaction, interaction.id, recipients, each, note)

    async def _resolve_mentions(self, text: str) -> list[discord.User]:
        ids = list(dict.fromkeys(int(m) for m in MENTION_RE.findall(text)))  # dedupe, keep order
        if not ids:
            raise InvalidTransfer("Mention at least one recipient, e.g. `@alice @bob`.")
        if len(ids) > MAX_RECIPIENTS:
            raise InvalidTransfer(f"At most {MAX_RECIPIENTS} recipients per transaction.")
        users = []
        for uid in ids:
            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except discord.NotFound:
                    raise InvalidTransfer(f"Unknown user <@{uid}>.") from None
            if user.bot:
                raise InvalidTransfer(f"{user.mention} is a bot; bots can't hold PB.")
            users.append(user)
        return users

    async def execute_sendmany(
        self,
        interaction: discord.Interaction,
        origin_id: int,
        recipients: list[discord.User],
        each: int,
        note: str | None,
    ) -> None:
        result = await self.economy.pay_many(
            interaction.user.id,
            [(u.id, each) for u in recipients],
            memo=note,
            guild_id=interaction.guild_id,
            idempotency_key=f"pay:{origin_id}",
        )
        if result.replayed:
            await _send(
                interaction,
                embed=ui.info_embed("Already sent", f"Transaction #{result.journal_id} was already processed."),
                ephemeral=True,
            )
            return

        mentions = " ".join(u.mention for u in recipients)
        embed = ui.ok_embed(
            "Transaction sent",
            f"{interaction.user.mention} → {mentions}\n"
            f"{ui.amount(each)} each · {ui.amount(result.amount)} total",
        )
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        if result.fee:
            embed.add_field(name="Fee", value=format_amount(result.fee), inline=True)
        embed.set_footer(text=f"txid #{result.journal_id}")
        await _send(
            interaction,
            content=mentions,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=recipients),
        )
        await interaction.followup.send(
            f"Your new balance: {ui.amount(result.sender_balance)}", ephemeral=True
        )

    # ------------------------------------------------------- listtransactions

    @app_commands.command(name="listtransactions", description="List your recent PB transactions.")
    @app_commands.describe(page="Page number (10 per page).")
    async def listtransactions(
        self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1
    ) -> None:
        items = await self.economy.history(
            interaction.user.id, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        embed = ui.info_embed(f"Transactions — page {page}")
        if not items:
            embed.description = "No transactions on this page."
        else:
            lines = []
            for item in items:
                sign = "+" if item.amount > 0 else "−"
                label = ui.KIND_LABELS.get(item.kind, item.kind)
                direction = "from" if item.amount > 0 else "to"
                if item.counterparty_count > 1:
                    label += f" {direction} {item.counterparty_count} users"
                elif item.counterparty_id:
                    label += f" {direction} <@{item.counterparty_id}>"
                lines.append(
                    f"`#{item.journal_id}` `{sign}{format_amount(abs(item.amount))}` · {label} · "
                    f"{ui.ts(item.created_at)}"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(text="Use /gettransaction txid:<number> for details.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --------------------------------------------------------- gettransaction

    @app_commands.command(name="gettransaction", description="Show details of one transaction.")
    @app_commands.describe(txid="Transaction number, e.g. 12 (shown as #12 on receipts).")
    async def gettransaction(
        self, interaction: discord.Interaction, txid: app_commands.Range[int, 1]
    ) -> None:
        tx = await self.economy.get_transaction(txid, viewer_id=interaction.user.id)
        if tx is None:
            raise NotFound(f"Transaction #{txid} not found (or it isn't yours).")
        embed = ui.info_embed(f"Transaction #{tx.id}", ui.KIND_LABELS.get(tx.kind, tx.kind))
        embed.add_field(name="Time", value=ui.ts(tx.created_at, "f"), inline=False)
        debits = [l for l in tx.legs if l.amount < 0]
        credits = [l for l in tx.legs if l.amount > 0]
        embed.add_field(name="From", value="\n".join(_leg_line(l) for l in debits), inline=True)
        embed.add_field(name="To", value="\n".join(_leg_line(l) for l in credits), inline=True)
        if tx.memo:
            embed.add_field(name="Note", value=tx.memo, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------- mine

    @app_commands.command(name="mine", description="Claim your mining reward (once per cooldown).")
    async def mine(self, interaction: discord.Interaction) -> None:
        result = await self.economy.claim_daily(interaction.user.id, guild_id=interaction.guild_id)
        embed = ui.ok_embed("Mining reward claimed", f"+{ui.amount(result.amount)}")
        embed.add_field(name="Streak", value=f"{result.streak}", inline=True)
        embed.add_field(name="Balance", value=format_amount(result.balance), inline=True)
        embed.add_field(name="Next claim", value=ui.ts(result.next_claim_at), inline=True)
        self._add_welcome(interaction, embed)
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------- getinfo

    @app_commands.command(name="getinfo", description="Show PB money-supply statistics.")
    async def getinfo(self, interaction: discord.Interaction) -> None:
        s = await self.economy.supply()
        embed = ui.info_embed("PolyBit network info")
        embed.add_field(name="Issued (all time)", value=format_amount(s.issued), inline=True)
        embed.add_field(name="Burned", value=format_amount(s.burned), inline=True)
        embed.add_field(name="In wallets", value=format_amount(s.circulating), inline=True)
        embed.add_field(name="Treasury", value=format_amount(s.treasury), inline=True)
        embed.add_field(name="In escrow", value=format_amount(s.escrow), inline=True)
        embed.add_field(name="Wallets", value=f"{s.wallets:,}", inline=True)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------ leaderboard

    @app_commands.command(name="leaderboard", description="Top PB wallets in this server.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        rows = await self.economy.leaderboard(interaction.guild_id, limit=10)
        embed = ui.info_embed("Leaderboard")
        if rows:
            embed.description = "\n".join(
                f"`{rank:>2}.` <@{user_id}> {ui.amount(balance)}" for rank, (user_id, balance) in enumerate(rows, 1)
            )
        else:
            embed.description = "No wallets in this server yet."
        await interaction.response.send_message(embed=embed)


def _leg_line(leg) -> str:
    who = f"<@{leg.owner_id}>" if leg.owner_id else ui.SYSTEM_LABELS.get(leg.system_name, leg.system_name)
    return f"{who} `{format_amount(abs(leg.amount))}`"


async def _send(interaction: discord.Interaction, **kwargs) -> None:
    """Reply with the initial response if it is still unused, otherwise as a follow-up."""
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


class ConfirmView(discord.ui.View):
    """Confirmation step for large transfers. Only shown to the sender (ephemeral).

    `action` performs the transfer, replying through the button interaction it is given.
    """

    def __init__(
        self,
        origin: discord.Interaction,
        action: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=60)
        self.origin = origin
        self.action = action

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.origin.user.id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=ui.info_embed("Processing…"), view=None)
        try:
            await self.action(interaction)
        except PolyBitError as exc:
            await interaction.edit_original_response(embed=ui.error_embed(exc.message))
            return
        await interaction.edit_original_response(embed=ui.ok_embed("Transaction confirmed"))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=ui.info_embed("Transaction cancelled"), view=None)

    async def on_timeout(self) -> None:
        try:
            await self.origin.edit_original_response(
                embed=ui.info_embed("Transaction expired", "No PB was sent."), view=None
            )
        except discord.HTTPException:
            pass

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("Error in send confirmation", exc_info=error)
        try:
            await interaction.followup.send(
                embed=ui.error_embed("Something went wrong. No PB was sent unless a receipt was posted."),
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
