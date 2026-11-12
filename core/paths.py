"""Emplacements des fichiers.

Deux cas :
- **logiciel installé** (SolanaTradingDesk.exe) : le programme est dans « Program Files »
  (lecture seule) et tes données (base, logs, sauvegardes, clés API) sont dans
  ``%LOCALAPPDATA%\\SolanaTradingDesk`` — elles sont conservées lors des mises à jour ;
- **code source** (développement, tests) : tout reste dans le dossier du projet.

La variable d'environnement ``SOLANA_DESK_HOME`` permet de forcer un autre dossier de données.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "SolanaTradingDesk"
FROZEN = bool(getattr(sys, "frozen", False))

# Dossier des fichiers fournis avec le programme (defaults.toml, icône…)
if FROZEN:
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    BUNDLE_DIR = Path(__file__).resolve().parent.parent
ROOT = BUNDLE_DIR


def _default_data_root() -> Path:
    override = os.environ.get("SOLANA_DESK_HOME")
    if override:
        return Path(override)
    if FROZEN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return BUNDLE_DIR


DATA_ROOT = _default_data_root()
CONFIG_DIR = BUNDLE_DIR / "config"
DEFAULTS_FILE = CONFIG_DIR / "defaults.toml"
ASSETS_DIR = BUNDLE_DIR / "assets"
STORAGE_DIR = DATA_ROOT / "storage"
LOGS_DIR = DATA_ROOT / "logs"
BACKUPS_DIR = DATA_ROOT / "backups"
UPDATES_DIR = DATA_ROOT / "updates"
DB_PATH = STORAGE_DIR / "desk.db"
ENV_FILE = DATA_ROOT / ".env"
# Fichier témoin de l'arrêt d'urgence : vérifié juste avant chaque signature,
# même si la base de données est inaccessible.
EMERGENCY_STOP_FILE = STORAGE_DIR / "EMERGENCY_STOP"


def ensure_dirs() -> None:
    for directory in (DATA_ROOT, STORAGE_DIR, LOGS_DIR, BACKUPS_DIR, UPDATES_DIR):
        directory.mkdir(parents=True, exist_ok=True)
