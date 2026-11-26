"""Domain errors. Every PolyBitError carries a message that is safe to show to users."""

from __future__ import annotations

from datetime import datetime


class PolyBitError(Exception):
    """Base class for expected, user-facing failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AmountError(PolyBitError):
    """An amount could not be parsed or is out of range."""


class InsufficientFunds(PolyBitError):
    def __init__(self, available: int, required: int) -> None:
        from polybit.money import format_amount

        super().__init__(
            f"Insufficient funds: you have {format_amount(available)}, "
            f"but this needs {format_amount(required)}."
        )
        self.available = available
        self.required = required


class AccountFrozen(PolyBitError):
    def __init__(self) -> None:
        super().__init__("This wallet is frozen. Contact a PolyBit administrator.")


class InvalidTransfer(PolyBitError):
    """A transfer request is malformed (e.g. paying yourself)."""


class NotFound(PolyBitError):
    """A requested record does not exist or is not visible to the caller."""


class CooldownActive(PolyBitError):
    def __init__(self, available_at: datetime) -> None:
        ts = int(available_at.timestamp())
        super().__init__(f"You already claimed your reward. Next claim <t:{ts}:R>.")
        self.available_at = available_at


class PermissionDenied(PolyBitError):
    def __init__(self) -> None:
        super().__init__("You are not a PolyBit administrator.")


class PolicyDenied(PolyBitError):
    """A policy (see polybit/policy.py) refused the action. The message says why."""


class RewardsLocked(PolyBitError):
    def __init__(self, eligible_at: datetime) -> None:
        ts = int(eligible_at.timestamp())
        super().__init__(f"Rewards aren't available for you yet. Try again <t:{ts}:R>.")
        self.eligible_at = eligible_at


class SelfCustody(PolyBitError):
    """The wallet has a key: only a transaction signed with it can move its PB."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "This wallet is self-custody. Sign the transfer with `polybit wallet send` and post it with /submit."
        )
