"""Client de l'API officielle GMGN (https://openapi.gmgn.ai).

Référence : client officiel GMGNAI/gmgn-skills (licence MIT).
- Toutes les requêtes : en-tête ``X-APIKEY`` + paramètres ``timestamp`` (±5 s) et ``client_id`` (UUID unique).
- Requêtes « critiques » (swap, suivi d'ordre) : en plus, ``X-Signature`` = signature Ed25519 (base64) du
  message ``{chemin}:{query triée}:{corps JSON}:{timestamp}``.
- Réponse : enveloppe ``{"code": 0, "data": …}`` ; tout autre code est une erreur.
- Limite de débit « seau percé » pondéré : Gratuit 5/5, Plus 20/20, Pro 50/50. Une route plus lourde que
  la capacité du forfait (ex. swap = 10 sur le forfait gratuit) répond ``PLAN_UPGRADE_REQUIRED``.
- IPv4 uniquement (limite connue de GMGN).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from core.logs import redact
from core.version import __version__

CHAIN = "sol"


class GmgnApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: Any = None, error: str | None = None,
                 reset_at: int | None = None):
        super().__init__(redact(message))
        self.status = status
        self.code = code
        self.error = error
        self.reset_at = reset_at

    @property
    def plan_upgrade_required(self) -> bool:
        return self.error == "PLAN_UPGRADE_REQUIRED"

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 or (self.error or "").startswith("RATE_LIMIT")


def _encode(value: str) -> str:
    """Équivalent de ``encodeURIComponent`` en JavaScript."""
    return quote(value, safe="-_.!~*'()")


def build_message(sub_path: str, query: dict[str, Any], body: str, timestamp: int) -> str:
    parts: list[str] = []
    for key in sorted(query):
        value = query[key]
        if isinstance(value, list | tuple):
            parts += [f"{_encode(key)}={_encode(str(item))}" for item in sorted(str(v) for v in value)]
        else:
            parts.append(f"{_encode(key)}={_encode(str(value))}")
    return f"{sub_path}:{'&'.join(parts)}:{body}:{timestamp}"


class WeightedLimiter:
    """Seau percé pondéré (même logique que GMGN) : ``rate`` unités/s, ``capacity`` unités au maximum."""

    def __init__(self, rate: float, capacity: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.rate, self.capacity = float(rate), float(capacity)
        self._level = self.capacity
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: float) -> None:
        if weight > self.capacity:
            return  # la requête sera refusée par GMGN (forfait insuffisant) : inutile d'attendre
        async with self._lock:
            while True:
                now = self._clock()
                self._level = min(self.capacity, self._level + (now - self._last) * self.rate)
                self._last = now
                if self._level >= weight - 1e-9:  # tolérance : évite une boucle infinie due aux arrondis
                    self._level = max(0.0, self._level - weight)
                    return
                await self._sleep((weight - self._level) / self.rate)


class GmgnClient:
    name = "gmgn"

    def __init__(self, base_url: str, api_key: str, *, key_store: Any = None, rate: float = 5.0,
                 capacity: float = 5.0, transport: httpx.AsyncBaseTransport | None = None,
                 usage: Callable[[str, int, int, int], None] | None = None,
                 clock: Callable[[], float] = time.time):
        if not api_key:
            raise GmgnApiError("Clé API GMGN manquante (menu Connexions & test).")
        self._client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(12.0, connect=5.0),
                                         transport=transport,
                                         headers={"User-Agent": f"SolanaTradingDesk/{__version__}"})
        self._api_key = api_key
        self._keys = key_store
        self._limiter = WeightedLimiter(rate, capacity)
        self._usage = usage
        self._clock = clock

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----------------------------------------------------------------- noyau
    async def request(self, method: str, path: str, query: dict[str, Any] | None = None, body: Any = None, *,
                      weight: float = 1, signed: bool = False) -> Any:
        await self._limiter.acquire(weight)
        timestamp = int(self._clock())
        params: dict[str, Any] = {**(query or {}), "timestamp": timestamp, "client_id": str(uuid.uuid4())}
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"X-APIKEY": self._api_key, "Content-Type": "application/json"}
        if signed:
            if self._keys is None:
                raise GmgnApiError("Clé de signature GMGN absente : crée-la dans Connexions & test.")
            headers["X-Signature"] = self._keys.sign(build_message(path, params, body_text, timestamp))
        flat: list[tuple[str, str]] = []
        for key, value in params.items():
            for item in (value if isinstance(value, list | tuple) else [value]):
                flat.append((key, str(item)))
        try:
            response = await self._client.request(method, path, params=flat, headers=headers,
                                                  content=body_text.encode() if body_text else None)
        except httpx.HTTPError as exc:
            self._record(0, 1)
            raise GmgnApiError(f"GMGN injoignable ({type(exc).__name__})") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            self._record(0, 1)
            raise GmgnApiError(f"Réponse GMGN illisible (HTTP {response.status_code})",
                               status=response.status_code) from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            self._record(0, 1)
            payload = payload if isinstance(payload, dict) else {}
            error = payload.get("error")
            reset = payload.get("reset_at") or response.headers.get("x-ratelimit-reset")
            raise GmgnApiError(f"GMGN {path} : {payload.get('message') or error or response.status_code}",
                               status=response.status_code, code=payload.get("code"), error=error,
                               reset_at=int(reset) if str(reset or "").isdigit() else None)
        self._record(int(weight), 0)
        return payload.get("data")

    def _record(self, weight: int, errors: int) -> None:
        if self._usage:
            try:
                self._usage(self.name, 1, weight, errors)
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- lecture
    async def user_info(self) -> Any:
        return await self.request("GET", "/v1/user/info", weight=2)

    async def wallet_activity(self, wallet: str, *, limit: int = 20, types: tuple[str, ...] = ("buy", "sell")) -> list:
        data = await self.request("GET", "/v1/user/wallet_activity",
                                  {"chain": CHAIN, "wallet_address": wallet, "limit": limit, "type": list(types)},
                                  weight=3)
        rows = (data or {}).get("activities") if isinstance(data, dict) else data
        return list(rows or [])

    async def wallet_stats(self, wallets: list[str], period: str = "30d") -> Any:
        return await self.request("GET", "/v1/user/wallet_stats",
                                  {"chain": CHAIN, "wallet_address": list(wallets), "period": period}, weight=3)

    async def token_info(self, address: str) -> dict:
        return await self.request("GET", "/v1/token/info", {"chain": CHAIN, "address": address}, weight=1) or {}

    async def token_security(self, address: str) -> dict:
        return await self.request("GET", "/v1/token/security", {"chain": CHAIN, "address": address}, weight=1) or {}

    async def smart_money(self, limit: int = 50) -> Any:
        return await self.request("GET", "/v1/user/smartmoney", {"chain": CHAIN, "limit": limit}, weight=1)

    async def kol(self, limit: int = 50) -> Any:
        return await self.request("GET", "/v1/user/kol", {"chain": CHAIN, "limit": limit}, weight=1)

    # ------------------------------------------------------------- trading
    async def trading_allowed(self) -> tuple[bool, str]:
        """Le forfait permet-il de trader par l'API ? (un devis « pèse » autant qu'un swap)"""
        try:
            await self.request("GET", "/v1/trade/quote",
                               {"chain": CHAIN, "from_address": "11111111111111111111111111111111",
                                "input_token": "So11111111111111111111111111111111111111112",
                                "output_token": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                                "input_amount": "1000000", "slippage": 5}, weight=10)
        except GmgnApiError as exc:
            if exc.plan_upgrade_required or exc.status == 403:
                return False, "forfait gratuit : trading par API non inclus"
            return False, str(exc)
        return True, "trading autorisé par le forfait"

    async def swap(self, *, from_address: str, input_token: str, output_token: str, input_amount: int,
                   slippage_pct: int, priority_fee_sol: float | None = None) -> Any:
        body: dict[str, Any] = {"chain": CHAIN, "from_address": from_address, "input_token": input_token,
                                "output_token": output_token, "input_amount": str(input_amount),
                                "slippage": max(1, min(100, int(slippage_pct))), "is_anti_mev": True}
        if priority_fee_sol is not None:
            body["priority_fee"] = f"{priority_fee_sol:.9f}".rstrip("0").rstrip(".")
        return await self.request("POST", "/v1/trade/swap", body=body, weight=10, signed=True)

    async def query_order(self, order_id: str) -> Any:
        return await self.request("GET", "/v1/trade/query_order", {"order_id": order_id, "chain": CHAIN},
                                  weight=5, signed=True)
