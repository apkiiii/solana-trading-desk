"""Construction des clients de données à partir des réglages et des clés API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from config.settings import Settings
from data.database import Database
from data.providers.dexscreener import DexScreenerClient
from data.providers.jupiter import JupiterClient
from data.providers.rugcheck import RugCheckClient
from data.providers.solana_rpc import SolanaRpcClient
from data.rate_limiter import AsyncRateLimiter
from security.secrets import ApiSecrets, SecretsError


@dataclass
class Providers:
    rpc: SolanaRpcClient
    ws_url: str
    jupiter: JupiterClient
    dex: DexScreenerClient
    rug: RugCheckClient
    gmgn: Any = None  # client GMGN (lecture seule) si une clé API GMGN est enregistrée

    async def aclose(self) -> None:
        for client in (self.rpc, self.jupiter, self.dex, self.rug, self.gmgn):
            if client is not None:
                await client.aclose()


def _ws_from_http(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def build_providers(settings: Settings, secrets: ApiSecrets, db: Database | None = None, *,
                    transport: httpx.AsyncBaseTransport | None = None) -> Providers:
    providers = settings["providers"]
    helius = providers["helius"]
    if secrets.rpc_url_override:
        rpc_url = secrets.rpc_url_override
        ws_url = secrets.ws_url_override or _ws_from_http(rpc_url)
    elif secrets.helius_api_key:
        rpc_url = helius["rpc_url"].format(key=secrets.helius_api_key)
        ws_url = helius["ws_url"].format(key=secrets.helius_api_key)
    else:
        raise SecretsError("HELIUS_API_KEY manquante dans le fichier .env (ou SOLANA_RPC_URL).")

    def usage(provider: str, requests: int, credits: int, errors: int) -> None:
        if db is not None:
            db.add_api_usage(provider, requests=requests, credits=credits, errors=errors)

    common: dict[str, Any] = {"usage": usage, "transport": transport}

    rpc = SolanaRpcClient(
        rpc_url,
        limiter=AsyncRateLimiter(float(helius["requests_per_second"]), burst=4),
        max_transaction_version=int(helius.get("max_transaction_version", 0)),
        credit_budget=int(helius["monthly_credit_budget"]) if not secrets.rpc_url_override else None,
        credits_used=(lambda: db.month_credits("helius")) if db is not None else None,
        **common)

    jup = providers["jupiter"]
    jup_rate = float(jup["requests_per_second"] if secrets.jupiter_api_key else jup["requests_per_second_keyless"])
    jupiter = JupiterClient(jup["base_url"], api_key=secrets.jupiter_api_key,
                            limiter=AsyncRateLimiter(jup_rate, burst=1), **common)

    dex_cfg = providers["dexscreener"]
    dex = DexScreenerClient(dex_cfg["base_url"],
                            limiter=AsyncRateLimiter(float(dex_cfg["requests_per_second"]), burst=2), **common)

    rug_cfg = providers["rugcheck"]
    rug = RugCheckClient(rug_cfg["base_url"], api_key=secrets.rugcheck_api_key,
                         limiter=AsyncRateLimiter(float(rug_cfg["requests_per_second"]), burst=1), **common)
    gmgn = build_gmgn(settings, secrets, db, transport=transport)
    return Providers(rpc=rpc, ws_url=ws_url, jupiter=jupiter, dex=dex, rug=rug, gmgn=gmgn)


def build_gmgn(settings: Settings, secrets: ApiSecrets, db: Database | None = None, *,
               transport: httpx.AsyncBaseTransport | None = None) -> Any:
    """Client GMGN en lecture (recherche de traders). None si aucune clé API GMGN."""
    if not secrets.gmgn_api_key:
        return None
    from data.providers.gmgn import GmgnClient

    def usage(provider: str, requests: int, credits: int, errors: int) -> None:
        if db is not None:
            db.add_api_usage(provider, requests=requests, credits=credits, errors=errors)

    return GmgnClient(settings["venues"]["gmgn"]["base_url"], secrets.gmgn_api_key, usage=usage,
                      transport=transport)
