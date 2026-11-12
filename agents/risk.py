"""RISK : le token est-il achetable ET revendable sans danger excessif ?

Contrôles : liquidité, autorités de création et de gel, extensions Token-2022 dangereuses,
frais de transfert, signaux RugCheck, concentration des détenteurs, âge du token, impact prix
à l'achat, et surtout un **devis de revente** (si Jupiter ne trouve pas de route pour revendre,
le token est traité comme invendable / honeypot).

Règle : un contrôle sans donnée = UNAVAILABLE (l'achat sera refusé), un contrôle échoué = BLOCK.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agents.base import BLOCK, OK, UNAVAILABLE, AgentReport, guarded
from data.models import TokenSnapshot

DANGEROUS_EXTENSIONS = {
    "transferHook": "programme exécuté à chaque transfert (peut bloquer la revente)",
    "permanentDelegate": "un tiers peut déplacer ou brûler tes tokens",
    "nonTransferable": "token non transférable",
    "defaultAccountState": "comptes gelés par défaut",
    "pausableConfig": "transferts suspendables par le créateur",
    "pausable": "transferts suspendables par le créateur",
}


class RiskAgent:
    name = "RISK"

    def __init__(self, jupiter: Any, settings: Any, *, timeout: float = 20.0):
        self.jupiter = jupiter
        judge = settings["judge"]
        self.min_liquidity = float(judge["min_liquidity_usd"])
        self.max_top10 = float(judge["max_top10_holders_pct"])
        self.min_age_minutes = float(judge["min_token_age_minutes"])
        self.max_buy_impact = float(judge["max_buy_price_impact_pct"])
        self.max_sell_impact = float(judge["max_sell_price_impact_pct"])
        self.slippage_bps = int(settings["execution"]["slippage_bps"])
        self.sol_mint = settings["tokens"]["sol_mint"]
        self.timeout = timeout

    async def analyze(self, mint: str, amount_lamports: int, snapshot: TokenSnapshot,
                      now: datetime | None = None) -> AgentReport:
        return await guarded(self.name, mint, self._analyze(mint, amount_lamports, snapshot, now), self.timeout)

    async def _analyze(self, mint: str, amount_lamports: int, snap: TokenSnapshot,
                       now: datetime | None) -> AgentReport:
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool | None, detail: str) -> None:
            checks.append({"name": name, "passed": passed, "detail": detail})

        # --- liquidité et âge (DexScreener)
        pair = snap.best_pair
        liquidity = pair.liquidity_usd if pair else None
        check("liquidité", None if liquidity is None else liquidity >= self.min_liquidity,
              "inconnue" if liquidity is None else f"{liquidity:,.0f} $ (minimum {self.min_liquidity:,.0f} $)")
        created = pair.pair_created_at if pair else None
        if created is None:
            check("âge du token", None, "date de création du pool inconnue")
        else:
            age_min = ((now or datetime.now(UTC)) - created).total_seconds() / 60
            check("âge du token", age_min >= self.min_age_minutes,
                  f"{age_min:.0f} min (minimum {self.min_age_minutes:.0f} min)")

        # --- compte du token (on-chain, prioritaire sur RugCheck)
        info = snap.mint_info
        if info is None:
            check("autorité de création", None, "compte du token illisible")
            check("autorité de gel", None, "compte du token illisible")
            check("extensions Token-2022", None, "compte du token illisible")
        else:
            check("autorité de création", info.mint_authority is None,
                  "révoquée" if info.mint_authority is None else "active : le créateur peut imprimer des tokens")
            check("autorité de gel", info.freeze_authority is None,
                  "révoquée" if info.freeze_authority is None else "active : le créateur peut geler tes tokens")
            dangers = [f"{ext} ({DANGEROUS_EXTENSIONS[ext]})" for ext in info.extensions if ext in DANGEROUS_EXTENSIONS]
            if info.transfer_fee_bps:
                dangers.append(f"frais de transfert de {info.transfer_fee_bps / 100:.2f} %")
            check("extensions Token-2022", not dangers, "aucune" if not dangers else "; ".join(dangers))

        # --- RugCheck
        rug = snap.rug
        if rug is None:
            check("rapport RugCheck", None, "indisponible")
            check("concentration des détenteurs", None, "inconnue")
        else:
            flags = [r.name for r in rug.danger_risks]
            if rug.rugged:
                flags.insert(0, "token signalé comme « rugged »")
            if rug.rugged is None and not rug.risks:
                check("rapport RugCheck", None, "rapport vide")
            else:
                check("rapport RugCheck", not flags, "aucun risque « danger »" if not flags else "; ".join(flags))
            top = rug.top10_holders_pct
            check("concentration des détenteurs", None if top is None else top <= self.max_top10,
                  "inconnue" if top is None else f"top 10 = {top:.1f} % (maximum {self.max_top10:.0f} %)")

        # --- devis d'achat et de revente (Jupiter)
        buy_quote = sell_quote = None
        try:
            buy_quote = await self.jupiter.get_order(self.sol_mint, mint, amount_lamports)
        except Exception as exc:  # noqa: BLE001
            check("devis d'achat", None, f"indisponible ({type(exc).__name__})")
        if buy_quote is not None:
            if not buy_quote.ok:
                check("devis d'achat", False, f"aucune route d'achat ({buy_quote.error_message or 'erreur'})")
            elif buy_quote.price_impact_pct is None:
                check("impact prix à l'achat", None, "non communiqué")
            else:
                check("impact prix à l'achat", buy_quote.price_impact_pct <= self.max_buy_impact,
                      f"{buy_quote.price_impact_pct:.2f} % (maximum {self.max_buy_impact:.1f} %)")
            if buy_quote.ok:
                try:
                    sell_quote = await self.jupiter.get_order(mint, self.sol_mint, buy_quote.out_amount_raw)
                except Exception as exc:  # noqa: BLE001
                    check("revente possible", None, f"devis de revente indisponible ({type(exc).__name__})")
        if sell_quote is not None:
            if not sell_quote.ok:
                check("revente possible", False, "aucune route de revente : token potentiellement invendable")
            else:
                check("revente possible", True, f"revente estimée {sell_quote.out_amount_raw / 1e9:.5f} SOL")
                impact = sell_quote.price_impact_pct
                check("impact prix à la revente", None if impact is None else impact <= self.max_sell_impact,
                      "non communiqué" if impact is None else f"{impact:.2f} % (maximum {self.max_sell_impact:.1f} %)")

        round_trip_loss = None
        if buy_quote is not None and buy_quote.ok and sell_quote is not None and sell_quote.ok and amount_lamports:
            round_trip_loss = (1 - sell_quote.out_amount_raw / amount_lamports) * 100
        data = {
            "mint": mint, "amount_lamports": amount_lamports, "checks": checks,
            "liquidity_usd": liquidity,
            "buy_quote": _quote_summary(buy_quote), "sell_quote": _quote_summary(sell_quote),
            "round_trip_loss_pct": round_trip_loss,
            "slippage_estimate_pct": (buy_quote.price_impact_pct or 0) + self.slippage_bps / 100
            if buy_quote is not None and buy_quote.ok else None,
            "snapshot_errors": dict(snap.errors),
        }
        failed = [c for c in checks if c["passed"] is False]
        missing = [c for c in checks if c["passed"] is None]
        known = len(checks) - len(missing)
        confidence = known / len(checks) if checks else 0.0
        if failed:
            reasons = [f"{c['name']} : {c['detail']}" for c in failed]
            return AgentReport(self.name, mint, BLOCK, f"Token refusé : {len(failed)} contrôle(s) échoué(s).",
                               reasons, data, confidence)
        if missing:
            reasons = [f"{c['name']} : {c['detail']}" for c in missing]
            return AgentReport(self.name, mint, UNAVAILABLE,
                               f"Informations insuffisantes : {len(missing)} contrôle(s) sans donnée.",
                               reasons, data, confidence)
        summary = f"Token acceptable : {len(checks)} contrôles réussis"
        if round_trip_loss is not None:
            summary += f", coût aller-retour estimé {round_trip_loss:.2f} %"
        return AgentReport(self.name, mint, OK, summary + ".", [], data, confidence)


def _quote_summary(quote: Any) -> dict[str, Any] | None:
    if quote is None:
        return None
    return {"ok": quote.ok, "in_amount_raw": quote.in_amount_raw, "out_amount_raw": quote.out_amount_raw,
            "price_impact_pct": quote.price_impact_pct, "fee_bps": quote.fee_bps, "router": quote.router,
            "prioritization_fee_lamports": quote.prioritization_fee_lamports,
            "signature_fee_lamports": quote.signature_fee_lamports, "error": quote.error_message}
