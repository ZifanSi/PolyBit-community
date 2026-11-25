"""Shared embed builders and colors."""

from __future__ import annotations

import discord

from polybit.money import format_amount

COLOR_OK = discord.Color.from_rgb(46, 160, 67)
COLOR_INFO = discord.Color.from_rgb(88, 101, 242)
COLOR_WARN = discord.Color.from_rgb(219, 154, 4)
COLOR_ERR = discord.Color.from_rgb(218, 54, 51)

KIND_LABELS = {
    "transfer": "Transfer",
    "signup": "Welcome grant",
    "daily": "Mining reward",
    "grant": "Minted by admin",
    "revoke": "Burned by admin",
    "treasury": "Treasury payout",
}

SYSTEM_LABELS = {
    "mint": "Mint (new coins)",
    "burn": "Burn",
    "treasury": "Treasury (fee)",
    "escrow": "Escrow",
}


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f"**Error:** {message}", color=COLOR_ERR)


def ok_embed(title: str, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_OK)


def info_embed(title: str, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_INFO)


def amount(units: int) -> str:
    return f"**{format_amount(units)}**"


def ts(dt, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"
