"""The PolyBit Discord client: wires the database, economy, extensions and command cogs together."""

from __future__ import annotations

import logging

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from polybit import extensions
from polybit.bot import ui
from polybit.config import Settings
from polybit.db import create_pool
from polybit.economy import Economy
from polybit.errors import PolyBitError

log = logging.getLogger(__name__)

HTTP_MAX_BODY = 64 * 1024


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


class PolyBitBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        # Default intents only: PolyBit uses slash commands and needs no privileged intents.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.pool = None
        self.economy: Economy | None = None
        # Extensions add routes here in setup(bot). Served only if PB_API_PORT is set.
        self.http_app = web.Application(client_max_size=HTTP_MAX_BODY)
        self.http_app.router.add_get("/healthz", _health)
        self._http_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        modules = extensions.load(self.settings.extensions)
        self.pool = await create_pool(self.settings.database_url)
        applied = await extensions.migrate_all(self.pool, modules)
        if applied:
            log.info("Database migrated: %s", ", ".join(applied))
        self.economy = Economy(self.pool, self.settings, extensions.build_policy(modules, self.settings))

        from polybit.bot.cogs.admin import AdminCog
        from polybit.bot.cogs.wallet import WalletCog

        await self.add_cog(WalletCog(self))
        await self.add_cog(AdminCog(self))
        await extensions.setup_all(self, modules)
        await self._start_http()
        self.tree.on_error = self.on_app_command_error

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands", len(synced))

    async def _start_http(self) -> None:
        if not self.settings.api_port:
            return
        self._http_runner = web.AppRunner(self.http_app, access_log=None)
        await self._http_runner.setup()
        await web.TCPSite(self._http_runner, self.settings.api_host, self.settings.api_port).start()
        log.info("HTTP API on %s:%d", self.settings.api_host, self.settings.api_port)

    async def on_ready(self) -> None:
        log.info("PolyBit online as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))
        await self.change_presence(activity=discord.Game(name="/getbalance · /mine · /send"))

    async def close(self) -> None:
        await super().close()
        if self._http_runner is not None:
            await self._http_runner.cleanup()
        if self.pool is not None:
            await self.pool.close()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, PolyBitError):
            embed = ui.error_embed(original.message)
        elif isinstance(error, app_commands.CheckFailure):
            embed = ui.error_embed("You can't use this command here.")
        else:
            log.exception("Unhandled error in /%s", interaction.command and interaction.command.name,
                          exc_info=original)
            embed = ui.error_embed("Something went wrong. The incident has been logged.")
        await send_ephemeral(interaction, embed=embed)


async def send_ephemeral(interaction: discord.Interaction, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=True, **kwargs)
