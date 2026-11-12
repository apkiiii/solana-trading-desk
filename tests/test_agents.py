"""Tests des agents avec des fournisseurs simulés (aucun accès réseau)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agents.analytics import compute_trader_stats, score_trader
from agents.base import BLOCK, DUPLICATE, IGNORED, OK, UNAVAILABLE, WARN, AgentReport, guarded, save_report
from agents.exit_agent import ExitAgent
from agents.head import HeadAgent
from agents.risk import RiskAgent
from agents.scout import ScoutAgent
from agents.sniper import SniperAgent
from agents.whale import WhaleAgent
from config.settings import load_settings
from data.models import MintInfo, PairInfo, PriceInfo, RiskItem, RugReport, SignatureNotice, SwapEvent, SwapQuote, \
    TokenSnapshot
from data.providers.base import ProviderError
from tests.conftest import OTHER_WALLET, SOL, TOKEN, TOKEN_B, USDC, WALLET
from tests.test_swap_parser import FEE, RENT, make_tx, tb

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
DAY = 86400
POOL = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"


def run(coro):
    return asyncio.run(coro)


def ev(side, ts, sol, amount=1_000, mint=TOKEN, quote=SOL, sig=None):
    return SwapEvent(signature=sig or f"s{ts}{side}{mint[:3]}", wallet=WALLET, slot=1, block_time=ts, side=side,
                     token_mint=mint, token_amount_raw=amount, token_decimals=6, quote_mint=quote,
                     quote_amount_raw=int(sol * 1e9), quote_decimals=9, fee_lamports=5000, fee_paid_by_wallet=True)


def good_history(trades=40, start=1_760_000_000):
    """Trader régulier : 75 % de trades gagnants (+40 %), 25 % perdants (-20 %), sur ~40 jours."""
    events = []
    for i in range(trades):
        ts = start + i * DAY
        mint = f"Mint{i:040d}"[:44]
        events.append(ev("BUY", ts, 1.0, mint=mint))
        events.append(ev("SELL", ts + 3600, 1.4 if i % 4 else 0.8, mint=mint))
    return events


# ------------------------------------------------------------------ statistiques
def test_stats_for_a_regular_profitable_trader():
    stats = compute_trader_stats(good_history())
    assert stats.closed_trades == 40 and stats.wins == 30
    assert stats.win_rate == pytest.approx(0.75)
    assert stats.realized_pnl_sol == pytest.approx(30 * 0.4 - 10 * 0.2)
    assert stats.avg_win_pct == pytest.approx(40.0)
    assert stats.max_drawdown_sol == pytest.approx(0.2)
    assert stats.suspicious == []
    score, notes = score_trader(stats, min_closed_trades=30, min_history_days=14)
    assert score >= 70 and notes == []


def test_score_is_not_based_on_total_gain_only():
    # Un seul énorme gain et beaucoup de pertes : gain total positif mais trader médiocre.
    events = [ev("BUY", 1_760_000_000, 1.0, mint=TOKEN), ev("SELL", 1_760_000_100, 30.0, mint=TOKEN)]
    for i in range(40):
        mint = f"Lose{i:040d}"[:44]
        ts = 1_760_000_000 + (i + 1) * DAY
        events += [ev("BUY", ts, 1.0, mint=mint), ev("SELL", ts + 600, 0.5, mint=mint)]
    stats = compute_trader_stats(events)
    assert stats.realized_pnl_sol > 0
    score, notes = score_trader(stats, min_closed_trades=30, min_history_days=14)
    assert score < 60
    assert stats.win_rate < 0.1


def test_short_history_caps_the_score():
    stats = compute_trader_stats(good_history(trades=5))
    score, notes = score_trader(stats, min_closed_trades=30, min_history_days=14)
    assert score <= 40 and len(notes) == 2


def test_suspicious_patterns_are_flagged():
    sells_only = [ev("SELL", 1_760_000_000 + i, 1.0, mint=f"Air{i:041d}"[:44]) for i in range(8)]
    stats = compute_trader_stats(sells_only)
    assert stats.unmatched_sells == 8 and any("jamais achetés" in s for s in stats.suspicious)
    bot = []
    for i in range(400):
        bot += [ev("BUY", 1_760_000_000 + i * 10, 0.1, mint=TOKEN), ev("SELL", 1_760_000_000 + i * 10 + 5, 0.11,
                                                                         mint=TOKEN)]
    flags = compute_trader_stats(bot).suspicious
    assert any("fréquence anormale" in f for f in flags) and any("moins d'une minute" in f for f in flags)


def test_usdc_trades_are_ignored_in_sol_stats():
    stats = compute_trader_stats([ev("BUY", 1, 1.0, quote=USDC), ev("SELL", 2, 2.0, quote=USDC)])
    assert stats.swaps == 0 and stats.closed_trades == 0


# ----------------------------------------------------------------------- base
def test_guarded_turns_errors_and_timeouts_into_unavailable():
    async def boom():
        raise ProviderError("helius", "HTTP 503")

    async def slow():
        await asyncio.sleep(1)

    assert run(guarded("RISK", "x", boom(), 1)).status == UNAVAILABLE
    report = run(guarded("RISK", "x", slow(), 0.01))
    assert report.status == UNAVAILABLE and "délai" in report.summary


def test_reports_are_saved(db):
    save_report(db, AgentReport("RISK", TOKEN, BLOCK, "refusé", ["liquidité"], {"x": 1.5}, 0.8))
    rows = db.latest_agent_reports("RISK")
    assert rows[0]["status"] == BLOCK and rows[0]["payload"]["reasons"] == ["liquidité"]


# ---------------------------------------------------------------------- SCOUT
class FakeRpc:
    def __init__(self, txs=None, signatures=None, fail=False):
        self.txs = txs or {}
        self.signatures = signatures or {}
        self.fail = fail

    async def get_transaction(self, signature, critical=False):
        if self.fail:
            raise ProviderError("helius", "HTTP 503")
        return self.txs.get(signature)

    async def get_signatures_for_address(self, address, limit=100, before=None, until=None, critical=False):
        items = self.signatures.get(address, [])
        if before:
            index = [s["signature"] for s in items].index(before) + 1
            items = items[index:]
        return items[:limit]


def buy_tx(sig, wallet=WALLET, sol=1_000_000_000, mint=TOKEN, ts=None, payer=None):
    keys = [payer, wallet, "ata1"] if payer else [wallet, "ata1", POOL]
    if payer:
        pre, post = [10**10, 5 * 10**9, 0], [10**10 - FEE - RENT, 5 * 10**9 - sol, RENT]
        post_tok = [tb(2, mint, wallet, 5_000, 6)]
    else:
        pre, post = [10 * 10**9, 0, 50], [10 * 10**9 - sol - FEE - RENT, RENT, 50]
        post_tok = [tb(1, mint, wallet, 5_000, 6)]
    tx = make_tx(keys, pre, post, post_tok=post_tok, sig=sig)
    if ts is not None:
        tx["blockTime"] = ts
    return tx


def sell_tx(sig, wallet=WALLET, sol=1_000_000_000, mint=TOKEN, ts=None):
    tx = make_tx([wallet, "ata1", POOL], [10**9, RENT, 50], [10**9 + sol - FEE + RENT, 0, 50],
                 pre_tok=[tb(1, mint, wallet, 5_000, 6)], sig=sig)
    if ts is not None:
        tx["blockTime"] = ts
    return tx


def notice(sig, wallet=WALLET):
    return SignatureNotice(wallet=wallet, signature=sig, slot=1, received_at=NOW)


def test_scout_detects_buy_dedupes_and_ignores_non_swaps(db):
    ts = int(NOW.timestamp()) - 5
    rpc = FakeRpc({"b1": buy_tx("b1", ts=ts),
                   "t1": make_tx([WALLET, OTHER_WALLET], [10**10, 0], [9 * 10**9 - FEE, 10**9], sig="t1")})
    scout = ScoutAgent(rpc, db, usdc_mint=USDC)
    report = run(scout.process_notice(notice("b1"), now=NOW))
    assert report.status == OK and report.data["side"] == "BUY" and report.data["age_seconds"] == pytest.approx(5)
    assert report.data["activity"]["buys_7d"] == 1
    assert run(scout.process_notice(notice("b1"), now=NOW)).status == DUPLICATE
    assert run(scout.process_notice(notice("t1"), now=NOW)).status == IGNORED
    assert run(scout.process_notice(notice("unknown"), now=NOW)).status == UNAVAILABLE
    assert run(ScoutAgent(FakeRpc(fail=True), db, usdc_mint=USDC).process_notice(notice("x"))).status == UNAVAILABLE


# ----------------------------------------------------------------------- HEAD
def test_head_evaluates_saves_and_reads_cache(db):
    db.upsert_trader(WALLET, "test")
    txs, sigs = {}, []
    start = int((NOW - timedelta(days=40)).timestamp())
    for i in range(36):
        mint = f"Mint{i:040d}"[:44]
        b, s = f"b{i}", f"s{i}"
        txs[b] = buy_tx(b, mint=mint, ts=start + i * DAY)
        txs[s] = sell_tx(s, mint=mint, sol=1_400_000_000 if i % 4 else 800_000_000, ts=start + i * DAY + 3600)
        sigs += [{"signature": s, "err": None}, {"signature": b, "err": None}]
    sigs.reverse()
    settings = load_settings(db)
    head = HeadAgent(FakeRpc(txs, {WALLET: sigs}), db, settings, usdc_mint=USDC)
    report = run(head.evaluate(WALLET))
    assert report.status in (OK, WARN)
    assert report.data["closed_trades"] == 36 and report.data["score"] >= 60
    cached = head.cached_report(WALLET)
    assert cached.data["score"] == report.data["score"]
    assert head.cached_report(WALLET, now=datetime.now(UTC) + timedelta(days=3)).status == UNAVAILABLE
    assert head.ranking()[0]["address"] == WALLET


def test_head_blocks_unknown_or_bad_traders(db):
    settings = load_settings(db)
    head = HeadAgent(FakeRpc(), db, settings, usdc_mint=USDC)
    assert head.cached_report(WALLET).status == UNAVAILABLE           # non suivi
    db.upsert_trader(WALLET)
    assert head.cached_report(WALLET).status == UNAVAILABLE           # jamais évalué
    report = run(head.evaluate(WALLET))                                # aucun historique
    assert report.status == BLOCK and report.data["score"] == 0


# ----------------------------------------------------------------------- RISK
def pair(liquidity=50_000.0, created_hours=10, buys_m5=10, sells_m5=5, change_m5=0.0):
    return PairInfo(pair_address=POOL, dex_id="raydium", base_mint=TOKEN, quote_mint=SOL, price_usd=0.01,
                    price_native=0.0001, liquidity_usd=liquidity, volume_h1=1000, volume_h24=20000, buys_m5=buys_m5,
                    sells_m5=sells_m5, buys_h1=100, sells_h1=80, price_change_m5=change_m5, price_change_h1=1.0,
                    fdv=1e6, market_cap=1e6, pair_created_at=NOW - timedelta(hours=created_hours))


def mint_info(mint_auth=None, freeze=None, extensions=(), fee=None):
    return MintInfo(mint=TOKEN, token_program="Tokenkeg", is_token_2022=bool(extensions), decimals=6,
                    supply_raw=10**15, mint_authority=mint_auth, freeze_authority=freeze, extensions=extensions,
                    transfer_fee_bps=fee)


def rug(top10=20.0, risks=(), rugged=False):
    return RugReport(mint=TOKEN, score=100, score_normalised=5, rugged=rugged, mint_authority_revoked=True,
                     freeze_authority_revoked=True, top10_holders_pct=top10, insider_detected=False,
                     transfer_fee_pct=None, total_market_liquidity_usd=50_000, total_holders=900, risks=tuple(risks))


def snapshot(**overrides):
    snap = TokenSnapshot(mint=TOKEN, fetched_at=NOW, price=PriceInfo(TOKEN, 0.01, 1, 6, None, None),
                         best_pair=pair(), mint_info=mint_info(), rug=rug(), largest_accounts=[])
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap


def quote(inp, out, in_amount, out_amount, impact=0.5, fee_bps=10, ok=True):
    return SwapQuote(input_mint=inp, output_mint=out, in_amount_raw=in_amount, out_amount_raw=out_amount if ok else 0,
                     min_out_amount_raw=None, price_impact_pct=impact, slippage_bps=50, fee_bps=fee_bps,
                     platform_fee_bps=5, router="metis", request_id="r", transaction_b64=None,
                     signature_fee_lamports=5000, prioritization_fee_lamports=50_000, rent_fee_lamports=None,
                     gasless=False, in_usd=None, out_usd=None, error_code=None if ok else 1,
                     error_message=None if ok else "no route")


class FakeJupiter:
    def __init__(self, sell_ok=True, buy_impact=0.5, sell_impact=0.6, fail=False, prices=None):
        self.sell_ok, self.buy_impact, self.sell_impact, self.fail = sell_ok, buy_impact, sell_impact, fail
        self.prices = prices or {}

    async def get_order(self, input_mint, output_mint, amount):
        if self.fail:
            raise ProviderError("jupiter", "HTTP 503")
        if input_mint == SOL:
            return quote(SOL, output_mint, amount, 2_000_000, self.buy_impact)
        return quote(input_mint, SOL, amount, 24_000_000, self.sell_impact, ok=self.sell_ok)

    async def get_prices(self, mints):
        return {m: self.prices[m] for m in mints if m in self.prices}


def risk_agent(db, **jup):
    return RiskAgent(FakeJupiter(**jup), load_settings(db))


def test_risk_accepts_a_clean_token(db):
    report = run(risk_agent(db).analyze(TOKEN, 25_000_000, snapshot(), now=NOW))
    assert report.status == OK, report.reasons
    assert report.data["sell_quote"]["ok"] and report.data["round_trip_loss_pct"] == pytest.approx(4.0)


@pytest.mark.parametrize("overrides,jup,expected_text", [
    ({"mint_info": mint_info(mint_auth="Creator")}, {}, "autorité de création"),
    ({"mint_info": mint_info(freeze="Creator")}, {}, "autorité de gel"),
    ({"mint_info": mint_info(extensions=("transferHook",))}, {}, "transferHook"),
    ({"mint_info": mint_info(fee=300)}, {}, "frais de transfert"),
    ({"best_pair": pair(liquidity=5_000)}, {}, "liquidité"),
    ({"best_pair": pair(created_hours=0.1)}, {}, "âge"),
    ({"rug": rug(top10=80)}, {}, "concentration"),
    ({"rug": rug(rugged=True)}, {}, "rugged"),
    ({"rug": rug(risks=[RiskItem("Single holder ownership", "danger", 5000, "")])}, {}, "Single holder"),
    ({}, {"sell_ok": False}, "invendable"),
    ({}, {"buy_impact": 5.0}, "impact prix à l'achat"),
    ({}, {"sell_impact": 9.0}, "impact prix à la revente"),
])
def test_risk_blocks_dangerous_tokens(db, overrides, jup, expected_text):
    report = run(risk_agent(db, **jup).analyze(TOKEN, 25_000_000, snapshot(**overrides), now=NOW))
    assert report.status == BLOCK
    assert any(expected_text in r for r in report.reasons), report.reasons


def test_risk_blocks_when_information_is_missing(db):
    assert run(risk_agent(db).analyze(TOKEN, 25_000_000, snapshot(rug=None), now=NOW)).status == UNAVAILABLE
    assert run(risk_agent(db, fail=True).analyze(TOKEN, 25_000_000, snapshot(), now=NOW)).status == UNAVAILABLE
    no_pool = snapshot(best_pair=None)
    assert run(risk_agent(db).analyze(TOKEN, 25_000_000, no_pool, now=NOW)).status == UNAVAILABLE


# ---------------------------------------------------------------------- WHALE
def test_whale_blocks_big_sellers_while_small_buyers_buy(db):
    sigs, txs = [], {}
    for i in range(3):  # 3 grosses ventes de 20 SOL
        whale = f"Whale{i}".ljust(44, "1")
        s = f"ws{i}"
        txs[s] = make_tx([whale, "a", POOL], [10**10, RENT, 50], [10**10 + 20 * 10**9 - FEE + RENT, 0, 50],
                         pre_tok=[tb(1, TOKEN, whale, 10**9, 6)], sig=s)
        sigs.append({"signature": s, "err": None})
    for i in range(6):  # 6 petits achats de 0,05 SOL
        small = f"Small{i}".ljust(44, "1")
        s = f"sb{i}"
        txs[s] = buy_tx(s, wallet=small, sol=50_000_000)
        sigs.append({"signature": s, "err": None})
    whale = WhaleAgent(FakeRpc(txs, {POOL: sigs}), db, load_settings(db))
    report = run(whale.analyze(TOKEN, snapshot(), sol_usd=90.0, now=NOW))
    assert report.status == BLOCK and "gros vendeurs" in report.summary
    assert report.data["big_sells_usd"] == pytest.approx(5400, rel=1e-3) and report.data["small_buys"] == 6


def test_whale_flags_group_buys_and_never_proposes_a_buy(db):
    db.upsert_trader(WALLET)
    db.upsert_trader(OTHER_WALLET)
    for w, s in ((WALLET, "g1"), (OTHER_WALLET, "g2")):
        e = ev("BUY", int(NOW.timestamp()) - 60, 0.5, sig=s)
        db.insert_source_event(SwapEvent(**{**e.__dict__, "wallet": w}))
    whale = WhaleAgent(FakeRpc({}, {POOL: []}), db, load_settings(db))
    report = run(whale.analyze(TOKEN, snapshot(), sol_usd=90.0, now=NOW))
    assert report.status == WARN and any("achat groupé" in r for r in report.reasons)
    assert report.status != OK or "BUY" not in str(report.data.get("action", ""))
    assert run(whale.analyze(TOKEN, snapshot(best_pair=None), sol_usd=90.0)).status == UNAVAILABLE


# --------------------------------------------------------------------- SNIPER
def head_report(status=OK, avg_win=40.0, median_buy=1.0):
    return AgentReport("HEAD", WALLET, status, "score 75", [], {"avg_win_pct": avg_win, "median_buy_sol": median_buy,
                                                              "score": 75}, 1.0)


def risk_report(status=OK, buy_impact=0.4, sell_impact=0.5, fee_bps=10, priority=50_000):
    q = {"ok": True, "price_impact_pct": buy_impact, "fee_bps": fee_bps, "prioritization_fee_lamports": priority}
    s = {"ok": True, "price_impact_pct": sell_impact, "fee_bps": fee_bps}
    return AgentReport("RISK", TOKEN, status, "ok", [], {"buy_quote": q, "sell_quote": s}, 1.0)


EVENT = {"signature": "src1", "wallet": WALLET, "side": "BUY", "token_mint": TOKEN, "quote_amount": 1.0}


def test_sniper_builds_a_bounded_proposal(db):
    sniper = SniperAgent(load_settings(db))
    report = sniper.propose(event=EVENT, head=head_report(), risk=risk_report(), eur_per_sol=80.0,
                            open_positions=0, invested_eur=0.0)
    assert report.status == OK, report.reasons
    assert report.data["amount_eur"] == 2.0
    assert report.data["amount_lamports"] == 25_000_000
    assert report.data["estimated_fees_pct"] < 3.0
    # Proportionnel mais borné : un petit achat du trader réduit la taille, jamais au-delà du maximum.
    small = sniper.propose(event={**EVENT, "quote_amount": 0.1}, head=head_report(), risk=risk_report(),
                           eur_per_sol=80.0, open_positions=0, invested_eur=0.0)
    assert small.data["amount_eur"] == 1.0
    big = sniper.propose(event={**EVENT, "quote_amount": 50.0}, head=head_report(), risk=risk_report(),
                         eur_per_sol=80.0, open_positions=0, invested_eur=0.0)
    assert big.data["amount_eur"] == 2.0


def test_sniper_cannot_execute_anything(db):
    sniper = SniperAgent(load_settings(db))
    for forbidden in ("execute", "send", "sign", "swap", "buy", "wallet", "rpc", "jupiter"):
        assert not hasattr(sniper, forbidden)


@pytest.mark.parametrize("kwargs,status,text", [
    ({"head": head_report(status=BLOCK)}, BLOCK, "Prérequis"),
    ({"risk": risk_report(status=UNAVAILABLE)}, BLOCK, "Prérequis"),
    ({"eur_per_sol": None}, UNAVAILABLE, "euros"),
    ({"open_positions": 2}, BLOCK, "positions"),
    ({"invested_eur": 17.2}, BLOCK, "Budget"),
    ({"risk": risk_report(priority=200_000, fee_bps=100)}, BLOCK, "frais"),
    ({"head": head_report(avg_win=3.0)}, BLOCK, "intéressant après frais"),
    ({"head": head_report(avg_win=None)}, BLOCK, "gain moyen"),
])
def test_sniper_refuses_bad_conditions(db, kwargs, status, text):
    params = {"event": EVENT, "head": head_report(), "risk": risk_report(), "eur_per_sol": 80.0,
              "open_positions": 0, "invested_eur": 0.0, **kwargs}
    report = SniperAgent(load_settings(db)).propose(**params)
    assert report.status == status
    assert text in report.summary or any(text in r for r in report.reasons), (report.summary, report.reasons)


# ----------------------------------------------------------------------- EXIT
def add_position(db, *, entry=0.01, opened=NOW - timedelta(hours=1), mint=TOKEN, wallet=WALLET):
    db.execute("INSERT INTO positions (token_mint, status, mode, source_wallet, opened_at, amount_raw, decimals, "
               "entry_price_usd, cost_eur) VALUES (?, 'OPEN', 'PAPER', ?, ?, '1000', 6, ?, 2.0)",
               (mint, wallet, opened.isoformat(), entry))


class FakeDex:
    def __init__(self, p=None):
        self.p = p

    async def best_pair(self, mint):
        return self.p


def exit_agent(db, price, p=None):
    prices = {TOKEN: PriceInfo(TOKEN, price, 1, 6, None, None)} if price is not None else {}
    return ExitAgent(FakeJupiter(prices=prices), FakeDex(p or pair()), db, load_settings(db))


@pytest.mark.parametrize("price,expected", [(0.007, "stop-loss"), (0.016, "prise de profit")])
def test_exit_stop_loss_and_take_profit(db, price, expected):
    add_position(db)
    report = run(exit_agent(db, price).review(now=NOW))[0]
    assert report.status == WARN and report.data["action"] == "SELL" and expected in report.summary


def test_exit_follows_trader_sell_and_works_even_when_stopped(db):
    db.set_control(actor="dashboard", manual_stop=True, bot_state="STOPPED")
    add_position(db)
    sell = ev("SELL", int(NOW.timestamp()) - 60, 1.0, sig="trader-sell")
    db.insert_source_event(sell)
    report = run(exit_agent(db, 0.0105).review(now=NOW))[0]
    assert report.data["action"] == "SELL" and "trader copié a vendu" in report.summary


def test_exit_liquidity_drop_reversal_max_hold_and_missing_price(db):
    add_position(db, opened=NOW - timedelta(hours=30))
    report = run(exit_agent(db, 0.009, pair(liquidity=1_000, change_m5=-20)).review(now=NOW))[0]
    joined = " ".join(report.reasons)
    assert "durée maximale" in joined and "liquidité" in joined and "retournement" in joined
    db.execute("DELETE FROM positions")
    add_position(db)
    missing = run(exit_agent(db, None).review(now=NOW))[0]
    assert missing.status == UNAVAILABLE and missing.data["action"] == "HOLD"


def test_exit_flags_unconfirmed_transactions(db):
    db.execute("INSERT INTO executions (side, mode, status, tx_signature, created_at) VALUES "
               "('BUY', 'LIVE', 'SENT', 'pending-sig', ?)", ((NOW - timedelta(minutes=5)).isoformat(),))
    reports = run(exit_agent(db, 0.01).review(now=NOW))
    assert any(r.data.get("action") == "CHECK_TX" for r in reports)


def test_unused_imports_guard():
    assert TOKEN_B and RENT
