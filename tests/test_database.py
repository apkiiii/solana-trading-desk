from __future__ import annotations

import pytest

from data.database import Database, DatabaseError
from data.models import SwapEvent
from tests.conftest import OTHER_WALLET, SOL, TOKEN, WALLET


def _event(signature: str = "sig1") -> SwapEvent:
    return SwapEvent(signature=signature, wallet=WALLET, slot=1, block_time=1_700_000_000, side="BUY",
                     token_mint=TOKEN, token_amount_raw=1000, token_decimals=6, quote_mint=SOL,
                     quote_amount_raw=10_000_000, quote_decimals=9, fee_lamports=5000, fee_paid_by_wallet=True)


def test_schema_and_safe_defaults(db):
    assert db.schema_version >= 1
    assert db.journal_mode().lower() == "wal"
    control = db.get_control()
    assert control["mode"] == "SIMULATION"
    assert control["bot_state"] == "STOPPED"
    assert control["manual_stop"] is True
    assert control["emergency_stop"] is False


def test_reopening_keeps_state_and_never_arms_live(tmp_path):
    path = tmp_path / "desk.db"
    first = Database(path)
    first.set_control(actor="dashboard", mode="LIVE", bot_state="RUNNING", manual_stop=False)
    first.close()
    second = Database(path)
    control = second.get_control()
    assert control["mode"] == "LIVE"
    # L'état « armé » du mode LIVE n'existe pas en base : il ne peut donc pas survivre à un redémarrage.
    assert "live_armed" not in control
    second.close()


def test_control_validation_and_audit(db):
    with pytest.raises(DatabaseError):
        db.set_control(actor="dashboard", mode="TURBO")
    with pytest.raises(DatabaseError):
        db.set_control(actor="dashboard", max_position=100)
    db.set_control(actor="dashboard", emergency_stop=True)
    assert db.get_control()["emergency_stop"] is True
    assert len(db.query_all("SELECT * FROM control_audit")) == 1


def test_duplicate_source_event_is_refused(db):
    assert db.insert_source_event(_event()) is True
    assert db.insert_source_event(_event()) is False
    assert db.has_source_event("sig1")
    assert len(db.recent_source_events()) == 1


def test_traders_validation_and_limit(db):
    with pytest.raises(DatabaseError):
        db.upsert_trader("pas-une-adresse")
    db.upsert_trader(WALLET, "trader A", max_traders=1)
    with pytest.raises(DatabaseError):
        db.upsert_trader(OTHER_WALLET, "trader B", max_traders=1)
    db.upsert_trader(WALLET, None, max_traders=1)  # mise à jour d'un trader existant : autorisée
    rows = db.list_traders()
    assert len(rows) == 1 and rows[0]["label"] == "trader A"
    db.set_trader_active(WALLET, False)
    assert db.list_traders(active_only=True) == []


def test_api_usage_counts_credits(db):
    db.add_api_usage("helius", credits=10)
    db.add_api_usage("helius", credits=5, errors=1)
    assert db.month_credits("helius") == 15


def test_backup_rotation(db, tmp_path):
    folder = tmp_path / "backups"
    for _ in range(4):
        db.backup(folder, keep=2)
    files = list(folder.glob("desk-*.db"))
    assert len(files) == 2
    restored = Database(sorted(files)[-1])
    assert restored.get_control()["mode"] == "SIMULATION"
    restored.close()
