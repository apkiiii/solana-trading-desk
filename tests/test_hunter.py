"""HUNTER : recherche automatique des traders (données GMGN simulées, aucun accès réseau)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agents.head import HeadAgent
from agents.hunter import SOURCE_AUTO, HunterAgent, evaluate_wallet
from config.settings import apply_user_change, load_settings
from data.models import PriceInfo
from tests.conftest import SOL, TOKEN, WALLET

NOW = datetime.now(UTC).replace(microsecond=0)


def run(coro):
    return asyncio.run(coro)


def addr(i: int) -> str:
    """Adresses Solana valides et distinctes pour les tests."""
    base = "So11111111111111111111111111111111111111112"
    return base[:-3] + "ABCDEFGHJK"[i // 10] + "ABCDEFGHJK"[i % 10] + "2"


def stats(winrate=0.62, tokens=30, buys=60, sells=55, realized=6000.0, roi=0.7, hold=1800, big=2, created=0):
    return {"buy": buys, "sell": sells, "realized_profit": realized, "realized_profit_pnl": roi,
            "pnl_stat": {"winrate": winrate, "token_num": tokens, "avg_holding_period": hold,
                         "pnl_lt_nd5_num": big},
            "common": {"created_token_count": created, "tags": ["smart_degen"], "twitter_username": "pro"}}


def activity(win_pct=50.0, n=10, age_s=300):
    rows = []
    ts = int(NOW.timestamp()) - age_s
    for i in range(n):
        gain = win_pct if i % 10 < 7 else -30.0
        rows.append({"event_type": "sell", "cost_usd": 100 * (1 + gain / 100), "buy_cost_usd": 100, "timestamp": ts})
        rows.append({"event_type": "buy", "cost_usd": 150.0, "buy_cost_usd": 0, "timestamp": ts - 600})
    return rows


class FakeGmgn:
    def __init__(self, profiles):
        self.profiles = profiles          # adresse -> (stats, activité)
        self.calls = 0

    async def smart_money(self, limit=100):
        return {"list": [{"maker": a, "side": "buy", "timestamp": int(NOW.timestamp())} for a in self.profiles]}

    async def kol(self, limit=100):
        return {"list": []}

    async def wallet_stats(self, wallets, period="7d"):
        self.calls += 1
        return self.profiles[wallets[0]][0]

    async def wallet_activity(self, wallet, limit=50):
        return self.profiles[wallet][1]


class FakeJupiter:
    async def get_prices(self, mints):
        return {SOL: PriceInfo(SOL, 150.0, 1, 9, None, None)}


def hunter(db, gmgn):
    return HunterAgent(gmgn, db, load_settings(db), jupiter=FakeJupiter())


def cfg(db):
    return load_settings(db)["discovery"]


# ------------------------------------------------------------------- notation
def test_good_trader_scores_high_with_real_average_gain(db):
    score, reason, payload = evaluate_wallet(stats(), activity(), cfg(db), sol_usd=150.0, now=NOW)
    assert reason is None and score >= 70
    assert payload["avg_win_pct"] == pytest.approx(50.0)
    assert payload["median_buy_sol"] == pytest.approx(1.0)
    assert payload["win_rate"] == 0.62 and payload["realized_pnl_sol"] == pytest.approx(40.0)


@pytest.mark.parametrize("kwargs,text", [
    ({"tokens": 4}, "trop peu de tokens"),
    ({"buys": 900, "sells": 900}, "robot"),
    ({"hold": 8}, "trop rapide"),
    ({"realized": -300.0, "roi": -0.2}, "pas gagnant"),
    ({"created": 5}, "créateur"),
])
def test_bad_profiles_are_excluded(db, kwargs, text):
    score, reason, _ = evaluate_wallet(stats(**kwargs), activity(), cfg(db), sol_usd=150.0, now=NOW)
    assert reason is not None and text in reason and score < 60


def test_inactive_trader_is_excluded(db):
    _, reason, _ = evaluate_wallet(stats(), activity(age_s=3 * 86400), cfg(db), sol_usd=150.0, now=NOW)
    assert "24 h" in reason


# -------------------------------------------------------------------- passages
def test_cycle_fills_five_traders_and_skips_bad_ones(db):
    profiles = {addr(i): (stats(winrate=0.55 + i * 0.02), activity()) for i in range(6)}
    profiles[addr(10)] = (stats(buys=2000, sells=2000), activity())     # robot
    profiles[addr(11)] = (stats(realized=-50.0, roi=-0.1), activity())  # perdant
    apply_user_change(db, "discovery", "candidates_per_cycle", 10, actor="dashboard")
    report = run(hunter(db, FakeGmgn(profiles)).cycle(now=NOW))
    roster = db.list_traders(active_only=True)
    assert report.status == "OK" and len(roster) == 5
    assert all(t["source"] == SOURCE_AUTO and t["score"] >= 60 for t in roster)
    assert addr(10) not in {t["address"] for t in roster} and addr(11) not in {t["address"] for t in roster}
    # les meilleurs ont été pris (le moins bon des 6 bons reste candidat)
    assert addr(0) not in {t["address"] for t in roster}
    excluded = {c["address"]: c for c in db.list_candidates()}
    assert excluded[addr(10)]["status"] == "excluded" and "robot" in excluded[addr(10)]["reason"]
    # HEAD lit la note de HUNTER : le JUDGE pourra copier ces traders
    head = HeadAgent(None, db, load_settings(db), usdc_mint="x").cached_report(roster[0]["address"])
    assert head.status in ("OK", "WARN") and head.data["avg_win_pct"] == pytest.approx(50.0)


def test_losing_copies_are_replaced_but_manual_and_open_positions_are_kept(db):
    profiles = {addr(i): (stats(), activity()) for i in range(7)}
    apply_user_change(db, "discovery", "candidates_per_cycle", 10, actor="dashboard")
    db.upsert_trader(WALLET, "à moi")                                   # trader manuel, jamais retiré
    h = hunter(db, FakeGmgn(profiles))
    run(h.cycle(now=NOW))
    roster = [t["address"] for t in db.list_traders(active_only=True)]
    assert WALLET in roster and len(roster) == 5
    loser, holder = [a for a in roster if a != WALLET][:2]
    for _ in range(2):  # deux copies perdantes de « loser »
        db.execute("INSERT INTO positions (token_mint, status, mode, source_wallet, opened_at, closed_at, cost_eur, "
                   "realized_pnl_eur) VALUES (?, 'CLOSED', 'PAPER', ?, ?, ?, 2.0, -0.6)",
                   (TOKEN, loser, (datetime.now(UTC) + timedelta(seconds=1)).isoformat(), NOW.isoformat()))
    db.execute("INSERT INTO positions (token_mint, status, mode, source_wallet, opened_at, cost_eur) "
               "VALUES (?, 'OPEN', 'PAPER', ?, ?, 2.0)", (TOKEN, holder, NOW.isoformat()))
    db.execute("UPDATE traders SET score = 10 WHERE address = ?", (holder,))  # mauvais, mais copie ouverte
    changes = h.rotate(NOW + timedelta(minutes=2))
    roster = [t["address"] for t in db.list_traders(active_only=True)]
    assert loser not in roster and holder in roster and WALLET in roster and len(roster) == 5
    assert any("copies perdantes" in c for c in changes)
    assert db.get_candidate(loser)["status"] == "dropped"
    # écarté : pas repris avant la fin de la mise à l'écart
    run(h.cycle(now=NOW + timedelta(minutes=4)))
    assert loser not in [t["address"] for t in db.list_traders(active_only=True)]


def test_inactive_trader_is_replaced_after_the_idle_limit(db):
    profiles = {addr(i): (stats(), activity()) for i in range(6)}
    apply_user_change(db, "discovery", "candidates_per_cycle", 10, actor="dashboard")
    apply_user_change(db, "discovery", "target_traders", 1, actor="dashboard")
    h = hunter(db, FakeGmgn(profiles))
    run(h.cycle(now=NOW))
    first = db.list_traders(active_only=True)[0]["address"]
    later = NOW + timedelta(minutes=90)
    db.execute("UPDATE traders SET stats_json = json_set(stats_json, '$.last_trade_at', ?) WHERE address = ?",
               (int(NOW.timestamp()) - 7200, first))
    changes = h.rotate(later)
    assert any("inactif" in c for c in changes)
    roster = db.list_traders(active_only=True)
    assert len(roster) == 1 and roster[0]["address"] != first


def test_clearly_better_candidate_replaces_the_weakest(db):
    profiles = {addr(i): (stats(winrate=0.5), activity()) for i in range(2)}
    apply_user_change(db, "discovery", "target_traders", 2, actor="dashboard")
    h = hunter(db, FakeGmgn(profiles))
    run(h.cycle(now=NOW))
    before = {t["address"]: t["score"] for t in db.list_traders(active_only=True)}
    profiles[addr(5)] = (stats(winrate=0.75, roi=1.2, realized=30000.0, tokens=60, big=0), activity())
    run(h.cycle(now=NOW + timedelta(minutes=2)))
    after = {t["address"] for t in db.list_traders(active_only=True)}
    assert addr(5) in after and len(after) == 2 and len(set(before) & after) == 1


def test_without_gmgn_key_hunter_explains_what_to_do(db):
    report = run(HunterAgent(None, db, load_settings(db)).cycle())
    assert report.status == "UNAVAILABLE" and "GMGN" in report.summary


def test_api_errors_never_crash_the_cycle(db):
    class Broken(FakeGmgn):
        async def smart_money(self, limit=100):
            raise RuntimeError("GMGN injoignable")

        async def kol(self, limit=100):
            raise RuntimeError("GMGN injoignable")

    report = run(hunter(db, Broken({})).cycle(now=NOW))
    assert report.status == "UNAVAILABLE" and "injoignable" in report.summary
