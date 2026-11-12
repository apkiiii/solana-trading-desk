"""Actions déclenchées depuis l'interface (asynchrones, exécutées par le serveur local)."""
from __future__ import annotations

import asyncio
import contextlib
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.base import AgentReport, save_report
from agents.head import HeadAgent
from agents.risk import RiskAgent
from agents.whale import WhaleAgent
from config.settings import load_settings
from data.database import Database
from data.market_data import MarketData
from data.providers.factory import build_providers
from security.secrets import load_secrets


async def _with_providers(db: Database, job) -> Any:
    settings = load_settings(db)
    providers = build_providers(settings, load_secrets(), db)
    try:
        return await job(settings, providers)
    finally:
        await providers.aclose()


async def evaluate_trader(db: Database, wallet: str) -> AgentReport:
    async def job(settings, providers):
        head = HeadAgent(providers.rpc, db, settings, usdc_mint=settings["tokens"]["usdc_mint"])
        report = await head.evaluate(wallet)
        save_report(db, report)
        return report

    return await _with_providers(db, job)


async def analyze_token(db: Database, mint: str) -> dict[str, Any]:
    """RISK + WHALE sur un token, pour le montant maximal d'une position."""
    async def job(settings, providers):
        tokens = settings["tokens"]
        market = MarketData(providers.rpc, providers.jupiter, providers.dex, providers.rug, tokens)
        eur_per_sol, reason = await market.eur_per_sol()
        result: dict[str, Any] = {"eur_per_sol": eur_per_sol, "eur_reason": reason, "reports": []}
        if eur_per_sol is None:
            return result
        snapshot = await market.snapshot(mint, use_cache=False)
        timeout = float(settings["agents"]["agent_timeout_seconds"])
        amount_eur = float(settings["capital"]["max_position_eur"])
        lamports = int(amount_eur / eur_per_sol * 1e9)
        prices = await providers.jupiter.get_prices([tokens["sol_mint"]])
        sol_usd = prices[tokens["sol_mint"]].usd_price if tokens["sol_mint"] in prices else None
        risk, whale = await asyncio.gather(
            RiskAgent(providers.jupiter, settings, timeout=timeout).analyze(mint, lamports, snapshot),
            WhaleAgent(providers.rpc, db, settings, timeout=timeout).analyze(mint, snapshot, sol_usd))
        for report in (risk, whale):
            save_report(db, report)
        result.update({"reports": [risk, whale], "amount_eur": amount_eur,
                       "symbol": None, "price_usd": snapshot.price.usd_price if snapshot.price else None})
        return result

    return await _with_providers(db, job)


async def run_diagnostic(wallet: str | None) -> str:
    from tools import diagnose
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        await diagnose.run(wallet)
    return buffer.getvalue()


def top_opportunities(db: Database, limit: int = 3, hours: int = 24) -> list[dict[str, Any]]:
    """Meilleurs tokens récemment achetés par les traders suivis, d'après les derniers rapports.

    Score = confiance RISK (si OK) + bonus si WHALE OK + score HEAD du trader acheteur.
    Ce classement est informatif : seul le JUDGE peut autoriser un achat.
    """
    since = int((datetime.now(UTC) - timedelta(hours=hours)).timestamp())
    events = db.query_all("SELECT * FROM source_events WHERE side = 'BUY' AND block_time >= ? "
                          "ORDER BY block_time DESC LIMIT 200", (since,))
    scores = {t["address"]: t["score"] for t in db.list_traders()}
    risk_by_mint = {r["ref"]: r for r in reversed(db.latest_agent_reports("RISK", 200))}
    whale_by_mint = {r["ref"]: r for r in reversed(db.latest_agent_reports("WHALE", 200))}
    best: dict[str, dict[str, Any]] = {}
    for event in events:
        mint = event["token_mint"]
        risk, whale = risk_by_mint.get(mint), whale_by_mint.get(mint)
        trader_score = scores.get(event["wallet"]) or 0
        value = trader_score * 0.4
        if risk and risk["status"] == "OK":
            value += 40 * (risk["confidence"] or 0)
        elif risk and risk["status"] in ("BLOCK", "UNAVAILABLE"):
            value -= 50
        if whale and whale["status"] == "OK":
            value += 20
        elif whale and whale["status"] == "BLOCK":
            value -= 50
        item = best.get(mint)
        if item is None or value > item["score"]:
            best[mint] = {"mint": mint, "score": round(value, 1), "wallet": event["wallet"],
                          "trader_score": trader_score, "risk": risk["status"] if risk else None,
                          "whale": whale["status"] if whale else None, "block_time": event["block_time"],
                          "buyers": 1 + (item["buyers"] if item else 0)}
        else:
            item["buyers"] += 1
    ranked = sorted(best.values(), key=lambda o: o["score"], reverse=True)
    return [o for o in ranked if o["score"] > 0][:limit]
