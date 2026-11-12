"""Comptes du bot : positions ouvertes, montant investi, pertes du jour, solde du compte démo."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo


def day_start_utc(now: datetime, timezone: str) -> datetime:
    """Minuit (heure locale du planning) du jour en cours, exprimé en UTC."""
    local = now.astimezone(ZoneInfo(timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def book_mode(control_mode: str) -> str:
    """Compte sur lequel sont enregistrées les positions : réel (LIVE) ou démo (PAPER)."""
    return "LIVE" if control_mode == "LIVE" else "PAPER"


class Portfolio:
    def __init__(self, db: Any):
        self.db = db
        self.unrealized_eur: dict[int, float] = {}   # position -> gain/perte latent (dernier passage d'EXIT)

    def snapshot(self, mode: str, settings: Any, now: datetime) -> dict[str, Any]:
        positions = self.db.open_positions(mode)
        invested = sum(float(p["cost_eur"] or 0) for p in positions)
        realized_today = self.db.realized_since(mode, day_start_utc(now, settings["schedule"]["timezone"]))
        latent = sum(min(0.0, self.unrealized_eur.get(int(p["id"]), 0.0)) for p in positions)
        return {"positions": positions, "count": len(positions), "invested_eur": invested,
                "open_mints": frozenset(p["token_mint"] for p in positions),
                "realized_today_eur": realized_today,
                "daily_loss_eur": max(0.0, -(realized_today + latent))}

    def demo_cash_eur(self, settings: Any) -> float:
        """Argent fictif disponible : solde de départ + résultats clôturés - montants en cours."""
        row = self.db.query_one("SELECT COALESCE(SUM(realized_pnl_eur), 0) AS p FROM positions WHERE mode = 'PAPER'")
        invested = sum(float(p["cost_eur"] or 0) for p in self.db.open_positions("PAPER"))
        return float(settings["demo"]["start_balance_eur"]) + float(row["p"] if row else 0) - invested
