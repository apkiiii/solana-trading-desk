"""Point d'entrée de SolanaTradingDesk.exe.

    SolanaTradingDesk.exe              -> démarre le serveur local et ouvre l'interface
    SolanaTradingDesk.exe --diagnose   -> diagnostic en ligne de commande
    SolanaTradingDesk.exe --version    -> affiche la version
    SolanaTradingDesk.exe --no-window  -> serveur seul (ex. VPS, version 0.8)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8766
URL = f"http://127.0.0.1:{PORT}/"


def _already_running() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}api/ping", timeout=1.5) as response:  # noqa: S310 - adresse locale fixe
            return json.loads(response.read()).get("app") == "SolanaTradingDesk"
    except (OSError, ValueError):
        return False


def _browser_candidates() -> list[str]:
    candidates: list[str] = []
    if sys.platform == "win32":
        roots = [os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")]
        for root in filter(None, roots):
            candidates += [str(Path(root) / "Microsoft/Edge/Application/msedge.exe"),
                           str(Path(root) / "Google/Chrome/Application/chrome.exe")]
    for name in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return [c for c in candidates if Path(c).exists()]


def open_window(profile_dir: Path) -> None:
    """Ouvre l'interface dans une fenêtre d'application (sans barre d'adresse) si possible."""
    for browser in _browser_candidates():
        try:
            subprocess.Popen([browser, f"--app={URL}", f"--user-data-dir={profile_dir}", "--window-size=1440,900",  # noqa: S603
                              "--no-first-run", "--no-default-browser-check"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue
    webbrowser.open(URL)


def _run_app(open_ui: bool) -> int:
    from app.server import serve
    from core.logs import setup_logging
    from core.paths import DATA_ROOT, DB_PATH, ensure_dirs
    from core.version import __version__
    from data.database import Database

    if _already_running():
        if open_ui:
            open_window(DATA_ROOT / "ui-profile")
        return 0
    ensure_dirs()
    log = setup_logging("app", console=not getattr(sys, "frozen", False))
    log.info("Démarrage de Solana Trading Desk %s", __version__)
    db = Database(DB_PATH)
    try:
        asyncio.run(serve(db, port=PORT, idle_exit_seconds=180.0 if open_ui else None,
                          on_ready=(lambda: open_window(DATA_ROOT / "ui-profile")) if open_ui else None))
    finally:
        db.close()
        log.info("Arrêt de Solana Trading Desk")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="SolanaTradingDesk")
    parser.add_argument("--diagnose", action="store_true", help="diagnostic des connexions")
    parser.add_argument("--wallet", help="avec --diagnose : adresse d'un trader à tester")
    parser.add_argument("--no-window", action="store_true", help="serveur seul, sans ouvrir de fenêtre")
    parser.add_argument("--version", action="store_true", help="affiche la version")
    args = parser.parse_args(argv)

    if args.version:
        from core.version import __version__
        print(__version__)
        return 0
    if args.diagnose:
        from core.logs import setup_logging
        from core.paths import ensure_dirs
        from tools import diagnose
        from tools.common import setup_console
        setup_console()
        ensure_dirs()
        setup_logging("diagnose", console=False)
        return asyncio.run(diagnose.run(args.wallet))
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return _run_app(open_ui=not args.no_window)


if __name__ == "__main__":
    raise SystemExit(main())
