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


def test_rate_limiter_backs_off_on_429_and_recovers_slowly():
    import asyncio

    from data.rate_limiter import AsyncRateLimiter, CombinedLimiter
    state = {"now": 0.0, "slept": 0.0}

    async def sleep(s):
        state["now"] += s
        state["slept"] += s

    shared = AsyncRateLimiter(8, burst=2, clock=lambda: state["now"], sleep=sleep)
    hunter = AsyncRateLimiter(2.5, burst=1, clock=lambda: state["now"], sleep=sleep)
    combined = CombinedLimiter(shared, hunter)
    combined.penalize(2.0)                       # Helius : « trop de requêtes, réessaie dans 2 s »
    assert shared.rate < 8 and hunter.rate < 2.5
    asyncio.run(shared.acquire())
    assert state["slept"] >= 2.0                 # pause commune respectée par tout le monde
    for _ in range(200):
        shared.reward()
    assert shared.rate == 8                      # retour progressif au débit normal
