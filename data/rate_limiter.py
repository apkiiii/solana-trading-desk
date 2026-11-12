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

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await self._sleep((1 - self._tokens) / self.rate)
