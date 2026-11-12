"""SNIPER : prépare une proposition d'achat chiffrée. Il n'exécute RIEN.

Ce module n'a accès à aucun client d'exécution ni à aucune clé : il produit seulement une
proposition que le JUDGE acceptera ou refusera.

Taille : montant maximal par position x « conviction » du trader (son achat comparé à son
achat habituel, borné entre 50 % et 100 %), puis plafonné par le budget restant.
Intérêt après frais : le gain moyen des trades gagnants du trader doit dépasser
``sniper_min_edge_ratio`` fois le coût aller-retour estimé.
"""
from __future__ import annotations

from typing import Any

from agents.base import BLOCK, OK, UNAVAILABLE, AgentReport

BASE_FEE_LAMPORTS = 5_000
LAMPORTS = 1_000_000_000


class SniperAgent:
    name = "SNIPER"

    def __init__(self, settings: Any):
        cap = settings["capital"]
        self.max_position = float(cap["max_position_eur"])
        self.trading_budget = float(cap["trading_budget_eur"])
        self.max_open = int(cap["max_open_positions"])
        self.max_fees_pct = float(settings["judge"]["max_total_fees_pct"])
        self.max_priority = int(settings["execution"]["max_priority_fee_lamports"])
        self.slippage_bps = int(settings["execution"]["slippage_bps"])
        agents = settings["agents"]
        self.min_position = float(agents["sniper_min_position_eur"])
        self.min_edge_ratio = float(agents["sniper_min_edge_ratio"])

    def propose(self, *, event: dict[str, Any], head: AgentReport, risk: AgentReport, eur_per_sol: float | None,
                open_positions: int, invested_eur: float) -> AgentReport:
        mint = event.get("token_mint")
        ref = event.get("signature")
        reasons: list[str] = []
        if event.get("side") != "BUY":
            return AgentReport(self.name, ref, BLOCK, "La transaction source n'est pas un achat.",
                               ["seuls les achats sont copiés"])
        for report in (head, risk):
            if report.blocking:
                reasons.append(f"{report.agent} : {report.summary}")
        if reasons:
            return AgentReport(self.name, ref, BLOCK, "Prérequis non remplis : aucune proposition.", reasons)
        if not eur_per_sol or eur_per_sol <= 0:
            return AgentReport(self.name, ref, UNAVAILABLE, "Prix du SOL en euros indisponible.",
                               ["conversion SOL/EUR impossible"], confidence=0.0)
        if open_positions >= self.max_open:
            return AgentReport(self.name, ref, BLOCK, "Nombre maximal de positions atteint.",
                               [f"{open_positions} positions ouvertes (maximum {self.max_open})"])

        # --- taille de la position
        median_buy = head.data.get("median_buy_sol")
        trader_buy = event.get("quote_amount")
        conviction = 1.0
        if median_buy and trader_buy:
            conviction = max(0.5, min(1.0, float(trader_buy) / float(median_buy)))
        remaining = self.trading_budget - invested_eur
        size_eur = round(min(self.max_position * conviction, remaining), 2)
        if size_eur < self.min_position:
            return AgentReport(self.name, ref, BLOCK, "Budget restant insuffisant.",
                               [f"montant possible {size_eur:.2f} € (minimum {self.min_position:.2f} €)"])
        lamports = int(size_eur / eur_per_sol * LAMPORTS)

        # --- coûts estimés (achat + revente)
        buy = risk.data.get("buy_quote") or {}
        sell = risk.data.get("sell_quote") or {}
        fee_bps = (buy.get("fee_bps") or 0) + (sell.get("fee_bps") or 0)
        impact_pct = (buy.get("price_impact_pct") or 0) + (sell.get("price_impact_pct") or 0)
        priority = min(int(buy.get("prioritization_fee_lamports") or self.max_priority), self.max_priority)
        network_lamports = 2 * (BASE_FEE_LAMPORTS + priority)
        network_pct = network_lamports / lamports * 100 if lamports else 100.0
        fees_pct = fee_bps / 100 + network_pct
        round_trip_pct = fees_pct + impact_pct
        fees_eur = size_eur * fees_pct / 100

        avg_win = head.data.get("avg_win_pct")
        data = {
            "mint": mint, "source_signature": ref, "source_wallet": event.get("wallet"),
            "amount_eur": size_eur, "amount_lamports": lamports, "eur_per_sol": eur_per_sol,
            "conviction": round(conviction, 2), "slippage_bps": self.slippage_bps,
            "max_priority_fee_lamports": self.max_priority,
            "estimated_fees_eur": round(fees_eur, 4), "estimated_fees_pct": round(fees_pct, 3),
            "price_impact_pct": round(impact_pct, 3), "round_trip_cost_pct": round(round_trip_pct, 3),
            "breakeven_gain_pct": round(round_trip_pct, 3), "trader_avg_win_pct": avg_win,
            "rent_deposit_note": "environ 0,0015 SOL bloqués à l'ouverture du compte token, rendus à la fermeture",
        }
        if fees_pct > self.max_fees_pct:
            reasons.append(f"frais estimés {fees_pct:.2f} % (maximum {self.max_fees_pct:.1f} %)")
        if avg_win is None:
            reasons.append("gain moyen du trader inconnu : intérêt après frais impossible à vérifier")
        elif avg_win < self.min_edge_ratio * round_trip_pct:
            reasons.append(f"pas intéressant après frais : gain moyen du trader {avg_win:.1f} % "
                           f"< {self.min_edge_ratio:.0f} x coût aller-retour {round_trip_pct:.2f} %")
        if reasons:
            return AgentReport(self.name, ref, BLOCK, "Proposition abandonnée : " + reasons[0], reasons, data)
        summary = (f"Proposition : acheter {mint[:4]}…{mint[-4:]} pour {size_eur:.2f} € "
                   f"(frais ≈ {fees_eur:.3f} €, seuil de rentabilité +{round_trip_pct:.2f} %)")
        return AgentReport(self.name, ref, OK, summary, [], data, confidence=min(head.confidence or 0,
                                                                                 risk.confidence or 0))
