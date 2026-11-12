"""EXIT : surveille les positions ouvertes et propose les sorties.

Fonctionne indépendamment du bouton STOPPER, du planning et de la limite de perte du jour :
ces protections bloquent les achats, jamais la gestion des positions existantes.

Motifs de sortie : stop-loss, prise de profit, vente du trader copié, durée maximale,
liquidité trop faible, retournement brutal. Signale aussi les transactions non confirmées.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agents.base import OK, UNAVAILABLE, WARN, AgentReport, guarded

PENDING_STATUSES = ("PENDING", "SENT")


class ExitAgent:
    name = "EXIT"

    def __init__(self, jupiter: Any, dex: Any, db: Any, settings: Any, *, timeout: float = 20.0):
        self.jupiter = jupiter
        self.dex = dex
        self.db = db
        ex = settings["exit"]
        self.stop_loss = float(ex["stop_loss_pct"])
        self.take_profit = float(ex["take_profit_pct"])
        self.follow_sells = bool(ex["follow_trader_sells"])
        self.max_hold_hours = float(ex["max_hold_hours"])
        self.min_liquidity = float(ex["min_liquidity_usd"])
        self.trend_drop = float(settings["agents"]["exit_trend_drop_m5_pct"])
        self.confirm_timeout = float(settings["execution"]["confirmation_timeout_seconds"])
        self.timeout = timeout

    async def review(self, now: datetime | None = None) -> list[AgentReport]:
        positions = self.db.open_positions()
        reports: list[AgentReport] = []
        if positions:
            mints = sorted({p["token_mint"] for p in positions})
            try:
                prices = await self.jupiter.get_prices(mints)
            except Exception:  # noqa: BLE001
                prices = {}
            pairs = await asyncio.gather(*(self.dex.best_pair(m) for m in mints), return_exceptions=True)
            pair_by_mint = {m: (p if not isinstance(p, BaseException) else None) for m, p in zip(mints, pairs,
                                                                                                    strict=True)}
            for position in positions:
                reports.append(await guarded(self.name, str(position["id"]),
                                             self._review_one(position, prices.get(position["token_mint"]),
                                                              pair_by_mint.get(position["token_mint"]), now),
                                             self.timeout))
        reports.extend(self.pending_transactions(now))
        return reports

    async def _review_one(self, position: dict[str, Any], price: Any, pair: Any,
                          now: datetime | None) -> AgentReport:
        current = now or datetime.now(UTC)
        mint = position["token_mint"]
        ref = str(position["id"])
        reasons: list[str] = []
        data: dict[str, Any] = {"position_id": position["id"], "token_mint": mint, "action": "HOLD"}

        opened = datetime.fromisoformat(position["opened_at"])
        held_hours = (current - opened).total_seconds() / 3600
        data["held_hours"] = round(held_hours, 2)

        entry = position.get("entry_price_usd")
        pnl_pct = None
        if price is not None and entry:
            pnl_pct = (price.usd_price / entry - 1) * 100
            data.update({"current_price_usd": price.usd_price, "entry_price_usd": entry,
                         "unrealized_pct": round(pnl_pct, 2)})
            if pnl_pct <= -self.stop_loss:
                reasons.append(f"stop-loss : {pnl_pct:.1f} % (seuil -{self.stop_loss:.0f} %)")
            elif pnl_pct >= self.take_profit:
                reasons.append(f"prise de profit : +{pnl_pct:.1f} % (seuil +{self.take_profit:.0f} %)")

        if self.follow_sells and position.get("source_wallet"):
            since = int(opened.timestamp())
            sells = [e for e in self.db.source_events_for_mint(mint, since, side="SELL")
                     if e["wallet"] == position["source_wallet"]]
            if sells:
                reasons.append("le trader copié a vendu ce token")
                data["trader_sell_signature"] = sells[-1]["signature"]

        if held_hours >= self.max_hold_hours:
            reasons.append(f"durée maximale atteinte ({held_hours:.1f} h)")

        if pair is not None:
            data["liquidity_usd"] = pair.liquidity_usd
            data["price_change_m5"] = pair.price_change_m5
            if pair.liquidity_usd is not None and pair.liquidity_usd < self.min_liquidity:
                reasons.append(f"liquidité tombée à {pair.liquidity_usd:,.0f} $ (minimum {self.min_liquidity:,.0f} $)")
            if (pair.price_change_m5 is not None and pair.price_change_m5 <= -self.trend_drop
                    and pnl_pct is not None and pnl_pct < 0):
                reasons.append(f"retournement : {pair.price_change_m5:.1f} % en 5 min, sous le prix d'entrée")

        if reasons:
            data["action"] = "SELL"
            return AgentReport(self.name, ref, WARN, f"Sortie proposée : {reasons[0]}", reasons, data, 1.0)
        if price is None:
            data["action"] = "HOLD"
            return AgentReport(self.name, ref, UNAVAILABLE, "Prix actuel indisponible : stop-loss non vérifiable.",
                               ["prix indisponible"], data, 0.0)
        text = f"{pnl_pct:+.1f} %" if pnl_pct is not None else "résultat inconnu"
        return AgentReport(self.name, ref, OK, f"Position conservée ({text}, {held_hours:.1f} h).", [], data, 1.0)

    def pending_transactions(self, now: datetime | None = None) -> list[AgentReport]:
        current = now or datetime.now(UTC)
        reports = []
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        rows = self.db.query_all(f"SELECT * FROM executions WHERE status IN ({placeholders})", PENDING_STATUSES)
        for row in rows:
            age = (current - datetime.fromisoformat(row["created_at"])).total_seconds()
            if age > self.confirm_timeout:
                reports.append(AgentReport(
                    self.name, row["tx_signature"] or str(row["id"]), WARN,
                    f"Transaction non confirmée depuis {age:.0f} s : à vérifier.",
                    ["transaction non confirmée"], {"execution_id": row["id"], "age_seconds": age,
                                                    "action": "CHECK_TX"}))
        return reports
