"""Outils partagés par les scripts en ligne de commande."""
from __future__ import annotations

import sys


def setup_console() -> None:
    """Affichage correct des accents dans PowerShell / l'invite de commandes Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def ok(message: str) -> None:
    print(f"  [OK]     {message}")


def fail(message: str) -> None:
    print(f"  [ECHEC]  {message}")


def warn(message: str) -> None:
    print(f"  [ATTENTION] {message}")


def title(message: str) -> None:
    print(f"\n=== {message} ===")
