"""SCOUT : détecte les achats et ventes des wallets suivis et mesure leur activité.

Il ne juge pas la compétence d'un trader (rôle de HEAD) : une transaction isolée n'est
jamais une preuve de compétence.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agents.base import DUPLICATE, IGNORED, OK, AgentReport, guarded, unavailable
from data.models import SignatureNotice
from data.swap_parser import SOL_MINT, parse_swap


def _short(value: str | None) -> str:
    return f"{value[:4]}…{value[-4:]}" if value else "?"


class ScoutAgent:
    name = "SCOUT"

    def __init__(self, rpc: Any, db: Any, *, usdc_mint: str, timeout: float = 20.0):
        self.rpc = rpc
        self.db = db
        self.usdc_mint = usdc_mint
        self.timeout = timeout

    async def process_notice(self, notice: SignatureNotice, now: datetime | None = None) -> AgentReport:
        return await guarded(self.name, notice.signature, self._process(notice, now), self.timeout)

    async def _process(self, notice: SignatureNotice, now: datetime | None) -> AgentReport:
        if self.db.has_source_event(notice.signature):
            return AgentReport(self.name, notice.signature, DUPLICATE, "Transaction déjà traitée.")
        tx = await self.rpc.get_transaction(notice.signature, critical=True)
        if tx is None:
            return unavailable(self.name, notice.signature, "Transaction pas encore confirmée ou introuvable.")
        event = parse_swap(tx, notice.wallet, usdc_mint=self.usdc_mint)
        if event is None:
            return AgentReport(self.name, notice.signature, IGNORED, "Transaction sans swap (transfert, frais…).")
        if not self.db.insert_source_event(event, backfill=notice.backfill):
            return AgentReport(self.name, notice.signature, DUPLICATE, "Transaction déjà traitée.")

        current = now or datetime.now(UTC)
        age = (current.timestamp() - event.block_time) if event.block_time else None
        unit = "SOL" if event.quote_mint == SOL_MINT else "USDC"
        data = {
            "signature": event.signature, "wallet": event.wallet, "side": event.side, "token_mint": event.token_mint,
            "token_amount_raw": event.token_amount_raw, "token_decimals": event.token_decimals,
            "quote_mint": event.quote_mint, "quote_amount": event.quote_amount, "quote_unit": unit,
            "block_time": event.block_time, "age_seconds": age, "confirmed": True, "backfill": notice.backfill,
            "fee_paid_by_wallet": event.fee_paid_by_wallet, "activity": self.activity(event.wallet, current),
        }
        if event.side == "OTHER":
            return AgentReport(self.name, event.signature, IGNORED,
                               "Échange token contre token : jamais copié.", data=data)
        verb = "a acheté" if event.side == "BUY" else "a vendu"
        age_text = f" (il y a {age:.0f} s)" if age is not None else ""
        summary = (f"{_short(event.wallet)} {verb} {_short(event.token_mint)} pour "
                   f"{event.quote_amount or 0:.4f} {unit}{age_text}")
        return AgentReport(self.name, event.signature, OK, summary, data=data, confidence=1.0)

    def activity(self, wallet: str, now: datetime | None = None) -> dict[str, Any]:
        """Fréquence et régularité observées depuis que le wallet est surveillé."""
        current = (now or datetime.now(UTC)).timestamp()
        events = self.db.source_events_for_wallet(wallet, int(current - 7 * 86400))
        last_24h = [e for e in events if e["block_time"] and e["block_time"] >= current - 86400]
        days = {datetime.fromtimestamp(e["block_time"], UTC).date() for e in events if e["block_time"]}
        return {"swaps_24h": len(last_24h), "swaps_7d": len(events),
                "buys_7d": sum(1 for e in events if e["side"] == "BUY"),
                "sells_7d": sum(1 for e in events if e["side"] == "SELL"),
                "active_days_7d": len(days)}
