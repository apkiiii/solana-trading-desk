"""Gestion des traders suivis en ligne de commande (l'application le fera aussi à l'étape 9).

Utilisation :
    python -m tools.traders list
    python -m tools.traders add <ADRESSE_PUBLIQUE> [nom]
    python -m tools.traders disable <ADRESSE_PUBLIQUE>
    python -m tools.traders enable <ADRESSE_PUBLIQUE>
"""
from __future__ import annotations

import sys

from config.settings import load_settings
from core.paths import DB_PATH, ensure_dirs
from data.database import Database, DatabaseError
from tools.common import fail, ok, setup_console, title


def main(argv: list[str]) -> int:
    setup_console()
    ensure_dirs()
    db = Database(DB_PATH)
    settings = load_settings(db)
    try:
        if not argv or argv[0] == "list":
            title("Traders suivis")
            rows = db.list_traders()
            if not rows:
                print("  (aucun trader enregistré)")
            for row in rows:
                state = "actif" if row["active"] else "désactivé"
                score = "-" if row["score"] is None else f"{row['score']:.0f}/100"
                print(f"  {row['address']}  {row['label'] or ''}  [{state}]  score {score}")
            return 0
        command, address = argv[0], (argv[1] if len(argv) > 1 else "")
        if command == "add":
            label = " ".join(argv[2:]) or None
            db.upsert_trader(address, label, max_traders=int(settings["capital"]["max_traders"]))
            ok(f"Trader ajouté : {address}")
        elif command == "disable":
            db.set_trader_active(address, False)
            ok(f"Trader désactivé : {address}")
        elif command == "enable":
            db.set_trader_active(address, True)
            ok(f"Trader réactivé : {address}")
        else:
            fail(f"Commande inconnue : {command}")
            print(__doc__)
            return 2
        return 0
    except DatabaseError as exc:
        fail(str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
