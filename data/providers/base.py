"""Client HTTP commun : limite de débit, délais, nouvelles tentatives, comptage, messages sûrs."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from core.logs import redact
from data.rate_limiter import AsyncRateLimiter

log = logging.getLogger(__name__)

# (fournisseur, requêtes, crédits, erreurs)
UsageRecorder = Callable[[str, int, int, int], None]


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(f"[{provider}] {redact(message)}")
        self.provider = provider
        self.status = status
        self.retryable = retryable


class BudgetExceeded(ProviderError):
    pass


class HttpProvider:
    name = "http"

    def __init__(self, base_url: str, *, limiter: AsyncRateLimiter, headers: dict[str, str] | None = None,
                 params: dict[str, str] | None = None, timeout: float = 10.0, max_retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None, usage: UsageRecorder | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers or {}, params=params or {},
                                         timeout=httpx.Timeout(timeout, connect=5.0), transport=transport)
        self._limiter = limiter
        self._max_retries = max_retries
        self._usage = usage
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _record(self, requests: int, credits: int, errors: int) -> None:
        if self._usage is not None:
            try:
                self._usage(self.name, requests, credits, errors)
            except Exception:  # noqa: BLE001 - le comptage ne doit jamais casser une requête
                log.exception("Échec de l'enregistrement de la consommation API")

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                       json: Any = None, credits: int = 1) -> Any:
        last_error: ProviderError | None = None
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            retry_after: float | None = None
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TimeoutException:
                last_error = ProviderError(self.name, f"délai dépassé sur {path or '/'}", retryable=True)
            except httpx.TransportError as exc:
                last_error = ProviderError(self.name, f"erreur réseau sur {path or '/'} : {type(exc).__name__}",
                                           retryable=True)
            else:
                status = response.status_code
                if status == 429 or status >= 500:
                    header = response.headers.get("retry-after")
                    if header and header.replace(".", "", 1).isdigit():
                        retry_after = min(float(header), 30.0)
                    if status == 429 and hasattr(self._limiter, "penalize"):
                        self._limiter.penalize(retry_after or 1.0)
                    last_error = ProviderError(self.name, f"HTTP {status} sur {path or '/'}", status=status,
                                               retryable=True)
                elif status >= 400:
                    self._record(1, credits, 1)
                    body = response.text[:200].replace("\n", " ")
                    raise ProviderError(self.name, f"HTTP {status} sur {path or '/'} : {body}", status=status)
                else:
                    self._record(1, credits, 0)
                    if hasattr(self._limiter, "reward"):
                        self._limiter.reward()
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ProviderError(self.name, f"réponse non JSON sur {path or '/'}") from exc
            self._record(1, 0, 1)
            if attempt < self._max_retries:
                delay = retry_after if retry_after is not None else min(8.0, 0.5 * 2 ** attempt)
                log.warning("%s — nouvelle tentative dans %.1fs", last_error, delay)
                await self._sleep(delay)
        assert last_error is not None
        raise last_error
