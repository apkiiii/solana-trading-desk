from __future__ import annotations

import pytest

from config.settings import (Settings, SettingsError, apply_user_change, load_defaults, load_settings,
                             validate_settings)


def test_defaults_are_valid_and_match_spec():
    defaults = load_defaults()
    assert validate_settings(defaults) == []
    cap = defaults["capital"]
    assert cap["total_eur"] == 50.0
    assert cap["reserve_eur"] == 27.5
    assert cap["trading_budget_eur"] == 17.5
    assert cap["fee_reserve_eur"] == 5.0
    assert cap["max_position_eur"] == 2.0
    assert cap["max_open_positions"] == 2
    assert cap["max_traders"] == 5  # demande de l'utilisateur (12/11/2026) : au moins 5 traders copiés
    assert cap["max_daily_loss_eur"] == 1.5
    assert defaults["schedule"]["timezone"] == "Europe/Paris"


def test_settings_are_read_only(db):
    settings = load_settings(db)
    with pytest.raises(TypeError):
        settings["capital"]["max_position_eur"] = 100.0  # type: ignore[index]
    with pytest.raises(AttributeError):
        settings._data = {}  # type: ignore[misc]
    assert isinstance(settings, Settings)


def test_only_dashboard_can_change_settings(db):
    for actor in ("agent", "judge", "sniper", "ai", ""):
        with pytest.raises(PermissionError):
            apply_user_change(db, "capital", "max_position_eur", 5.0, actor=actor)
    assert db.get_setting_overrides() == {}


def test_dashboard_change_is_validated_persisted_and_audited(db):
    apply_user_change(db, "capital", "max_position_eur", 1.5, actor="dashboard")
    assert load_settings(db)["capital"]["max_position_eur"] == 1.5
    audit = db.query_all("SELECT * FROM settings_audit")
    assert len(audit) == 1 and audit[0]["actor"] == "dashboard"


@pytest.mark.parametrize("section,key,value", [
    ("capital", "max_position_eur", 20.0),        # dépasse le budget de trading
    ("capital", "trading_budget_eur", 40.0),      # réserve + budget + frais > capital
    ("capital", "max_position_eur", "2"),         # mauvais type
    ("schedule", "start", "25:00"),               # heure invalide
    ("schedule", "timezone", "Mars/Olympus"),     # fuseau inconnu
    ("providers", "helius", {}),                  # section technique non modifiable
])
def test_invalid_changes_are_rejected(db, section, key, value):
    with pytest.raises((SettingsError, KeyError)):
        apply_user_change(db, section, key, value, actor="dashboard")
    assert load_settings(db)["capital"]["max_position_eur"] == 2.0


def test_corrupted_overrides_fall_back_to_defaults(db):
    db.put_setting_override("capital", "max_position_eur", 999.0, actor="dashboard")
    assert load_settings(db)["capital"]["max_position_eur"] == 2.0
