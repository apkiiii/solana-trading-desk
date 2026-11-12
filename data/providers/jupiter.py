"""Jupiter : prix (Price API V3) et devis de swap (Swap API V2 /order).

Envoi des transactions signées : /swap/v2/execute.
Documentation : https://developers.jup.ag/docs/swap
"""
from __future__ import annotations

from typing import Any

from data.models import PriceInfo, SwapQuote
from data.providers.base import HttpProvider, ProviderError


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def parse_order(data: dict[str, Any]) -> SwapQuote:
    impact = _float(data.get("priceImpact"))
    if impact is None and data.get("priceImpactPct") is not None:
        legacy = _float(data.get("priceImpactPct"))
        impact = legacy * 100 if legacy is not None else None
    platform = data.get("platformFee") or {}
    error_message = data.get("errorMessage") or data.get("error")
    error_code = _int(data.get("errorCode"))
    if error_code is None and error_message:
        error_code = -1
    return SwapQuote(
        input_mint=str(data.get("inputMint", "")),
        output_mint=str(data.get("outputMint", "")),
        in_amount_raw=_int(data.get("inAmount")) or 0,
        out_amount_raw=_int(data.get("outAmount")) or 0,
        min_out_amount_raw=_int(data.get("otherAmountThreshold")),
        price_impact_pct=abs(impact) if impact is not None else None,
        slippage_bps=_int(data.get("slippageBps")),
        fee_bps=_int(data.get("feeBps")),
        platform_fee_bps=_int(platform.get("feeBps")) if isinstance(platform, dict) else None,
        router=data.get("router"),
        request_id=data.get("requestId"),
        transaction_b64=data.get("transaction") or None,
        signature_fee_lamports=_int(data.get("signatureFeeLamports")),
        prioritization_fee_lamports=_int(data.get("prioritizationFeeLamports")),
        rent_fee_lamports=_int(data.get("rentFeeLamports")),
        gasless=data.get("gasless") if isinstance(data.get("gasless"), bool) else None,
        in_usd=_float(data.get("inUsdValue")),
        out_usd=_float(data.get("outUsdValue")),
        error_code=error_code,
        error_message=error_message,
        raw=data,
    )


class JupiterClient(HttpProvider):
    name = "jupiter"

    def __init__(self, base_url: str, *, api_key: str = "", **kwargs: Any):
        headers = {"x-api-key": api_key} if api_key else {}
        super().__init__(base_url, headers=headers, **kwargs)

    async def get_prices(self, mints: list[str]) -> dict[str, PriceInfo]:
        """Prix en USD (50 tokens max par appel). Un token absent de la réponse n'a pas de prix fiable."""
        if not mints:
            return {}
        if len(mints) > 50:
            raise ValueError("50 tokens maximum par appel")
        data = await self._request("GET", "/price/v3", params={"ids": ",".join(mints)})
        if not isinstance(data, dict):
            raise ProviderError(self.name, "réponse de prix inattendue")
        prices: dict[str, PriceInfo] = {}
        for mint, item in data.items():
            if not isinstance(item, dict):
                continue
            usd = _float(item.get("usdPrice"))
            if usd is None or usd <= 0:
                continue
            prices[mint] = PriceInfo(mint=mint, usd_price=usd, block_id=_int(item.get("blockId")),
                                     decimals=_int(item.get("decimals")),
                                     liquidity_usd=_float(item.get("liquidity")),
                                     price_change_24h=_float(item.get("priceChange24h")))
        return prices

    async def get_order(self, input_mint: str, output_mint: str, amount_raw: int, *, taker: str | None = None,
                        slippage_bps: int | None = None, priority_fee_lamports: int | None = None) -> SwapQuote:
        """Devis (et transaction à signer si ``taker`` est fourni)."""
        if amount_raw <= 0:
            raise ValueError("amount_raw doit être > 0")
        params: dict[str, Any] = {"inputMint": input_mint, "outputMint": output_mint,
                                  "amount": str(amount_raw), "swapMode": "ExactIn"}
        if taker:
            params["taker"] = taker
        if slippage_bps is not None:
            params["slippageBps"] = str(slippage_bps)
        if priority_fee_lamports is not None:
            params["priorityFeeLamports"] = str(priority_fee_lamports)
            params["broadcastFeeType"] = "maxCap"
        data = await self._request("GET", "/swap/v2/order", params=params)
        if not isinstance(data, dict):
            raise ProviderError(self.name, "réponse de devis inattendue")
        return parse_order(data)

    async def execute(self, signed_transaction_b64: str, request_id: str) -> dict[str, Any]:
        """Envoie une transaction signée (issue de /order). Renvoyer la même transaction est sans risque :
        Jupiter ne l'exécute jamais deux fois (même signature)."""
        data = await self._request("POST", "/swap/v2/execute",
                                   json={"signedTransaction": signed_transaction_b64, "requestId": request_id})
        if not isinstance(data, dict):
            raise ProviderError(self.name, "réponse d'exécution inattendue")
        return {"status": data.get("status"), "signature": data.get("signature"), "code": _int(data.get("code")),
                "input_amount": _int(data.get("inputAmountResult")),
                "output_amount": _int(data.get("outputAmountResult")), "error": data.get("error")}
