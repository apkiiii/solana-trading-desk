"""RugCheck : rapport de risque d'un token (https://api.rugcheck.xyz).

Le format de réponse peut évoluer : chaque champ est lu prudemment. Un champ absent
devient ``None``, que l'agent RISK traitera comme « information indisponible » (donc blocage).
"""
from __future__ import annotations

from typing import Any

from data.models import RiskItem, RugReport
from data.providers.base import HttpProvider, ProviderError

_MISSING = object()


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _revoked(container: dict[str, Any], key: str) -> bool | None:
    """True si la clé est présente avec la valeur null (autorité révoquée), None si absente."""
    value = container.get(key, _MISSING)
    if value is _MISSING:
        return None
    return value in (None, "")


def parse_report(mint: str, data: dict[str, Any]) -> RugReport:
    token = data.get("token") if isinstance(data.get("token"), dict) else {}
    mint_revoked = _revoked(data, "mintAuthority")
    if mint_revoked is None:
        mint_revoked = _revoked(token, "mintAuthority")
    freeze_revoked = _revoked(data, "freezeAuthority")
    if freeze_revoked is None:
        freeze_revoked = _revoked(token, "freezeAuthority")

    top_pct: float | None = None
    holders = data.get("topHolders")
    if isinstance(holders, list):
        values = [_f(h.get("pct")) for h in holders[:10] if isinstance(h, dict)]
        top_pct = sum(v for v in values if v is not None) if values else 0.0
    insider = None
    if isinstance(holders, list):
        insider = any(bool(h.get("insider")) for h in holders if isinstance(h, dict))
    if data.get("graphInsidersDetected") is not None:
        insider = bool(insider) or bool(_f(data.get("graphInsidersDetected")))

    transfer_fee = data.get("transferFee")
    transfer_fee_pct = _f(transfer_fee.get("pct")) if isinstance(transfer_fee, dict) else None

    risks = tuple(
        RiskItem(name=str(r.get("name", "?")), level=r.get("level"), score=_f(r.get("score")),
                 description=r.get("description"))
        for r in data.get("risks") or [] if isinstance(r, dict))

    holders_count = data.get("totalHolders")
    return RugReport(
        mint=mint,
        score=_f(data.get("score")),
        score_normalised=_f(data.get("score_normalised")),
        rugged=data.get("rugged") if isinstance(data.get("rugged"), bool) else None,
        mint_authority_revoked=mint_revoked,
        freeze_authority_revoked=freeze_revoked,
        top10_holders_pct=top_pct,
        insider_detected=insider,
        transfer_fee_pct=transfer_fee_pct,
        total_market_liquidity_usd=_f(data.get("totalMarketLiquidity")),
        total_holders=int(holders_count) if isinstance(holders_count, int | float) else None,
        risks=risks,
        raw=data,
    )


class RugCheckClient(HttpProvider):
    name = "rugcheck"

    def __init__(self, base_url: str, *, api_key: str = "", **kwargs: Any):
        headers = {"X-API-KEY": api_key} if api_key else {}
        super().__init__(base_url, headers=headers, **kwargs)

    async def get_report(self, mint: str) -> RugReport:
        data = await self._request("GET", f"/v1/tokens/{mint}/report")
        if not isinstance(data, dict):
            raise ProviderError(self.name, "rapport inattendu")
        return parse_report(mint, data)
