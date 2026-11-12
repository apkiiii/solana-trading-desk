"""Tests des adaptateurs d'API avec des réponses simulées (aucun accès réseau)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from config.settings import load_settings
from data.market_data import MarketData
from data.models import PriceInfo
from data.providers.base import BudgetExceeded, ProviderError
from data.providers.dexscreener import DexScreenerClient
from data.providers.factory import build_providers
from data.providers.jupiter import JupiterClient
from data.providers.rugcheck import RugCheckClient
from data.providers.solana_rpc import TOKEN_2022_PROGRAM, SolanaRpcClient
from data.rate_limiter import AsyncRateLimiter
from security.secrets import ApiSecrets, SecretsError
from tests.conftest import SOL, TOKEN, USDC

EURC = "HzwqbKZw8HxMN6bF2yFZNrht3c2iXXzpKcFu7uBEDKtr"


async def _no_sleep(_: float) -> None:
    return None


def fast_limiter() -> AsyncRateLimiter:
    return AsyncRateLimiter(1000, burst=1000)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ RPC
def test_rpc_keeps_api_key_in_query_and_parses_mint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        body = json.loads(request.content)
        assert body["method"] == "getAccountInfo"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"value": {
            "owner": TOKEN_2022_PROGRAM,
            "data": {"parsed": {"type": "mint", "info": {
                "decimals": 6, "supply": "1000000000", "mintAuthority": None, "freezeAuthority": "Freeze111",
                "extensions": [{"extension": "transferFeeConfig",
                                "state": {"newerTransferFee": {"transferFeeBasisPoints": 250}}}]}}}}}})

    async def go():
        rpc = SolanaRpcClient("https://rpc.example/?api-key=SECRETKEY123", limiter=fast_limiter(),
                              transport=httpx.MockTransport(handler), sleep=_no_sleep)
        info = await rpc.get_mint_info(TOKEN)
        await rpc.aclose()
        return info

    info = run(go())
    assert "api-key=SECRETKEY123" in seen["url"]
    assert info.mint_authority is None and info.freeze_authority == "Freeze111"
    assert info.is_token_2022 and info.transfer_fee_bps == 250 and info.decimals == 6


def test_rpc_retries_on_429_then_succeeds_and_counts_usage():
    calls = {"n": 0}
    usage: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 4242})

    async def go():
        rpc = SolanaRpcClient("https://rpc.example/?api-key=K", limiter=fast_limiter(), sleep=_no_sleep,
                              transport=httpx.MockTransport(handler), usage=lambda *a: usage.append(a))
        slot = await rpc.get_slot()
        await rpc.aclose()
        return slot

    assert run(go()) == 4242
    assert calls["n"] == 3
    assert sum(u[3] for u in usage) == 2  # deux erreurs comptées
    assert sum(u[2] for u in usage) == 1  # crédits comptés uniquement pour la réponse réussie


def test_rpc_gives_up_after_retries_without_leaking_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def go():
        rpc = SolanaRpcClient("https://rpc.example/?api-key=LEAKME123456", limiter=fast_limiter(),
                              sleep=_no_sleep, transport=httpx.MockTransport(handler), max_retries=2)
        try:
            await rpc.get_slot()
        finally:
            await rpc.aclose()

    with pytest.raises(ProviderError) as info:
        run(go())
    assert "LEAKME123456" not in str(info.value)


def test_rpc_error_object_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "error": {"code": -32602, "message": "Invalid param"}})

    async def go():
        rpc = SolanaRpcClient("https://rpc.example/", limiter=fast_limiter(), sleep=_no_sleep,
                              transport=httpx.MockTransport(handler))
        try:
            await rpc.get_transaction("sig")
        finally:
            await rpc.aclose()

    with pytest.raises(ProviderError, match="Invalid param"):
        run(go())


def test_rpc_switches_to_v1_transactions_when_needed():
    versions = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        version = body["params"][1]["maxSupportedTransactionVersion"]
        versions.append(version)
        if version == 0:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                "code": -32015, "message": "Transaction version (1) is not supported by the requesting client."}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"slot": 1}})

    async def go():
        rpc = SolanaRpcClient("https://rpc.example/", limiter=fast_limiter(), sleep=_no_sleep,
                              transport=httpx.MockTransport(handler))
        result = await rpc.get_transaction("sig")
        await rpc.aclose()
        return result

    assert run(go()) == {"slot": 1}
    assert versions == [0, 1]


def test_rpc_credit_budget_blocks_non_critical_calls_only():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

    async def go():
        rpc = SolanaRpcClient("https://rpc.example/", limiter=fast_limiter(), sleep=_no_sleep,
                              transport=httpx.MockTransport(handler), credit_budget=100,
                              credits_used=lambda: 100)
        try:
            assert await rpc.get_signatures_for_address(TOKEN, critical=True) == []
            await rpc.get_signatures_for_address(TOKEN)
        finally:
            await rpc.aclose()

    with pytest.raises(BudgetExceeded):
        run(go())


# --------------------------------------------------------------- Jupiter
def test_jupiter_prices_and_order_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") == "jupkey"
        if request.url.path == "/price/v3":
            return httpx.Response(200, json={SOL: {"usdPrice": 90.0, "blockId": 1, "decimals": 9},
                                             TOKEN: {"usdPrice": None}})
        assert request.url.path == "/swap/v2/order"
        assert request.url.params["amount"] == "10000000"
        assert "taker" not in request.url.params
        return httpx.Response(200, json={
            "inputMint": SOL, "outputMint": USDC, "inAmount": "10000000", "outAmount": "900000",
            "otherAmountThreshold": "895000", "priceImpact": -0.12, "slippageBps": 50, "feeBps": 12,
            "platformFee": {"amount": "100", "feeBps": 5}, "router": "metis", "requestId": "req-1",
            "transaction": None, "gasless": False, "inUsdValue": 0.9, "outUsdValue": 0.9})

    async def go():
        jup = JupiterClient("https://api.jup.ag", api_key="jupkey", limiter=fast_limiter(),
                            transport=httpx.MockTransport(handler), sleep=_no_sleep)
        prices = await jup.get_prices([SOL, TOKEN])
        quote = await jup.get_order(SOL, USDC, 10_000_000)
        await jup.aclose()
        return prices, quote

    prices, quote = run(go())
    assert prices[SOL].usd_price == 90.0 and TOKEN not in prices
    assert quote.ok and quote.out_amount_raw == 900_000 and quote.min_out_amount_raw == 895_000
    assert quote.price_impact_pct == pytest.approx(0.12)
    assert quote.fee_bps == 12 and quote.platform_fee_bps == 5 and quote.transaction_b64 is None


def test_jupiter_order_error_is_not_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"inputMint": SOL, "outputMint": TOKEN, "inAmount": "1", "outAmount": "0",
                                         "transaction": "", "errorCode": 1, "errorMessage": "Insufficient funds"})

    async def go():
        jup = JupiterClient("https://api.jup.ag", limiter=fast_limiter(), transport=httpx.MockTransport(handler))
        quote = await jup.get_order(SOL, TOKEN, 1, taker="x")
        await jup.aclose()
        return quote

    quote = run(go())
    assert not quote.ok and quote.error_code == 1


# ------------------------------------------------------------ DexScreener
def test_dexscreener_best_pair_picks_highest_liquidity():
    def pair(address, liquidity, base=TOKEN):
        return {"chainId": "solana", "dexId": "raydium", "pairAddress": address,
                "baseToken": {"address": base}, "quoteToken": {"address": SOL}, "priceUsd": "1.5",
                "liquidity": {"usd": liquidity}, "volume": {"h24": 1000, "h1": 10},
                "txns": {"m5": {"buys": 3, "sells": 1}}, "pairCreatedAt": 1_700_000_000_000}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/tokens/v1/solana/{TOKEN}"
        return httpx.Response(200, json=[pair("P1", 5_000), pair("P2", 80_000), pair("P3", 999_999, base=SOL)])

    async def go():
        dex = DexScreenerClient("https://api.dexscreener.com", limiter=fast_limiter(),
                                transport=httpx.MockTransport(handler))
        best = await dex.best_pair(TOKEN)
        await dex.aclose()
        return best

    best = run(go())
    assert best.pair_address == "P2" and best.liquidity_usd == 80_000 and best.buys_m5 == 3
    assert best.pair_created_at is not None


# ---------------------------------------------------------------- RugCheck
def test_rugcheck_normalisation_handles_present_and_missing_fields():
    full = {"score": 500, "score_normalised": 12, "rugged": False, "mintAuthority": None,
            "freezeAuthority": None, "topHolders": [{"pct": 10.0, "insider": False}, {"pct": 5.5}],
            "risks": [{"name": "Low liquidity", "level": "danger", "score": 1000}],
            "totalMarketLiquidity": 45000.5, "totalHolders": 1234}

    def handler(request: httpx.Request) -> httpx.Response:
        if TOKEN in request.url.path:
            return httpx.Response(200, json=full)
        return httpx.Response(200, json={"score": 1})

    async def go():
        rug = RugCheckClient("https://api.rugcheck.xyz", limiter=fast_limiter(),
                             transport=httpx.MockTransport(handler))
        a = await rug.get_report(TOKEN)
        b = await rug.get_report(SOL)
        await rug.aclose()
        return a, b

    report, sparse = run(go())
    assert report.mint_authority_revoked is True and report.freeze_authority_revoked is True
    assert report.top10_holders_pct == pytest.approx(15.5)
    assert report.danger_risks and report.danger_risks[0].name == "Low liquidity"
    # Champs absents -> None (traité comme « information indisponible »)
    assert sparse.mint_authority_revoked is None and sparse.top10_holders_pct is None and sparse.rugged is None


# ------------------------------------------------------------- MarketData
class _FakeJup:
    def __init__(self, prices):
        self.prices = prices

    async def get_prices(self, mints):
        return {m: self.prices[m] for m in mints if m in self.prices}


class _Failing:
    async def best_pair(self, mint):
        raise ProviderError("dexscreener", "HTTP 503")

    async def get_mint_info(self, mint):
        return None

    async def get_token_largest_accounts(self, mint):
        return []

    async def get_report(self, mint):
        raise ProviderError("rugcheck", "HTTP 500")


def _price(mint, usd):
    return PriceInfo(mint=mint, usd_price=usd, block_id=1, decimals=6, liquidity_usd=None, price_change_24h=None)


def test_eur_per_sol_and_sanity_check():
    tokens = {"sol_mint": SOL, "eurc_mint": EURC}
    good = MarketData(None, _FakeJup({SOL: _price(SOL, 90.0), EURC: _price(EURC, 1.125)}), None, None, tokens)
    rate, _ = run(good.eur_per_sol())
    assert rate == pytest.approx(80.0)
    bad = MarketData(None, _FakeJup({SOL: _price(SOL, 90.0), EURC: _price(EURC, 3.0)}), None, None, tokens)
    rate, reason = run(bad.eur_per_sol())
    assert rate is None and "incohérent" in reason
    missing = MarketData(None, _FakeJup({SOL: _price(SOL, 90.0)}), None, None, tokens)
    assert run(missing.eur_per_sol())[0] is None


def test_snapshot_records_every_failing_source():
    failing = _Failing()
    market = MarketData(failing, _FakeJup({}), failing, failing, {"sol_mint": SOL, "eurc_mint": EURC})
    snap = run(market.snapshot(TOKEN))
    assert not snap.complete
    assert set(snap.errors) == {"price", "pair", "mint", "rug"}


# ----------------------------------------------------------------- factory
def test_factory_requires_helius_key_and_builds_urls(db):
    settings = load_settings(db)
    with pytest.raises(SecretsError):
        build_providers(settings, ApiSecrets())
    providers = build_providers(settings, ApiSecrets(helius_api_key="abcdef123456"), db)
    assert providers.ws_url == "wss://mainnet.helius-rpc.com/?api-key=abcdef123456"
    run(providers.aclose())
    override = build_providers(settings, ApiSecrets(rpc_url_override="https://my-rpc.example/x"))
    assert override.ws_url == "wss://my-rpc.example/x"
    run(override.aclose())
