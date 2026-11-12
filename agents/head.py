"""HEAD : évalue et classe les traders suivis à partir de leur historique on-chain.

L'évaluation complète (plusieurs centaines de transactions) est lente : elle est faite à
l'avance et mise en cache. Au moment d'un achat, ``cached_report`` lit le dernier score en
base, sans appel réseau.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from agents.analytics import compute_trader_stats, score_trader
from agents.base import BLOCK, OK, UNAVAILABLE, WARN, AgentReport, guarded, unavailable
from data.swap_parser import parse_swap


class HeadAgent:
    name = "HEAD"

    def __init__(self, rpc: Any, db: Any, settings: Any, *, usdc_mint: str):
        self.rpc = rpc
        self.db = db
        self.usdc_mint = usdc_mint
        agents = settings["agents"]
        judge = settings["judge"]
        self.max_transactions = int(agents["head_max_transactions"])
        self.cache_hours = float(agents["head_cache_hours"])
        self.min_score = float(judge["min_trader_score"])
        self.min_closed = int(judge["min_trader_closed_trades"])
        self.min_days = float(judge["min_trader_history_days"])

    # ---------------------------------------------------------------- évaluation
    async def evaluate(self, wallet: str, *, timeout: float = 900.0) -> AgentReport:
        return await guarded(self.name, wallet, self._evaluate(wallet), timeout)

    async def _fetch_events(self, wallet: str) -> list[Any]:
        signatures: list[dict[str, Any]] = []
        before: str | None = None
        while len(signatures) < self.max_transactions:
            page = await self.rpc.get_signatures_for_address(
                wallet, limit=min(1000, self.max_transactions - len(signatures)), before=before)
            if not page:
                break
            signatures.extend(page)
            before = page[-1]["signature"]
            if len(page) < 2:
                break
        semaphore = asyncio.Semaphore(4)

        async def load(signature: str) -> Any:
            async with semaphore:
                tx = await self.rpc.get_transaction(signature)
            return parse_swap(tx, wallet, usdc_mint=self.usdc_mint)

        results = await asyncio.gather(*(load(s["signature"]) for s in signatures if s.get("err") is None))
        return [e for e in results if e is not None]

    async def _evaluate(self, wallet: str) -> AgentReport:
        events = await self._fetch_events(wallet)
        stats = compute_trader_stats(events)
        score, notes = score_trader(stats, min_closed_trades=self.min_closed, min_history_days=self.min_days)
        payload = asdict(stats)
        payload.update({"score": score, "notes": notes, "evaluated_at": datetime.now(UTC).isoformat(),
                        "transactions_analysed": len(events)})
        self.db.update_trader_score(wallet, score, payload)
        return self._report(wallet, score, payload)

    # -------------------------------------------------------------- lecture cache
    def _report(self, wallet: str, score: float | None, payload: dict[str, Any]) -> AgentReport:
        notes = list(payload.get("notes", []))
        closed = payload.get("closed_trades", 0) or 0
        win_rate = payload.get("win_rate")
        summary = (f"Score {score:.0f}/100 — {closed} trades clôturés, réussite "
                   f"{(win_rate or 0) * 100:.0f} %, gain réalisé {payload.get('realized_pnl_sol', 0):+.3f} SOL, "
                   f"drawdown {payload.get('max_drawdown_pct') or 0:.0f} %")
        confidence = min(1.0, closed / self.min_closed) if self.min_closed else 1.0
        if score is None:
            return unavailable(self.name, wallet, "Trader jamais évalué.")
        if score < self.min_score:
            reasons = [f"score {score:.0f} inférieur au minimum {self.min_score:.0f}"] + notes
            return AgentReport(self.name, wallet, BLOCK, summary, reasons, payload, confidence)
        return AgentReport(self.name, wallet, WARN if notes else OK, summary, notes, payload, confidence)

    def cached_report(self, wallet: str, now: datetime | None = None) -> AgentReport:
        """Dernière évaluation en base (aucun appel réseau). UNAVAILABLE si absente ou trop ancienne."""
        trader = next((t for t in self.db.list_traders() if t["address"] == wallet), None)
        if trader is None:
            return unavailable(self.name, wallet, "Wallet non suivi.")
        stats = trader.get("stats") or {}
        evaluated_at = stats.get("evaluated_at")
        if trader.get("score") is None or not evaluated_at:
            return unavailable(self.name, wallet, "Trader pas encore évalué par HEAD.")
        age_hours = ((now or datetime.now(UTC)) - datetime.fromisoformat(evaluated_at)).total_seconds() / 3600
        if age_hours > self.cache_hours * 4:
            report = unavailable(self.name, wallet, f"Évaluation trop ancienne ({age_hours:.0f} h).")
            report.data = stats
            return report
        return self._report(wallet, float(trader["score"]), stats)

    def needs_refresh(self, wallet: str, now: datetime | None = None) -> bool:
        report = self.cached_report(wallet, now)
        if report.status == UNAVAILABLE:
            return True
        evaluated_at = datetime.fromisoformat(report.data["evaluated_at"])
        return ((now or datetime.now(UTC)) - evaluated_at).total_seconds() > self.cache_hours * 3600

    def ranking(self) -> list[dict[str, Any]]:
        """Traders classés du meilleur au moins bon score (les non évalués en dernier)."""
        traders = self.db.list_traders()
        return sorted(traders, key=lambda t: (t["score"] is None, -(t["score"] or 0)))
