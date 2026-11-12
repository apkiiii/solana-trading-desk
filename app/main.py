"""Point d'entrée de SolanaTradingDesk.exe.

    SolanaTradingDesk.exe              -> ouvre l'application
    SolanaTradingDesk.exe --diagnose   -> diagnostic en ligne de commande
    SolanaTradingDesk.exe --version    -> affiche la version
    SolanaTradingDesk.exe --engine     -> moteur en arrière-plan (disponible à l'étape 8)
"""
from __future__ import annotations

import argparse
import logging
import sys


def _run_gui() -> int:
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.theme import STYLESHEET
    from app.window import MainWindow
    from core.logs import setup_logging
    from core.paths import DATA_ROOT, DB_PATH, ensure_dirs
    from core.version import __version__
    from data.database import Database

    ensure_dirs()
    log = setup_logging("app", console=False)
    app = QApplication(sys.argv)
    app.setApplicationName("SolanaTradingDesk")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(STYLESHEET)

    lock = QLockFile(str(DATA_ROOT / "app.lock"))
    if not lock.tryLock(200):
        QMessageBox.information(None, "Déjà ouvert", "MY SOLANA TRADING DESK est déjà ouvert.")
        return 0

    try:
        db = Database(DB_PATH)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ouverture de la base impossible")
        QMessageBox.critical(None, "Erreur", f"Impossible d'ouvrir la base de données :\n{exc}")
        return 1

    log.info("Application démarrée (version %s)", __version__)
    window = MainWindow(db)
    window.show()
    code = app.exec()
    db.close()
    lock.unlock()
    log.info("Application fermée")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="SolanaTradingDesk")
    parser.add_argument("--diagnose", action="store_true", help="diagnostic des connexions")
    parser.add_argument("--wallet", help="avec --diagnose : adresse d'un trader à tester")
    parser.add_argument("--engine", action="store_true", help="moteur en arrière-plan")
    parser.add_argument("--version", action="store_true", help="affiche la version")
    args = parser.parse_args(argv)

    if args.version:
        from core.version import __version__
        print(__version__)
        return 0
    if args.diagnose:
        import asyncio

        from core.logs import setup_logging
        from core.paths import ensure_dirs
        from tools import diagnose
        from tools.common import setup_console
        setup_console()
        ensure_dirs()
        setup_logging("diagnose", console=False)
        return asyncio.run(diagnose.run(args.wallet))
    if args.engine:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("engine").warning("Le moteur de trading arrive dans une prochaine version.")
        return 0
    return _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
