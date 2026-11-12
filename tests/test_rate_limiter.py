from __future__ import annotations

import asyncio

from data.rate_limiter import AsyncRateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def test_limiter_spaces_requests_after_burst():
    clock = FakeClock()
    limiter = AsyncRateLimiter(2.0, burst=2, clock=clock, sleep=clock.sleep)

    async def go():
        for _ in range(6):
            await limiter.acquire()

    asyncio.run(go())
    # 2 requêtes immédiates, puis 4 requêtes à 2/s -> 2 secondes d'attente au total
    assert abs(clock.slept - 2.0) < 1e-6
