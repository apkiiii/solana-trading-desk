from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from data.providers.gmgn import GmgnApiError, GmgnClient, WeightedLimiter, build_message
from security.gmgn_keys import GmgnKeyStore

crypto = pytest.importorskip("cryptography.hazmat.primitives.serialization")


def run(coro):
    return asyncio.run(coro)


def test_message_format_matches_official_client():
    # Même règle que buildMessage() du client officiel : clés triées, valeurs de liste triées, encodeURIComponent.
    msg = build_message("/v1/trade/swap", {"timestamp": 100, "client_id": "a b", "chain": "sol",
                                           "wallet_address": ["z", "a"]}, '{"x":1}', 100)
    assert msg == "/v1/trade/swap:chain=sol&client_id=a%20b&timestamp=100&wallet_address=a&wallet_address=z:{\"x\":1}:100"


def test_signed_swap_request_is_verifiable_and_read_requests_are_not_signed(tmp_path):
    keys = GmgnKeyStore(tmp_path, use_keyring=False)
    public_pem = keys.create()["public_pem"]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"order_id": "o1", "status": "pending"}})

    async def go():
        client = GmgnClient("https://openapi.gmgn.ai", "api-key-123", key_store=keys, rate=1000, capacity=1000,
                            transport=httpx.MockTransport(handler), clock=lambda: 1_700_000_000)
        result = await client.swap(from_address="W", input_token="So1", output_token="Tok", input_amount=25_000_000,
                                   slippage_pct=2)
        await client.token_security("Tok")
        await client.aclose()
        return result

    assert run(go())["order_id"] == "o1"
    swap_req, read_req = seen
    assert swap_req.headers["X-APIKEY"] == "api-key-123" and "X-Signature" in swap_req.headers
    assert "X-Signature" not in read_req.headers
    params = dict(swap_req.url.params)
    body = swap_req.content.decode()
    assert json.loads(body)["slippage"] == 2 and json.loads(body)["is_anti_mev"] is True
    message = build_message("/v1/trade/swap", params, body, int(params["timestamp"]))
    public = crypto.load_pem_public_key(public_pem.encode())
    public.verify(base64.b64decode(swap_req.headers["X-Signature"]), message.encode())


def test_errors_and_plan_detection():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/trade/quote":
            return httpx.Response(403, json={"code": 403, "error": "PLAN_UPGRADE_REQUIRED", "message": "upgrade"})
        return httpx.Response(429, json={"code": 429, "error": "RATE_LIMIT_EXCEEDED", "message": "slow down",
                                         "reset_at": 1700000100})

    async def go():
        client = GmgnClient("https://openapi.gmgn.ai", "k", rate=1000, capacity=1000,
                            transport=httpx.MockTransport(handler))
        allowed = await client.trading_allowed()
        try:
            await client.token_info("Tok")
        except GmgnApiError as exc:
            err = exc
        await client.aclose()
        return allowed, err

    allowed, err = run(go())
    assert allowed == (False, "forfait gratuit : trading par API non inclus")
    assert err.rate_limited and err.reset_at == 1700000100


def test_missing_key_and_activity_parsing():
    with pytest.raises(GmgnApiError):
        GmgnClient("https://openapi.gmgn.ai", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("type") == ["buy", "sell"]
        return httpx.Response(200, json={"code": 0, "data": {"activities": [{"tx_hash": "t1"}], "next": ""}})

    async def go():
        client = GmgnClient("https://openapi.gmgn.ai", "k", rate=1000, capacity=1000,
                            transport=httpx.MockTransport(handler))
        rows = await client.wallet_activity("W")
        await client.aclose()
        return rows

    assert run(go()) == [{"tx_hash": "t1"}]


def test_weighted_limiter():
    state = {"now": 0.0, "slept": 0.0}

    async def sleep(s):
        state["now"] += s
        state["slept"] += s

    limiter = WeightedLimiter(5, 5, clock=lambda: state["now"], sleep=sleep)

    async def go():
        for _ in range(4):
            await limiter.acquire(3)   # 12 unités : 5 disponibles, puis 7 à 5/s
        await limiter.acquire(10)      # plus lourd que la capacité : pas d'attente (GMGN refusera)

    run(go())
    assert abs(state["slept"] - 1.4) < 1e-6
