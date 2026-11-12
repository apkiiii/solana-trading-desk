"""Chargement des clés API depuis le fichier .env.

Règles :
- le fichier .env ne contient QUE des clés API (lecture de données, devis) ;
- toute variable qui ressemble à une clé privée ou à une phrase de récupération
  fait échouer le chargement : ces secrets n'ont rien à faire dans un fichier texte ;
- chaque clé chargée est enregistrée pour être masquée dans les logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from core.logs import register_secret
from core.paths import ENV_FILE

_FORBIDDEN_MARKERS = ("PRIVATE", "SECRET_KEY", "SEED", "MNEMONIC", "KEYPAIR", "PHRASE")
_KNOWN = ("HELIUS_API_KEY", "JUPITER_API_KEY", "RUGCHECK_API_KEY", "SOLANA_RPC_URL", "SOLANA_WS_URL")


class SecretsError(RuntimeError):
    pass


def mask(value: str | None) -> str:
    """Représentation sûre d'un secret pour l'affichage : début + longueur."""
    if not value:
        return "(vide)"
    return f"{value[:4]}... ({len(value)} caractères)"


@dataclass(frozen=True)
class ApiSecrets:
    helius_api_key: str = field(default="", repr=False)
    jupiter_api_key: str = field(default="", repr=False)
    rugcheck_api_key: str = field(default="", repr=False)
    rpc_url_override: str = field(default="", repr=False)
    ws_url_override: str = field(default="", repr=False)

    def __repr__(self) -> str:  # jamais de valeur en clair
        return (f"ApiSecrets(helius={mask(self.helius_api_key)}, jupiter={mask(self.jupiter_api_key)}, "
                f"rugcheck={mask(self.rugcheck_api_key)}, rpc_override={'oui' if self.rpc_url_override else 'non'})")


def save_api_keys(values: dict[str, str], env_path: Path | None = None) -> Path:
    """Enregistre des clés API dans le fichier .env (utilisé par l'application).

    Seules les variables connues sont acceptées ; les autres lignes déjà présentes sont conservées.
    """
    path = env_path or ENV_FILE
    for name, value in values.items():
        if name not in _KNOWN:
            raise SecretsError(f"Variable non autorisée : {name}")
        if any(ch in value for ch in "\r\n\"' "):
            raise SecretsError(f"La valeur de {name} contient des caractères invalides (espace, guillemet…).")
    existing: list[str] = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written: set[str] = set()
    lines: list[str] = []
    for line in existing:
        name = line.split("=", 1)[0].strip()
        if name in values:
            lines.append(f"{name}={values[name].strip()}")
            written.add(name)
        else:
            lines.append(line)
    for name, value in values.items():
        if name not in written:
            lines.append(f"{name}={value.strip()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for value in values.values():
        register_secret(value.strip())
    return path


def load_secrets(env_path: Path | None = None, environ: dict[str, str] | None = None) -> ApiSecrets:
    path = env_path or ENV_FILE
    values: dict[str, str] = {}
    if path.exists():
        values.update({k: (v or "") for k, v in dotenv_values(path).items()})
    env = os.environ if environ is None else environ
    for name in _KNOWN:
        if env.get(name):
            values[name] = env[name]

    for name, value in values.items():
        upper = name.upper()
        if value and any(marker in upper for marker in _FORBIDDEN_MARKERS):
            raise SecretsError(
                f"La variable '{name}' du fichier .env ressemble à une clé privée ou une phrase de "
                "récupération. Supprime-la : ce secret ne doit jamais être stocké dans .env.")

    secrets = ApiSecrets(
        helius_api_key=values.get("HELIUS_API_KEY", "").strip(),
        jupiter_api_key=values.get("JUPITER_API_KEY", "").strip(),
        rugcheck_api_key=values.get("RUGCHECK_API_KEY", "").strip(),
        rpc_url_override=values.get("SOLANA_RPC_URL", "").strip(),
        ws_url_override=values.get("SOLANA_WS_URL", "").strip(),
    )
    for value in (secrets.helius_api_key, secrets.jupiter_api_key, secrets.rugcheck_api_key,
                  secrets.rpc_url_override, secrets.ws_url_override):
        register_secret(value)
    return secrets
