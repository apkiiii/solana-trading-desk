"""Données de marché agrégées pour un token (interrogation des sources en parallèle).

Chaque source qui échoue est notée dans ``TokenSnapshot.errors`` : l'agent RISK et le
JUDGE refuseront un achat tant que l'instantané n'est pas complet.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from core.logs import redact
from data.models import TokenSnapshot

EURC_SANE_RANGE = (0.8, 1.6)  # 1 EURC vaut entre 0,80 et 1,60 USD, sinon donnée suspecte


class MarketData:
    def __init__(self, rpc: Any, jupiter: Any, dex: Any, rug: Any, tokens: Mapping[str, str], *,
                 ttl_seconds: float = 15.0, clock: Callable[[], float] = time.monotonic):
        self.rpc, self.jupiter, self.dex, self.rug = rpc, jupiter, dex, rug
        self.sol_mint = tokens["sol_mint"]
        self.eurc_mint = tokens["eurc_mint"]
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, TokenSnapshot]] = {}

    async def eur_per_sol(self) -> tuple[float | None, str]:
        """Prix d'1 SOL en euros (via SOL/USD et EURC/USD de Jupiter). None + raison si indisponible."""
        try:
            prices = await self.jupiter.get_prices([self.sol_mint, self.eurc_mint])
        except Exception as exc:  # noqa: BLE001
            return None, redact(f"prix indisponibles : {exc}")
        sol, eurc = prices.get(self.sol_mint), prices.get(self.eurc_mint)
        if sol is None or eurc is None:
            return None, "prix SOL ou EURC absent de la réponse"
        low, high = EURC_SANE_RANGE
        if not low <= eurc.usd_price <= high:
            return None, f"prix EURC incohérent ({eurc.usd_price:.4f} USD)"
        return sol.usd_price / eurc.usd_price, "ok"

    async def snapshot(self, mint: str, *, use_cache: bool = True) -> TokenSnapshot:
        now = self._clock()
        if use_cache and mint in self._cache and now - self._cache[mint][0] < self._ttl:
            return self._cache[mint][1]

        names = ("price", "pair", "mint", "rug", "holders")
        results = await asyncio.gather(
            self.jupiter.get_prices([mint]),
            self.dex.best_pair(mint),
            self.rpc.get_mint_info(mint),
            self.rug.get_report(mint),
            self.rpc.get_token_largest_accounts(mint),
            return_exceptions=True,
        )
        snap = TokenSnapshot(mint=mint, fetched_at=datetime.now(UTC))
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                snap.errors[name] = redact(f"{type(result).__name__}: {result}")
                continue
            if name == "price":
                snap.price = result.get(mint)
                if snap.price is None:
                    snap.errors[name] = "aucun prix fiable"
            elif name == "pair":
                snap.best_pair = result
                if result is None:
                    snap.errors[name] = "aucun pool connu"
            elif name == "mint":
                snap.mint_info = result
                if result is None:
                    snap.errors[name] = "token introuvable"
            elif name == "rug":
                snap.rug = result
            elif name == "holders":
                snap.largest_accounts = result
        self._cache[mint] = (now, snap)
        return snap
