"""WHALE : gros mouvements de capitaux autour d'un token.

Examine les dernières transactions du pool principal, le flux d'achats/ventes (DexScreener)
et les achats simultanés de plusieurs traders suivis. WHALE ne déclenche jamais un achat :
il ne fait que signaler (OK / WARN) ou s'opposer (BLOCK).

Approximation : dans chaque transaction du pool, le trader est supposé être le payeur des frais.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agents.base import BLOCK, OK, WARN, AgentReport, guarded, unavailable
from data.models import TokenSnapshot
from data.swap_parser import SOL_MINT, parse_swap


class WhaleAgent:
    name = "WHALE"

    def __init__(self, rpc: Any, db: Any, settings: Any, *, timeout: float = 20.0):
        self.rpc = rpc
        self.db = db
        agents = settings["agents"]
        self.sample = int(agents["whale_sample_size"])
        self.big_pct = float(agents["whale_big_trade_pct_liquidity"])
        self.group_window = float(agents["whale_group_window_minutes"]) * 60
        self.usdc_mint = settings["tokens"]["usdc_mint"]
        self.timeout = timeout

    async def analyze(self, mint: str, snapshot: TokenSnapshot, sol_usd: float | None,
                      now: datetime | None = None) -> AgentReport:
        return await guarded(self.name, mint, self._analyze(mint, snapshot, sol_usd, now), self.timeout)

    async def _pool_swaps(self, pool: str, mint: str) -> list[Any]:
        signatures = await self.rpc.get_signatures_for_address(pool, limit=self.sample)
        semaphore = asyncio.Semaphore(4)

        async def load(signature: str) -> Any:
            async with semaphore:
                tx = await self.rpc.get_transaction(signature)
            if not tx:
                return None
            keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
            if not keys:
                return None
            payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else str(keys[0])
            event = parse_swap(tx, payer, usdc_mint=self.usdc_mint)
            if event and event.token_mint == mint and event.side in ("BUY", "SELL") and event.quote_mint == SOL_MINT:
                return event
            return None

        results = await asyncio.gather(*(load(s["signature"]) for s in signatures if s.get("err") is None))
        return [r for r in results if r is not None]

    async def _analyze(self, mint: str, snap: TokenSnapshot, sol_usd: float | None,
                       now: datetime | None) -> AgentReport:
        pair = snap.best_pair
        if pair is None or pair.liquidity_usd is None:
            return unavailable(self.name, mint, "Pool principal inconnu : flux impossibles à analyser.")
        if sol_usd is None or sol_usd <= 0:
            return unavailable(self.name, mint, "Prix du SOL indisponible.")
        current = (now or datetime.now(UTC)).timestamp()
        big_threshold = pair.liquidity_usd * self.big_pct / 100

        swaps = await self._pool_swaps(pair.pair_address, mint)
        big_buys = big_sells = 0.0
        small_buys = small_sells = 0
        largest = 0.0
        for event in swaps:
            usd = (event.quote_amount or 0) * sol_usd
            largest = max(largest, usd)
            if usd >= big_threshold:
                if event.side == "BUY":
                    big_buys += usd
                else:
                    big_sells += usd
            elif event.side == "BUY":
                small_buys += 1
            else:
                small_sells += 1

        followed = {t["address"] for t in self.db.list_traders(active_only=True)}
        recent = self.db.source_events_for_mint(mint, int(current - self.group_window), side="BUY")
        group = sorted({e["wallet"] for e in recent if e["wallet"] in followed})

        reasons: list[str] = []
        warnings: list[str] = []
        if big_sells >= big_threshold and big_sells >= 2 * max(big_buys, 1.0) and small_buys > small_sells:
            reasons.append(f"gros vendeurs ({big_sells:,.0f} $) pendant que les petits achètent "
                           f"({small_buys} petits achats) : risque de servir de liquidité de sortie")
        sells_m5, buys_m5 = pair.sells_m5 or 0, pair.buys_m5 or 0
        if sells_m5 >= 10 and sells_m5 > 2 * buys_m5:
            warnings.append(f"pression vendeuse sur 5 min ({sells_m5} ventes contre {buys_m5} achats)")
        if largest >= pair.liquidity_usd * 0.05:
            warnings.append(f"mouvement inhabituel : une transaction de {largest:,.0f} $ "
                            f"(≥ 5 % de la liquidité)")
        if not swaps:
            warnings.append("aucune transaction récente exploitable sur le pool : flux non vérifiés")
        if len(group) >= 2:
            warnings.append(f"achat groupé : {len(group)} traders suivis ont acheté ce token en "
                            f"{self.group_window / 60:.0f} min (signal fort, mais possible coordination)")

        data = {"pool": pair.pair_address, "liquidity_usd": pair.liquidity_usd, "big_threshold_usd": big_threshold,
                "swaps_analysed": len(swaps), "big_buys_usd": big_buys, "big_sells_usd": big_sells,
                "small_buys": small_buys, "small_sells": small_sells, "largest_trade_usd": largest,
                "buys_m5": pair.buys_m5, "sells_m5": pair.sells_m5, "buys_h1": pair.buys_h1, "sells_h1": pair.sells_h1,
                "group_buyers": group}
        confidence = min(1.0, len(swaps) / max(self.sample / 2, 1))
        if reasons:
            return AgentReport(self.name, mint, BLOCK, "Flux défavorables : " + reasons[0], reasons + warnings,
                               data, confidence)
        if warnings:
            return AgentReport(self.name, mint, WARN, f"{len(warnings)} point(s) d'attention sur les flux.",
                               warnings, data, confidence)
        return AgentReport(self.name, mint, OK, f"Flux normaux ({len(swaps)} transactions récentes analysées).",
                           [], data, confidence)
