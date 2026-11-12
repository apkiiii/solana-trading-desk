"""Réglages du trading desk.

- ``defaults.toml`` contient les valeurs d'origine.
- Les modifications faites dans l'application (et seulement là) sont stockées en base
  et fusionnées par-dessus.
- Les agents reçoivent un objet ``Settings`` immuable : il n'a aucune méthode d'écriture.
- ``apply_user_change`` est la seule porte d'entrée pour modifier un réglage ; elle
  exige ``actor="dashboard"``, vérifie le type et la cohérence, et journalise le changement.
"""
from __future__ import annotations

import copy
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.paths import DEFAULTS_FILE

if TYPE_CHECKING:
    from data.database import Database

EDITABLE_SECTIONS = frozenset({"capital", "judge", "exit", "execution", "schedule"})
ALLOWED_ACTOR = "dashboard"
VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    pass


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


class Settings:
    """Réglages en lecture seule (aucune méthode de modification)."""

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]):
        object.__setattr__(self, "_data", _freeze(copy.deepcopy(_thaw(data))))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Les réglages sont en lecture seule.")

    def __getitem__(self, section: str) -> Mapping[str, Any]:
        return self._data[section]

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return _thaw(self._data)


def load_defaults(path: Path | None = None) -> dict[str, Any]:
    with open(path or DEFAULTS_FILE, "rb") as handle:
        return tomllib.load(handle)


def _same_type(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list | tuple)
    return isinstance(value, type(default))


def validate_settings(data: Mapping[str, Any]) -> list[str]:
    """Retourne la liste des incohérences (vide si tout va bien)."""
    errors: list[str] = []
    cap = data.get("capital", {})
    numbers = ("total_eur", "reserve_eur", "trading_budget_eur", "fee_reserve_eur",
               "max_position_eur", "max_daily_loss_eur")
    for key in numbers:
        if cap.get(key, 0) < 0:
            errors.append(f"capital.{key} ne peut pas être négatif.")
    if cap.get("max_position_eur", 0) <= 0:
        errors.append("capital.max_position_eur doit être supérieur à 0.")
    total_parts = cap.get("reserve_eur", 0) + cap.get("trading_budget_eur", 0) + cap.get("fee_reserve_eur", 0)
    if total_parts > cap.get("total_eur", 0) + 1e-9:
        errors.append("Réserve + budget de trading + réserve de frais dépassent le capital total.")
    if cap.get("max_position_eur", 0) > cap.get("trading_budget_eur", 0):
        errors.append("Le montant maximal par position dépasse le budget de trading.")
    if cap.get("max_open_positions", 0) < 1:
        errors.append("capital.max_open_positions doit être au moins 1.")
    if cap.get("max_open_positions", 0) * cap.get("max_position_eur", 0) > cap.get("trading_budget_eur", 0) + 1e-9:
        errors.append("Nombre de positions x montant par position dépasse le budget de trading.")
    if not 1 <= cap.get("max_traders", 0) <= 10:
        errors.append("capital.max_traders doit être compris entre 1 et 10.")

    judge = data.get("judge", {})
    for key in ("max_buy_price_impact_pct", "max_sell_price_impact_pct", "max_total_fees_pct",
                "max_top10_holders_pct"):
        if not 0 < judge.get(key, 0) <= 100:
            errors.append(f"judge.{key} doit être compris entre 0 et 100.")
    if not 0 <= judge.get("min_trader_score", 0) <= 100:
        errors.append("judge.min_trader_score doit être compris entre 0 et 100.")
    for key in ("max_source_age_seconds", "max_report_age_seconds", "wait_timeout_seconds"):
        if judge.get(key, 0) <= 0:
            errors.append(f"judge.{key} doit être supérieur à 0.")

    ex = data.get("exit", {})
    if not 0 < ex.get("stop_loss_pct", 0) < 100:
        errors.append("exit.stop_loss_pct doit être compris entre 0 et 100.")
    if ex.get("take_profit_pct", 0) <= 0:
        errors.append("exit.take_profit_pct doit être supérieur à 0.")

    exe = data.get("execution", {})
    if not 1 <= exe.get("slippage_bps", 0) <= 1000:
        errors.append("execution.slippage_bps doit être compris entre 1 et 1000 (10 %).")
    if exe.get("max_priority_fee_lamports", -1) < 0:
        errors.append("execution.max_priority_fee_lamports ne peut pas être négatif.")

    sch = data.get("schedule", {})
    for key in ("start", "stop"):
        if not _TIME_RE.match(str(sch.get(key, ""))):
            errors.append(f"schedule.{key} doit être au format HH:MM.")
    days = sch.get("days", [])
    if not days or any(d not in VALID_DAYS for d in days):
        errors.append("schedule.days doit contenir des jours parmi " + ", ".join(VALID_DAYS) + ".")
    try:
        ZoneInfo(str(sch.get("timezone", "")))
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("schedule.timezone n'est pas un fuseau horaire valide (ex. Europe/Paris).")
    return errors


def merge_overrides(defaults: Mapping[str, Any], overrides: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    merged = copy.deepcopy(_thaw(defaults))
    for section, values in overrides.items():
        if section not in EDITABLE_SECTIONS or section not in merged:
            continue
        for key, value in values.items():
            if key in merged[section] and _same_type(merged[section][key], value):
                merged[section][key] = value
    return merged


def load_settings(db: Database | None = None, defaults_path: Path | None = None) -> Settings:
    defaults = load_defaults(defaults_path)
    merged = merge_overrides(defaults, db.get_setting_overrides() if db is not None else {})
    errors = validate_settings(merged)
    if errors:
        # Des réglages incohérents en base ne doivent jamais être utilisés : retour aux valeurs d'origine.
        defaults_errors = validate_settings(defaults)
        if defaults_errors:
            raise SettingsError("defaults.toml est incohérent : " + " ; ".join(defaults_errors))
        return Settings(defaults)
    return Settings(merged)


def apply_user_change(db: Database, section: str, key: str, value: Any, *, actor: str,
                      defaults_path: Path | None = None) -> Settings:
    """Modifie un réglage. Réservé à l'application (actor="dashboard")."""
    if actor != ALLOWED_ACTOR:
        raise PermissionError("Seule l'application (tableau de bord) peut modifier les réglages.")
    if section not in EDITABLE_SECTIONS:
        raise SettingsError(f"La section '{section}' n'est pas modifiable.")
    defaults = load_defaults(defaults_path)
    if key not in defaults[section]:
        raise SettingsError(f"Réglage inconnu : {section}.{key}")
    if not _same_type(defaults[section][key], value):
        raise SettingsError(f"Type invalide pour {section}.{key}.")
    if isinstance(defaults[section][key], float):
        value = float(value)
    if isinstance(value, tuple):
        value = list(value)

    current = merge_overrides(defaults, db.get_setting_overrides())
    current[section][key] = value
    errors = validate_settings(current)
    if errors:
        raise SettingsError(" ; ".join(errors))
    db.put_setting_override(section, key, value, actor=actor)
    return Settings(current)
