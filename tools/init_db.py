"""Crée (ou met à jour) la base de données locale et affiche son état.

Utilisation (dans le dossier du projet) :
    python -m tools.init_db
"""
from __future__ import annotations

from config.settings import load_settings
from core.paths import BACKUPS_DIR, DB_PATH, ensure_dirs
from data.database import Database
from tools.common import ok, setup_console, title


def main() -> int:
    setup_console()
    ensure_dirs()
    title("Base de données")
    db = Database(DB_PATH)
    ok(f"Base prête : {DB_PATH} (schéma v{db.schema_version}, journal {db.journal_mode()})")
    control = db.get_control()
    ok(f"Mode : {control['mode']} | Bot : {control['bot_state']} | STOP manuel : "
       f"{'oui' if control['manual_stop'] else 'non'} | Urgence : {'oui' if control['emergency_stop'] else 'non'}")
    settings = load_settings(db)
    cap = settings["capital"]
    ok(f"Capital {cap['total_eur']} € | position max {cap['max_position_eur']} € | "
       f"{cap['max_open_positions']} positions max | perte max/jour {cap['max_daily_loss_eur']} €")
    backup = db.backup(BACKUPS_DIR)
    ok(f"Sauvegarde créée : {backup.name}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
