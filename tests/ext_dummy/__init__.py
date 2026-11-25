"""Test-only extension: one table, a policy and a slash command."""

from discord import app_commands
from discord.ext import commands

from polybit.policy import Policy

MIGRATIONS = "tests.ext_dummy.migrations"


class DummyPolicy(Policy):
    pass


def policy(settings) -> Policy:
    return DummyPolicy()


class HelloCog(commands.Cog):
    @app_commands.command(name="hello", description="Say hi.")
    async def hello(self, interaction) -> None:
        await interaction.response.send_message("hi", ephemeral=True)


async def _hello(request):
    return None


async def setup(bot) -> None:
    await bot.add_cog(HelloCog())
    bot.http_app.router.add_get("/hello", _hello)
