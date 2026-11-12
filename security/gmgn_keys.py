"""Clé de signature de l'API GMGN (Ed25519).

GMGN demande d'enregistrer une **clé publique** sur gmgn.ai/ai ; chaque ordre est ensuite signé
avec la **clé secrète** correspondante. Cette clé ne donne PAS accès au wallet : elle prouve
seulement que les ordres viennent bien de ce logiciel.

- La paire est générée ici (aléa du système), au format attendu par GMGN :
  clé publique SPKI PEM, clé secrète PKCS#8 PEM.
- La clé secrète est rangée dans le Gestionnaire d'identifiants Windows (``keyring``).
  Si aucun coffre-fort n'est disponible (ex. serveur Linux), elle est écrite dans un fichier
  lisible uniquement par l'utilisateur, dans le dossier de données.
- Elle n'est jamais affichée, jamais journalisée, jamais envoyée : seule la signature part.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

from solders.keypair import Keypair

from core.logs import register_secret
from core.paths import DATA_ROOT

log = logging.getLogger(__name__)

SERVICE = "SolanaTradingDesk"
ACCOUNT = "gmgn-api-signing-key"
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")          # Ed25519 SubjectPublicKeyInfo
_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")  # Ed25519 PrivateKeyInfo


class GmgnKeyError(RuntimeError):
    pass


def _pem(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def _seed_from_pem(pem: str) -> bytes:
    match = re.search(r"-----BEGIN PRIVATE KEY-----(.+?)-----END PRIVATE KEY-----", pem, re.S)
    if not match:
        raise GmgnKeyError("Clé secrète GMGN illisible.")
    der = base64.b64decode("".join(match.group(1).split()))
    if not der.startswith(_PKCS8_PREFIX) or len(der) != len(_PKCS8_PREFIX) + 32:
        raise GmgnKeyError("La clé secrète GMGN n'est pas une clé Ed25519 valide.")
    return der[len(_PKCS8_PREFIX):]


def public_pem_from_seed(seed: bytes) -> str:
    return _pem("PUBLIC KEY", _SPKI_PREFIX + bytes(Keypair.from_seed(seed).pubkey()))


class GmgnKeyStore:
    def __init__(self, fallback_dir: Path | None = None, *, use_keyring: bool = True):
        self.fallback_file = (fallback_dir or DATA_ROOT / "secure") / "gmgn_signing_key.pem"
        self.use_keyring = use_keyring

    # ------------------------------------------------------------- stockage
    def _keyring(self):
        if not self.use_keyring:
            return None
        try:
            import keyring
            from keyring.backends import fail
            backend = keyring.get_keyring()
            if isinstance(backend, fail.Keyring):
                return None
            return keyring
        except Exception:  # noqa: BLE001 - aucun coffre-fort disponible
            return None

    def _read_private(self) -> str | None:
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(SERVICE, ACCOUNT)
            except Exception:  # noqa: BLE001
                value = None
            if value:
                return value
        if self.fallback_file.exists():
            return self.fallback_file.read_text(encoding="ascii")
        return None

    def _write_private(self, pem: str) -> str:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(SERVICE, ACCOUNT, pem)
                self.fallback_file.unlink(missing_ok=True)
                return "Gestionnaire d'identifiants Windows"
            except Exception:  # noqa: BLE001
                log.warning("Coffre-fort indisponible : clé GMGN rangée dans un fichier protégé.")
        self.fallback_file.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_file.write_text(pem, encoding="ascii")
        try:
            os.chmod(self.fallback_file, 0o600)
        except OSError:
            pass
        return "fichier protégé du dossier de données"

    # -------------------------------------------------------------- public
    def exists(self) -> bool:
        return self._read_private() is not None

    def create(self, *, replace: bool = False) -> dict[str, str]:
        if self.exists() and not replace:
            raise GmgnKeyError("Une clé GMGN existe déjà. La remplacer rendra ta clé API GMGN actuelle inutilisable.")
        seed = os.urandom(32)
        private_pem = _pem("PRIVATE KEY", _PKCS8_PREFIX + seed)
        register_secret(private_pem)
        where = self._write_private(private_pem)
        return {"public_pem": public_pem_from_seed(seed), "stored_in": where}

    def public_pem(self) -> str | None:
        private = self._read_private()
        if private is None:
            return None
        register_secret(private)
        return public_pem_from_seed(_seed_from_pem(private))

    def sign(self, message: str) -> str:
        """Signature Ed25519 (base64) du message, au format attendu par l'API GMGN."""
        private = self._read_private()
        if private is None:
            raise GmgnKeyError("Aucune clé GMGN : crée-la dans Connexions.")
        register_secret(private)
        signature = Keypair.from_seed(_seed_from_pem(private)).sign_message(message.encode("utf-8"))
        return base64.b64encode(bytes(signature)).decode()

    def delete(self) -> None:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, ACCOUNT)
            except Exception:  # noqa: BLE001
                pass
        self.fallback_file.unlink(missing_ok=True)
