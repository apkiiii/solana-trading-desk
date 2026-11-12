"""Limiteur de débit asynchrone (seau à jetons) pour respecter les limites de chaque API."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class AsyncRateLimiter:
    """Autorise au plus ``rate`` requêtes par seconde, avec une rafale de ``burst`` requêtes."""

    def __init__(self, rate: float, burst: int = 1, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if rate <= 0:
            raise ValueError("rate doit être > 0")
        self.rate = float(rate)
        self.base_rate = float(rate)
        self._paused_until = 0.0
        self.capacity = float(max(1, burst))
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def penalize(self, retry_after: float = 1.0) -> None:
        """Le fournisseur a répondu « trop de requêtes » : pause commune et débit réduit (adaptatif)."""
        now = self._clock()
        self._paused_until = max(self._paused_until, now + max(1.0, retry_after))
        self.rate = max(self.base_rate * 0.3, self.rate * 0.7)
        self._tokens = 0.0

    def reward(self) -> None:
        """Requête acceptée : le débit remonte doucement vers sa valeur normale."""
        self.rate = min(self.base_rate, self.rate * 1.03)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                pause = self._paused_until - self._clock()
                if pause > 0:
                    await self._sleep(pause)
                    continue
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await self._sleep((1 - self._tokens) / self.rate)


class CombinedLimiter:
    """Respecte plusieurs limiteurs à la fois (ex. limite commune Helius + part réservée à HUNTER)."""

    def __init__(self, *limiters: AsyncRateLimiter):
        self.limiters = limiters

    async def acquire(self) -> None:
        for limiter in self.limiters:
            await limiter.acquire()

    def penalize(self, retry_after: float = 1.0) -> None:
        for limiter in self.limiters:
            limiter.penalize(retry_after)

    def reward(self) -> None:
        for limiter in self.limiters:
            limiter.reward()
