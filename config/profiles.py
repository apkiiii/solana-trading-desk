"""Profils de risque prêts à l'emploi : réglages simples, expliqués en français courant.

Appliquer un profil modifie plusieurs réglages d'un coup, avec les mêmes contrôles de cohérence
que les modifications manuelles (et uniquement depuis l'application).
"""
from __future__ import annotations

from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "prudent": {
        "title": "Prudent",
        "description": "Petites mises, traders très bien notés, sortie rapide en cas de baisse. "
                       "Moins de trades, pertes limitées.",
        "values": {
            ("capital", "max_position_eur"): 1.5,
            ("capital", "max_daily_loss_eur"): 1.0,
            ("judge", "min_trader_score"): 70.0,
            ("judge", "min_liquidity_usd"): 30000.0,
            ("judge", "max_total_fees_pct"): 2.5,
            ("exit", "stop_loss_pct"): 20.0,
            ("exit", "take_profit_pct"): 40.0,
            ("execution", "slippage_bps"): 100,
        },
    },
    "equilibre": {
        "title": "Équilibré",
        "description": "Le réglage conseillé pour démarrer : mises de 2 €, contrôles stricts, "
                       "stop-loss à -25 % et prise de profit à +50 %.",
        "values": {
            ("capital", "max_position_eur"): 2.0,
            ("capital", "max_daily_loss_eur"): 1.5,
            ("judge", "min_trader_score"): 60.0,
            ("judge", "min_liquidity_usd"): 20000.0,
            ("judge", "max_total_fees_pct"): 3.0,
            ("exit", "stop_loss_pct"): 25.0,
            ("exit", "take_profit_pct"): 50.0,
            ("execution", "slippage_bps"): 150,
        },
    },
    "dynamique": {
        "title": "Dynamique",
        "description": "Mises un peu plus grosses et plus de tokens acceptés. Plus d'occasions, "
                       "mais des pertes possibles plus importantes.",
        "values": {
            ("capital", "max_position_eur"): 2.5,
            ("capital", "max_daily_loss_eur"): 2.5,
            ("judge", "min_trader_score"): 55.0,
            ("judge", "min_liquidity_usd"): 15000.0,
            ("judge", "max_total_fees_pct"): 4.0,
            ("exit", "stop_loss_pct"): 30.0,
            ("exit", "take_profit_pct"): 80.0,
            ("execution", "slippage_bps"): 200,
        },
    },
}


def detect_profile(settings: Any) -> str | None:
    """Nom du profil qui correspond exactement aux réglages actuels, sinon None (personnalisé)."""
    for name, profile in PROFILES.items():
        if all(settings[section][key] == value for (section, key), value in profile["values"].items()):
            return name
    return None


def apply_profile(db: Any, name: str) -> None:
    from config.settings import apply_user_change, load_defaults, merge_overrides, validate_settings
    if name not in PROFILES:
        raise ValueError(f"Profil inconnu : {name}")
    # Validation de l'ensemble avant d'écrire quoi que ce soit (pas d'état intermédiaire incohérent).
    merged = merge_overrides(load_defaults(), db.get_setting_overrides())
    for (section, key), value in PROFILES[name]["values"].items():
        merged[section][key] = value
    errors = validate_settings(merged)
    if errors:
        raise ValueError(" ; ".join(errors))
    ordered = sorted(PROFILES[name]["values"].items(),
                     key=lambda item: item[0] != ("capital", "max_position_eur") or item[1] > 2.0)
    for (section, key), value in ordered:
        apply_user_change(db, section, key, value, actor="dashboard")
