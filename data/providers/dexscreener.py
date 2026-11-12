"""DexScreener : paires, liquidité, volumes (API publique, sans clé, 300 requêtes/min).

Documentation : https://docs.dexscreener.com/api/reference
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from data.models import PairInfo
from data.providers.base import HttpProvider, ProviderError


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_pair(item: dict[str, Any]) -> PairInfo | None:
    try:
        base = item["baseToken"]["address"]
        quote = item["quoteToken"]["address"]
        address = item["pairAddress"]
    except (KeyError, TypeError):
        return None
    txns = item.get("txns") or {}
    volume = item.get("volume") or {}
    change = item.get("priceChange") or {}
    liquidity = item.get("liquidity") or {}
    created_ms = _i(item.get("pairCreatedAt"))
    return PairInfo(
        pair_address=address,
        dex_id=str(item.get("dexId", "")),
        base_mint=base,
        quote_mint=quote,
        price_usd=_f(item.get("priceUsd")),
        price_native=_f(item.get("priceNative")),
        liquidity_usd=_f(liquidity.get("usd")),
        volume_h1=_f(volume.get("h1")),
        volume_h24=_f(volume.get("h24")),
        buys_m5=_i((txns.get("m5") or {}).get("buys")),
        sells_m5=_i((txns.get("m5") or {}).get("sells")),
        buys_h1=_i((txns.get("h1") or {}).get("buys")),
        sells_h1=_i((txns.get("h1") or {}).get("sells")),
        price_change_m5=_f(change.get("m5")),
        price_change_h1=_f(change.get("h1")),
        fdv=_f(item.get("fdv")),
        market_cap=_f(item.get("marketCap")),
        pair_created_at=datetime.fromtimestamp(created_ms / 1000, UTC) if created_ms else None,
    )


class DexScreenerClient(HttpProvider):
    name = "dexscreener"

    async def get_token_pairs(self, mints: list[str]) -> list[PairInfo]:
        if not mints:
            return []
        if len(mints) > 30:
            raise ValueError("30 tokens maximum par appel")
        data = await self._request("GET", f"/tokens/v1/solana/{','.join(mints)}")
        if isinstance(data, dict) and "pairs" in data:  # ancien format
            data = data["pairs"] or []
        if not isinstance(data, list):
            raise ProviderError(self.name, "réponse inattendue")
        pairs = [parse_pair(item) for item in data if isinstance(item, dict)
                 and item.get("chainId", "solana") == "solana"]
        return [p for p in pairs if p is not None]

    async def best_pair(self, mint: str) -> PairInfo | None:
        """Pool le plus liquide où ``mint`` est le token de base. None si aucun pool connu."""
        pairs = [p for p in await self.get_token_pairs([mint]) if p.base_mint == mint]
        pairs = [p for p in pairs if p.liquidity_usd is not None]
        return max(pairs, key=lambda p: p.liquidity_usd or 0.0) if pairs else None
