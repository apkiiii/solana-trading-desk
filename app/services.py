"""Actions lancées depuis l'application (exécutées en arrière-plan, jamais dans la fenêtre)."""
from __future__ import annotations

import asyncio
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


def evaluate_trader(db: Database, wallet: str) -> AgentReport:
    async def job(settings, providers):
        head = HeadAgent(providers.rpc, db, settings, usdc_mint=settings["tokens"]["usdc_mint"])
        report = await head.evaluate(wallet)
        save_report(db, report)
        return report

    return asyncio.run(_with_providers(db, job))


def analyze_token(db: Database, mint: str) -> dict[str, Any]:
    """RISK + WHALE sur un token, pour le montant maximal d'une position."""
    async def job(settings, providers):
        tokens = settings["tokens"]
        market = MarketData(providers.rpc, providers.jupiter, providers.dex, providers.rug, tokens)
        eur_per_sol, reason = await market.eur_per_sol()
        snapshot = await market.snapshot(mint, use_cache=False)
        timeout = float(settings["agents"]["agent_timeout_seconds"])
        results: dict[str, Any] = {"eur_per_sol": eur_per_sol, "eur_reason": reason, "reports": []}
        if eur_per_sol is None:
            return results
        lamports = int(float(settings["capital"]["max_position_eur"]) / eur_per_sol * 1e9)
        prices = await providers.jupiter.get_prices([tokens["sol_mint"]])
        sol_usd = prices[tokens["sol_mint"]].usd_price if tokens["sol_mint"] in prices else None
        risk, whale = await asyncio.gather(
            RiskAgent(providers.jupiter, settings, timeout=timeout).analyze(mint, lamports, snapshot),
            WhaleAgent(providers.rpc, db, settings, timeout=timeout).analyze(mint, snapshot, sol_usd))
        for report in (risk, whale):
            save_report(db, report)
        results["reports"] = [risk, whale]
        results["amount_eur"] = float(settings["capital"]["max_position_eur"])
        return results

    return asyncio.run(_with_providers(db, job))


def format_report_html(report: AgentReport, colors: dict[str, str]) -> str:
    color = colors.get(report.status, "#8b98a9")
    items = "".join(f"<li>{r}</li>" for r in report.reasons)
    checks = report.data.get("checks") or []
    rows = "".join(
        f"<li>{'✅' if c['passed'] else '❌' if c['passed'] is False else '❔'} <b>{c['name']}</b> : {c['detail']}</li>"
        for c in checks)
    return (f"<h3 style='margin-bottom:2px'>{report.agent} — <span style='color:{color}'>{report.status}</span></h3>"
            f"<p>{report.summary}</p>" + (f"<ul>{items}</ul>" if items and not rows else "") +
            (f"<ul>{rows}</ul>" if rows else ""))
