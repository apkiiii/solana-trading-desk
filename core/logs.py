"""Journalisation avec masquage automatique des secrets.

Tout ce qui est écrit dans les logs passe par ``redact`` :
- chaque valeur secrète enregistrée avec ``register_secret`` (clés API, clé privée)
  est remplacée par ``***`` ;
- tout paramètre ``api-key=...`` / ``api_key: ...`` est masqué ;
- tout tableau de 64 entiers (format des fichiers de clé Solana) est masqué.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.paths import LOGS_DIR

_SECRETS: set[str] = set()
_SECRETS_LOCK = threading.Lock()

_API_KEY_RE = re.compile(r"(?i)(api[-_]?key\s*[=:]\s*)([^&\s\"',;}]+)")
_INT_ARRAY_RE = re.compile(r"\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]")

MASK = "***"


def register_secret(value: str | None) -> None:
    """Ajoute une valeur à masquer partout dans les logs (ignorée si trop courte)."""
    if value and len(value) >= 8:
        with _SECRETS_LOCK:
            _SECRETS.add(value)


def redact(text: str) -> str:
    with _SECRETS_LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    text = _API_KEY_RE.sub(lambda m: m.group(1) + MASK, text)
    text = _INT_ARRAY_RE.sub("[CLE MASQUEE]", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formate le message (y compris les traces d'erreur) puis masque les secrets."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(name: str = "engine", level: int = logging.INFO, console: bool = True,
                  logs_dir: Path | None = None) -> logging.Logger:
    """Configure le journal racine : fichier tournant ``logs/<name>.log`` + console."""
    directory = logs_dir or LOGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter(_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = RotatingFileHandler(directory / f"{name}.log", maxBytes=5_000_000,
                                       backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # httpx journalise les URL complètes (avec la clé API) au niveau INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger(name)
