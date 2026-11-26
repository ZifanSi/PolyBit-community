"""Offline wallet: make a key and sign with it. Runs on your machine, needs no database
and no network. Only the printed `pb1.` token goes to Discord; the key file never does.

    polybit wallet keygen                       new key in ~/.polybit/wallet.key
    polybit wallet pubkey                       show its public key
    polybit wallet bind --user <discord id>     token for /setkey
    polybit wallet send --from <id> --to <id>=<amount> [--to ...] --nonce <n>
                                                token for /submit (your next nonce is on /getbalance)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from polybit import signing
from polybit.errors import PolyBitError
from polybit.money import format_amount, parse_amount

DEFAULT_KEY = Path.home() / ".polybit" / "wallet.key"


def _payout(text: str) -> tuple[int, int]:
    uid, sep, amount = text.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"{text!r}: use <discord id>=<amount>")
    try:
        return int(uid), parse_amount(amount)
    except (ValueError, PolyBitError):
        raise argparse.ArgumentTypeError(f"{text!r}: use <discord id>=<amount>") from None


def _amount(text: str) -> int:
    if text.strip() in ("0", "0.0"):
        return 0
    try:
        return parse_amount(text)
    except PolyBitError as exc:
        raise argparse.ArgumentTypeError(exc.message) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polybit wallet", description="Offline self-custody wallet.")
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY, help=f"key file (default {DEFAULT_KEY})")
    parser.add_argument("--chain", default="polybit", help="network name, PB_CHAIN_ID of the bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen", help="make a new key")
    sub.add_parser("pubkey", help="show the public key")
    bind = sub.add_parser("bind", help="sign a key binding for /setkey")
    bind.add_argument("--user", type=int, required=True, help="your Discord user id")
    send = sub.add_parser("send", help="sign a transfer for /submit")
    send.add_argument("--from", dest="sender", type=int, required=True, help="your Discord user id")
    send.add_argument("--to", type=_payout, action="append", required=True, metavar="ID=AMOUNT")
    send.add_argument("--nonce", type=int, required=True, help="your next nonce, from /getbalance")
    send.add_argument("--fee", type=_amount, default=0, help="fee you agree to pay (default 0)")
    send.add_argument("--memo")
    send.add_argument("--expires", type=int, default=60, metavar="MINUTES", help="0 = never (default 60)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "keygen":
            if args.key.exists():
                print(f"{args.key} already exists. Not overwriting a key.", file=sys.stderr)
                return 1
            key = signing.new_key()
            signing.save_key(key, args.key)
            print(f"Saved to {args.key}. Back it up: lose it and the wallet is locked for good.")
            print(f"Public key: {signing.public_bytes(key).hex()}")
            return 0

        key = signing.load_key(args.key)
        if args.command == "pubkey":
            print(signing.public_bytes(key).hex())
            return 0
        if args.command == "bind":
            payload = signing.bind_payload(chain=args.chain, user_id=args.user, pubkey=signing.public_bytes(key))
            print(signing.sign(key, payload))
            return 0

        memo = " ".join(args.memo.split()) if args.memo else None
        expires = int(time.time()) + args.expires * 60 if args.expires > 0 else None
        payload = signing.transfer_payload(
            chain=args.chain, sender_id=args.sender, payouts=args.to, nonce=args.nonce,
            fee=args.fee, memo=memo, expires_at=expires,
        )
        total = sum(a for _, a in args.to)
        print(f"Signing: {format_amount(total)} + fee {format_amount(args.fee)}, nonce {args.nonce}", file=sys.stderr)
        print(signing.sign(key, payload))
        return 0
    except FileNotFoundError:
        print(f"No key at {args.key}. Run `polybit wallet keygen` first.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
