"""Wallet de trading dédié : import, refus des phrases de récupération, signature, aucune fuite."""
from __future__ import annotations

import base64
import json
import logging

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.null_signer import NullSigner
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.logs import redact
from security.trading_wallet import TradingWalletError, TradingWalletStore, _b58decode, parse_private_key

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def unsigned_tx(*signers: Pubkey) -> str:
    program = Pubkey.from_string("11111111111111111111111111111111")
    ix = Instruction(program, bytes([2, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]),
                     [AccountMeta(s, True, True) for s in signers])
    msg = MessageV0.try_compile(signers[0], [ix], [], Hash.default())
    tx = VersionedTransaction(msg, [NullSigner(s) for s in signers])
    return base64.b64encode(bytes(tx)).decode()


def test_import_phantom_base58_and_json(tmp_path):
    kp = Keypair()
    store = TradingWalletStore(tmp_path, use_keyring=False)
    assert not store.exists() and store.address() is None
    result = store.import_key(b58encode(bytes(kp)))
    assert result["address"] == str(kp.pubkey())
    assert store.address() == str(kp.pubkey())
    assert parse_private_key(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()
    assert _b58decode(b58encode(b"\0\0abc")) == b"\0\0abc"
    # le fichier ne contient pas la clé en base58 d'origine et n'est lisible que par l'utilisateur
    assert b58encode(bytes(kp)) not in store.fallback_file.read_text()


def test_refuses_seed_phrase_and_garbage(tmp_path):
    store = TradingWalletStore(tmp_path, use_keyring=False)
    phrase = " ".join(["abandon"] * 11 + ["about"])
    with pytest.raises(TradingWalletError, match="phrase de récupération"):
        store.import_key(phrase)
    with pytest.raises(TradingWalletError):
        store.import_key("0OIl")
    with pytest.raises(TradingWalletError, match="64 octets"):
        store.import_key(b58encode(bytes(Keypair())[:32]))
    bad = bytes(Keypair())[:32] + bytes(Keypair().pubkey())
    with pytest.raises(TradingWalletError):
        store.import_key(b58encode(bad))
    assert not store.exists()


def test_signs_only_when_signer(tmp_path):
    kp = Keypair()
    store = TradingWalletStore(tmp_path, use_keyring=False)
    store.import_key(b58encode(bytes(kp)))
    signed = VersionedTransaction.from_bytes(base64.b64decode(store.sign(unsigned_tx(kp.pubkey()))))
    assert signed.signatures[0].verify(kp.pubkey(), to_bytes_versioned(signed.message))
    # transaction à deux signataires (ex. devis RFQ) : signature partielle à la bonne place
    other = Keypair()
    partial = VersionedTransaction.from_bytes(base64.b64decode(store.sign(unsigned_tx(other.pubkey(), kp.pubkey()))))
    index = list(partial.message.account_keys).index(kp.pubkey())
    assert partial.signatures[index].verify(kp.pubkey(), to_bytes_versioned(partial.message))
    with pytest.raises(TradingWalletError, match="pas signataire"):
        store.sign(unsigned_tx(other.pubkey()))


def test_secret_never_logged(tmp_path, caplog):
    kp = Keypair()
    secret = b58encode(bytes(kp))
    TradingWalletStore(tmp_path, use_keyring=False).import_key(secret)
    assert secret not in redact(f"erreur avec {secret}")
    caplog.set_level(logging.DEBUG)
    assert secret not in caplog.text
