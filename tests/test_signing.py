"""Signing and the token format, no database."""

import pytest

from polybit import signing
from polybit.signing import SignatureError
from polybit.wallet_cli import main as wallet

CHAIN = "polybit"


def _transfer(**overrides):
    fields = dict(chain=CHAIN, sender_id=101, payouts=[(102, 5_000_000)], nonce=1)
    fields.update(overrides)
    return signing.transfer_payload(**fields)


def test_sign_and_verify_roundtrip():
    key = signing.new_key()
    signed = signing.decode(signing.sign(key, _transfer(memo="lunch")))
    assert signing.verify(signing.public_bytes(key), signed.raw, signed.signature)
    tx = signing.parse_transfer(signed, chain=CHAIN)
    assert (tx.sender_id, tx.payouts, tx.nonce, tx.memo, tx.fee) == (101, [(102, 5_000_000)], 1, "lunch", 0)


def test_other_key_or_edited_payload_fails():
    key, other = signing.new_key(), signing.new_key()
    signed = signing.decode(signing.sign(key, _transfer()))
    assert not signing.verify(signing.public_bytes(other), signed.raw, signed.signature)
    edited = signed.raw.replace(b"5000000", b"9000000")
    assert not signing.verify(signing.public_bytes(key), edited, signed.signature)


def test_signature_is_domain_separated():
    key = signing.new_key()
    raw = signing.canonical(_transfer())
    assert not signing.verify(signing.public_bytes(key), raw, key.sign(raw))


@pytest.mark.parametrize("token", ["", "pb2.a.b", "pb1.abc", "pb1.!!.!!", "pb1." + "A" * 5000 + ".x"])
def test_garbage_tokens_are_rejected(token):
    with pytest.raises(SignatureError):
        signing.decode(token)


def test_non_canonical_payload_is_rejected():
    key = signing.new_key()
    raw = b'{"v": 1}'
    token = f"pb1.{signing._b64(raw)}.{signing._b64(key.sign(signing.DOMAIN + raw))}"
    with pytest.raises(SignatureError, match="canonical"):
        signing.decode(token)


def test_bind_checks_chain_and_key():
    key = signing.new_key()
    pub = signing.public_bytes(key)
    signed = signing.decode(signing.sign(key, signing.bind_payload(chain=CHAIN, user_id=7, pubkey=pub)))
    assert signing.check_bind(signed, chain=CHAIN) == (7, pub)
    with pytest.raises(SignatureError, match="different PolyBit network"):
        signing.check_bind(signed, chain="other")
    # Claiming someone else's public key: the signature won't match it.
    stolen = signing.bind_payload(chain=CHAIN, user_id=7, pubkey=signing.public_bytes(signing.new_key()))
    with pytest.raises(SignatureError, match="doesn't match"):
        signing.check_bind(signing.decode(signing.sign(key, stolen)), chain=CHAIN)


@pytest.mark.parametrize(
    "overrides, message",
    [
        (dict(chain="elsewhere"), "different PolyBit network"),
        (dict(payouts=[]), "recipients"),
        (dict(payouts=[(102, 0)]), "bad number"),
        (dict(payouts=[(102, -5)]), "bad number"),
        (dict(nonce=0), "bad number"),
        (dict(fee=-1), "bad number"),
        (dict(memo="  padded "), "memo"),
        (dict(memo="x" * 201), "memo"),
        (dict(expires_at=1), "expired"),
    ],
)
def test_bad_transfers_are_rejected(overrides, message):
    signed = signing.decode(signing.sign(signing.new_key(), _transfer(**overrides)))
    with pytest.raises(SignatureError, match=message):
        signing.parse_transfer(signed, chain=CHAIN)


def test_extra_fields_are_rejected():
    payload = _transfer()
    payload["admin"] = True
    with pytest.raises(SignatureError, match="isn't a transfer"):
        signing.parse_transfer(signing.decode(signing.sign(signing.new_key(), payload)), chain=CHAIN)


def test_bool_is_not_a_number():
    payload = _transfer()
    payload["nonce"] = True
    with pytest.raises(SignatureError):
        signing.parse_transfer(signing.decode(signing.sign(signing.new_key(), payload)), chain=CHAIN)


def test_wallet_cli(tmp_path, capsys):
    key = tmp_path / "w.key"
    assert wallet(["--key", str(key), "keygen"]) == 0
    assert wallet(["--key", str(key), "keygen"]) == 1  # never overwrites
    capsys.readouterr()

    assert wallet(["--key", str(key), "pubkey"]) == 0
    pub = bytes.fromhex(capsys.readouterr().out.strip())

    assert wallet(["--key", str(key), "bind", "--user", "101"]) == 0
    bind = signing.decode(capsys.readouterr().out.strip())
    assert signing.check_bind(bind, chain=CHAIN) == (101, pub)

    argv = ["--key", str(key), "send", "--from", "101", "--to", "102=2.5", "--to", "103=1", "--nonce", "4",
            "--fee", "0.01", "--memo", "split  bill"]
    assert wallet(argv) == 0
    signed = signing.decode(capsys.readouterr().out.strip())
    assert signing.verify(pub, signed.raw, signed.signature)
    tx = signing.parse_transfer(signed, chain=CHAIN)
    assert tx.payouts == [(102, 2_500_000), (103, 1_000_000)]
    assert (tx.nonce, tx.fee, tx.memo) == (4, 10_000, "split bill")
    assert tx.expires_at is not None


def test_wallet_cli_without_key(tmp_path, capsys):
    assert wallet(["--key", str(tmp_path / "missing.key"), "pubkey"]) == 1
    assert "keygen" in capsys.readouterr().err
