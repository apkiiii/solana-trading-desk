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
from security.secrets import ApiSecrets


@dataclass
class Providers:
    rpc: SolanaRpcClient
    ws_url: str
    jupiter: JupiterClient
    dex: DexScreenerClient
    rug: RugCheckClient
    gmgn: Any = None  # client GMGN (lecture seule) si une clé API GMGN est enregistrée
    public_rpc: Any = None  # RPC public Solana (sans clé) : recherche de traders par HUNTER
    hunter_rpc: Any = None  # Helius réservé à HUNTER (plafond de crédits par jour), si une clé Helius existe
    keyless_rpc: bool = False  # True si tout le bot passe par le RPC public (aucune clé Helius)

    async def aclose(self) -> None:
        seen: set[int] = set()
        for client in (self.rpc, self.jupiter, self.dex, self.rug, self.gmgn, self.public_rpc, self.hunter_rpc):
            if client is not None and id(client) not in seen:
                seen.add(id(client))
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
    public = providers["public_rpc"]
    keyless = False
    if secrets.rpc_url_override:
        rpc_url = secrets.rpc_url_override
        ws_url = secrets.ws_url_override or _ws_from_http(rpc_url)
    elif secrets.helius_api_key:
        rpc_url = helius["rpc_url"].format(key=secrets.helius_api_key)
        ws_url = helius["ws_url"].format(key=secrets.helius_api_key)
    else:
        # Aucune clé : RPC public de la Fondation Solana (gratuit, limité, plus lent).
        rpc_url, ws_url, keyless = public["rpc_url"], public["ws_url"], True

    def usage(provider: str, requests: int, credits: int, errors: int) -> None:
        if db is not None:
            db.add_api_usage(provider, requests=requests, credits=credits, errors=errors)

    common: dict[str, Any] = {"usage": usage, "transport": transport}

    def public_client() -> SolanaRpcClient:
        client = SolanaRpcClient(public["rpc_url"],
                                 limiter=AsyncRateLimiter(float(public["requests_per_second"]), burst=3),
                                 max_transaction_version=int(helius.get("max_transaction_version", 0)), **common)
        client.name = "solana-public"
        return client

    if keyless:
        rpc = public_client()
    else:
        rpc = SolanaRpcClient(
            rpc_url,
            limiter=AsyncRateLimiter(float(helius["requests_per_second"]), burst=4),
            max_transaction_version=int(helius.get("max_transaction_version", 0)),
            credit_budget=int(helius["monthly_credit_budget"]) if not secrets.rpc_url_override else None,
            credits_used=(lambda: db.month_credits("helius") + db.month_credits("helius-hunter"))
            if db is not None else None,
            **common)
    # Sans clé, un seul client public (les limites du RPC public sont par adresse IP).
    public_rpc = rpc if keyless else public_client()
    hunter_rpc = None
    if secrets.helius_api_key and not secrets.rpc_url_override:
        daily = int(settings["discovery"].get("helius_daily_credits", 0))
        if daily > 0:
            hunter_rpc = SolanaRpcClient(
                helius["rpc_url"].format(key=secrets.helius_api_key),
                limiter=AsyncRateLimiter(float(helius.get("hunter_requests_per_second", 4.0)), burst=4),
                max_transaction_version=int(helius.get("max_transaction_version", 0)),
                credit_budget=daily, credits_used=(lambda: db.day_credits("helius-hunter")) if db is not None else None,
                **common)
            hunter_rpc.name = "helius-hunter"

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
    return Providers(rpc=rpc, ws_url=ws_url, jupiter=jupiter, dex=dex, rug=rug, gmgn=gmgn, public_rpc=public_rpc,
                     keyless_rpc=keyless, hunter_rpc=hunter_rpc)


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
