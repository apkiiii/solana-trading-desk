"""Wallet de trading du bot (compte Phantom DÉDIÉ, jamais le compte principal).

L'utilisateur crée un nouveau compte dans Phantom, exporte SA clé privée (chaîne base58) et la
colle dans l'application, sur son propre PC. La clé :
- est rangée dans le Gestionnaire d'identifiants Windows (repli : fichier protégé) ;
- n'est jamais affichée, jamais écrite dans les journaux, jamais envoyée sur le réseau ;
- sert uniquement à signer localement les transactions approuvées par le JUDGE.

Une phrase de récupération (12 ou 24 mots) est toujours refusée.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.logs import register_secret
from core.paths import DATA_ROOT

log = logging.getLogger(__name__)

SERVICE = "SolanaTradingDesk"
ACCOUNT = "trading-wallet-key"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class TradingWalletError(RuntimeError):
    pass


def _b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            raise TradingWalletError("Caractère invalide : ce n'est pas une clé privée Phantom.")
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading + raw


def parse_private_key(secret: str) -> Keypair:
    """Clé privée Phantom (base58) ou tableau JSON de 64 nombres -> Keypair. Refuse les phrases de récupération."""
    text = (secret or "").strip()
    if not text:
        raise TradingWalletError("Clé privée vide.")
    words = text.split()
    if not text.startswith("[") and len(words) >= 2:
        raise TradingWalletError("Ceci ressemble à une phrase de récupération : elle n'est JAMAIS acceptée. "
                                 "Dans Phantom, exporte la « clé privée » du compte dédié au bot.")
    if text.startswith("["):
        try:
            raw = bytes(json.loads(text))
        except (ValueError, TypeError) as exc:
            raise TradingWalletError("Tableau de clé illisible.") from exc
    else:
        raw = _b58decode(text)
    if len(raw) != 64:
        raise TradingWalletError("Longueur incorrecte : une clé privée Phantom fait 64 octets.")
    try:
        keypair = Keypair.from_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise TradingWalletError("Clé privée invalide.") from exc
    if bytes(keypair.pubkey()) != raw[32:]:
        raise TradingWalletError("Clé privée incohérente (partie publique différente).")
    return keypair


def sign_transaction(keypair: Keypair, tx_base64: str) -> str:
    """Signe (partiellement si besoin) une transaction base64 de Jupiter et la renvoie en base64."""
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_base64))
    except Exception as exc:  # noqa: BLE001
        raise TradingWalletError("Transaction à signer illisible.") from exc
    message = tx.message
    header = message.header
    signers = list(message.account_keys)[: header.num_required_signatures]
    me = keypair.pubkey()
    if me not in signers:
        raise TradingWalletError("Le wallet du bot n'est pas signataire de cette transaction : refus.")
    signature = keypair.sign_message(to_bytes_versioned(message))
    signatures = list(tx.signatures)
    signatures[signers.index(me)] = signature
    signed = VersionedTransaction.populate(message, signatures)
    return base64.b64encode(bytes(signed)).decode()


class TradingWalletStore:
    def __init__(self, fallback_dir: Path | None = None, *, use_keyring: bool = True):
        self.fallback_file = (fallback_dir or DATA_ROOT / "secure") / "trading_wallet.key"
        self.use_keyring = use_keyring

    # ------------------------------------------------------------- stockage
    def _keyring(self):
        if not self.use_keyring:
            return None
        try:
            import keyring
            from keyring.backends import fail
            if isinstance(keyring.get_keyring(), fail.Keyring):
                return None
            return keyring
        except Exception:  # noqa: BLE001
            return None

    def _read(self) -> str | None:
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(SERVICE, ACCOUNT)
            except Exception:  # noqa: BLE001
                value = None
            if value:
                register_secret(value)
                return value
        if self.fallback_file.exists():
            value = self.fallback_file.read_text(encoding="ascii").strip()
            register_secret(value)
            return value or None
        return None

    def _write(self, value: str) -> str:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(SERVICE, ACCOUNT, value)
                self.fallback_file.unlink(missing_ok=True)
                return "Gestionnaire d'identifiants Windows"
            except Exception:  # noqa: BLE001
                log.warning("Coffre-fort indisponible : clé du wallet rangée dans un fichier protégé.")
        self.fallback_file.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_file.write_text(value, encoding="ascii")
        try:
            os.chmod(self.fallback_file, 0o600)
        except OSError:
            pass
        return "fichier protégé du dossier de données"

    # -------------------------------------------------------------- public
    def exists(self) -> bool:
        return self._read() is not None

    def import_key(self, secret: str) -> dict[str, str]:
        """Vérifie et range la clé. Renvoie seulement l'adresse publique et l'emplacement."""
        register_secret((secret or "").strip())
        keypair = parse_private_key(secret)
        stored = base64.b64encode(bytes(keypair)).decode()
        register_secret(stored)
        where = self._write(stored)
        return {"address": str(keypair.pubkey()), "stored_in": where}

    def _keypair(self) -> Keypair:
        stored = self._read()
        if stored is None:
            raise TradingWalletError("Aucun wallet de trading : colle la clé du compte Phantom dédié dans Connexions.")
        try:
            return Keypair.from_bytes(base64.b64decode(stored))
        except Exception as exc:  # noqa: BLE001
            raise TradingWalletError("Clé du wallet de trading illisible : importe-la à nouveau.") from exc

    def address(self) -> str | None:
        if not self.exists():
            return None
        return str(self._keypair().pubkey())

    def pubkey(self) -> Pubkey:
        return self._keypair().pubkey()

    def sign(self, tx_base64: str) -> str:
        return sign_transaction(self._keypair(), tx_base64)

    def delete(self) -> None:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, ACCOUNT)
            except Exception:  # noqa: BLE001
                pass
        self.fallback_file.unlink(missing_ok=True)
