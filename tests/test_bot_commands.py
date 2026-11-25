"""Load the cogs into an offline bot and check the slash command tree.

Discord rejects bad command definitions at sync time; this catches them first.
"""

from polybit.bot.client import PolyBitBot
from polybit.bot.cogs.admin import AdminCog
from polybit.bot.cogs.chain import ChainCog
from polybit.bot.cogs.wallet import WalletCog
from polybit.config import Settings


def _walk(commands, prefix=""):
    for cmd in commands:
        name = f"{prefix}{cmd.name}"
        children = getattr(cmd, "commands", None)
        if children:
            yield from _walk(children, prefix=f"{name} ")
        else:
            yield name


async def _command_names():
    bot = PolyBitBot(Settings())
    await bot.add_cog(WalletCog(bot))
    await bot.add_cog(AdminCog(bot))
    await bot.add_cog(ChainCog(bot))
    return set(_walk(bot.tree.get_commands()))


async def test_command_tree():
    names = await _command_names()
    for expected in [
        "getbalance",
        "send",
        "sendmany",
        "listtransactions",
        "gettransaction",
        "mine",
        "getinfo",
        "leaderboard",
        "block",
        "verify",
        "pbadmin mint",
        "pbadmin burn",
        "pbadmin freeze",
        "pbadmin audit",
        "pbadmin treasury pay",
    ]:
        assert expected in names, expected
