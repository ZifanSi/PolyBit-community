"""Self-custody wallets: Ed25519 keys, signed transactions, and their wire format.

A wallet that binds a key stops being custodial. From then on only a transaction signed
with that key can take PB out of it: not /send, not an admin, not someone with the
database. Every signed transaction is stored with its signature and checked again by
`polybit audit`, so a forged debit shows up even if someone edits the database directly.

Wire format, pasted into /setkey or /submit:

    pb1.<base64url(payload)>.<base64url(signature)>

The payload is canonical JSON (sorted keys, no spaces, ASCII). The signature covers
DOMAIN + payload, so a PolyBit signature can't be replayed as anything else.

Nothing here touches the database. The private key never leaves the user's machine:
`polybit wallet` signs offline and only the token goes to Discord.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from polybit.errors import PolyBitError

PREFIX = "pb1"
DOMAIN = b"polybit signed message v1\n"
MAX_TOKEN = 4000
MAX_RECIPIENTS = 10
MEMO_MAX = 200


class SignatureError(PolyBitError):
    """A token is malformed, or its signature doesn't check out."""


# --------------------------------------------------------------------- keys


def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


def save_key(key: Ed25519PrivateKey, path: Path) -> None:
    """Write the raw 32-byte seed as hex, readable by the owner only. Never overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.private_bytes_raw().hex() + "\n")


def load_key(path: Path) -> Ed25519PrivateKey:
    raw = bytes.fromhex(path.read_text().strip())
    if len(raw) != 32:
        raise ValueError(f"{path} doesn't hold an Ed25519 key")
    return Ed25519PrivateKey.from_private_bytes(raw)


# ------------------------------------------------------------------ payloads


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def bind_payload(*, chain: str, user_id: int, pubkey: bytes) -> dict[str, Any]:
    return {"v": 1, "type": "bind", "chain": chain, "user": user_id, "pub": pubkey.hex()}


def transfer_payload(
    *,
    chain: str,
    sender_id: int,
    payouts: list[tuple[int, int]],
    nonce: int,
    fee: int = 0,
    memo: str | None = None,
    expires_at: int | None = None,
) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "transfer",
        "chain": chain,
        "from": sender_id,
        "to": [[uid, amount] for uid, amount in payouts],
        "fee": fee,
        "nonce": nonce,
        "memo": memo,
        "exp": expires_at,
    }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key: Ed25519PrivateKey, payload: dict[str, Any]) -> str:
    raw = canonical(payload)
    return f"{PREFIX}.{_b64(raw)}.{_b64(key.sign(DOMAIN + raw))}"


@dataclass(frozen=True)
class Signed:
    payload: dict[str, Any]
    raw: bytes  # exactly the bytes that were signed (minus DOMAIN)
    signature: bytes

    @property
    def text(self) -> str:
        return self.raw.decode("ascii")


def decode(token: str) -> Signed:
    """Parse a token. Checks the shape only; call `verify` with the right key."""
    token = token.strip()
    if len(token) > MAX_TOKEN:
        raise SignatureError("That signed message is too long.")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise SignatureError("That isn't a PolyBit signed message (it should start with `pb1.`).")
    try:
        raw, sig = _unb64(parts[1]), _unb64(parts[2])
        payload = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        raise SignatureError("That signed message is damaged. Copy it again in full.") from None
    if not isinstance(payload, dict) or canonical(payload) != raw:
        raise SignatureError("That signed message isn't in canonical form.")
    if len(sig) != 64:
        raise SignatureError("That signed message has a bad signature.")
    return Signed(payload, raw, sig)


def verify(pubkey: bytes, raw: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pubkey).verify(signature, DOMAIN + raw)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------- validation


def _int(value: Any, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SignatureError("That signed message has a bad number in it.")
    return value


def check_bind(signed: Signed, *, chain: str) -> tuple[int, bytes]:
    """A bind proves the signer holds the key. Returns (user_id, pubkey)."""
    p = signed.payload
    if set(p) != {"v", "type", "chain", "user", "pub"} or p["v"] != 1 or p["type"] != "bind":
        raise SignatureError("That isn't a key-binding message.")
    if p["chain"] != chain:
        raise SignatureError("That message was signed for a different PolyBit network.")
    try:
        pubkey = bytes.fromhex(p["pub"]) if isinstance(p["pub"], str) else b""
    except ValueError:
        pubkey = b""
    if len(pubkey) != 32:
        raise SignatureError("That message has a bad public key.")
    if not verify(pubkey, signed.raw, signed.signature):
        raise SignatureError("Signature doesn't match the key.")
    return _int(p["user"], minimum=1), pubkey


@dataclass(frozen=True)
class Transfer:
    sender_id: int
    payouts: list[tuple[int, int]]
    fee: int
    nonce: int
    memo: str | None
    expires_at: int | None


def parse_transfer(signed: Signed, *, chain: str, now: float | None = None) -> Transfer:
    """Check a transfer's fields (not its signature: that needs the sender's key)."""
    p = signed.payload
    if set(p) != {"v", "type", "chain", "from", "to", "fee", "nonce", "memo", "exp"}:
        raise SignatureError("That isn't a transfer message.")
    if p["v"] != 1 or p["type"] != "transfer":
        raise SignatureError("That isn't a transfer message.")
    if p["chain"] != chain:
        raise SignatureError("That transfer was signed for a different PolyBit network.")
    if not isinstance(p["to"], list) or not 1 <= len(p["to"]) <= MAX_RECIPIENTS:
        raise SignatureError(f"A transfer pays 1 to {MAX_RECIPIENTS} recipients.")
    payouts = []
    for leg in p["to"]:
        if not isinstance(leg, list) or len(leg) != 2:
            raise SignatureError("That transfer has a bad recipient.")
        payouts.append((_int(leg[0], minimum=1), _int(leg[1], minimum=1)))
    memo = p["memo"]
    if memo is not None and (not isinstance(memo, str) or not memo or len(memo) > MEMO_MAX
                             or " ".join(memo.split()) != memo):
        raise SignatureError("That transfer's memo isn't allowed (trim it, max 200 characters).")
    expires_at = None if p["exp"] is None else _int(p["exp"])
    if expires_at is not None and (now if now is not None else time.time()) > expires_at:
        raise SignatureError("That transfer has expired. Sign a new one.")
    return Transfer(
        sender_id=_int(p["from"], minimum=1),
        payouts=payouts,
        fee=_int(p["fee"]),
        nonce=_int(p["nonce"], minimum=1),
        memo=memo,
        expires_at=expires_at,
    )
