"""Planning des nouvelles entrées (fuseau horaire, jours, passage de minuit, changements d'heure).

Le planning n'autorise ou n'interdit que les NOUVEAUX achats. Les sorties et la protection
des positions ouvertes ne dépendent jamais du planning.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def entries_window_open(schedule: Mapping[str, Any], now: datetime) -> bool:
    """Vrai si les nouvelles entrées sont autorisées à l'instant ``now`` (datetime avec fuseau)."""
    if now.tzinfo is None:
        raise ValueError("now doit avoir un fuseau horaire")
    if not schedule.get("enabled", True):
        return True
    local = now.astimezone(ZoneInfo(schedule["timezone"]))
    start, stop = _parse(schedule["start"]), _parse(schedule["stop"])
    days = set(schedule["days"])
    today = DAYS[local.weekday()]
    yesterday = DAYS[(local - timedelta(days=1)).weekday()]
    current = local.time()
    if start == stop:
        return today in days
    if start < stop:
        return today in days and start <= current < stop
    # La plage passe minuit (ex. 20:00 -> 02:00)
    return (today in days and current >= start) or (yesterday in days and current < stop)


def next_change(schedule: Mapping[str, Any], now: datetime, horizon_hours: int = 24 * 8) -> datetime | None:
    """Prochain changement d'état (ouverture/fermeture), à la minute près. None si aucun dans l'horizon."""
    state = entries_window_open(schedule, now)
    probe = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(horizon_hours * 60):
        if entries_window_open(schedule, probe) != state:
            return probe
        probe += timedelta(minutes=1)
    return None
