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


def test_without_any_source_hunter_explains_what_to_do(db):
    report = run(HunterAgent(None, db, load_settings(db)).cycle())
    assert report.status == "UNAVAILABLE" and "source" in report.summary


def test_api_errors_never_crash_the_cycle(db):
    class Broken(FakeGmgn):
        async def smart_money(self, limit=100):
            raise RuntimeError("GMGN injoignable")

        async def kol(self, limit=100):
            raise RuntimeError("GMGN injoignable")

    report = run(hunter(db, Broken({})).cycle(now=NOW))
    assert report.status == "UNAVAILABLE" and "injoignable" in report.summary


# ------------------------------------------------------------ source blockchain (sans clé)
from tests.test_agents import buy_tx, pair, sell_tx  # noqa: E402

DAY = 86400


class ChainRpc:
    def __init__(self):
        self.txs: dict = {}
        self.sigs: dict = {}

    async def get_signatures_for_address(self, address, limit=100, before=None, **_):
        items = self.sigs.get(address, [])
        if before:
            items = items[[s["signature"] for s in items].index(before) + 1:]
        return items[:limit]

    async def get_transaction(self, signature, **_):
        return self.txs.get(signature)


class ChainJupiter(FakeJupiter):
    async def top_tokens(self, category, interval, limit=20):
        return [{"id": SOL, "liquidity": 1e9}, {"id": TOKEN, "liquidity": 90_000.0},
                {"id": "Tiny1111111111111111111111111111111111111111", "liquidity": 500.0}]


class ChainDex:
    async def best_pair(self, mint):
        return pair() if mint == TOKEN else None


def chain_world():
    """Un bon trader régulier, un robot hyperactif, un petit wallet et un micro-acheteur."""
    rpc = ChainRpc()
    good, bot, small, tiny = addr(20), addr(21), addr(22), addr(23)
    now = int(datetime.now(UTC).timestamp())
    pool = []
    for i, (wallet, sol) in enumerate(((good, 1_000_000_000), (bot, 500_000_000), (small, 300_000_000),
                                       (tiny, 50_000_000))):
        sig = f"pool{i}"
        rpc.txs[sig] = buy_tx(sig, wallet=wallet, sol=sol, ts=now - 60)
        pool.append({"signature": sig, "err": None, "blockTime": now - 60})
    rpc.sigs[TOKEN] = pool
    # historique du bon trader : 36 trades sur ~35 jours, 75 % gagnants
    start = now - int(35.5 * DAY)
    history = []
    for i in range(36):
        mint = f"Mint{i:040d}"[:44]
        b, s = f"g{i}b", f"g{i}s"
        rpc.txs[b] = buy_tx(b, wallet=good, mint=mint, ts=start + i * DAY)
        rpc.txs[s] = sell_tx(s, wallet=good, mint=mint, sol=1_400_000_000 if i % 4 else 800_000_000,
                             ts=start + i * DAY + 3600)
        history += [{"signature": s, "err": None, "blockTime": start + i * DAY + 3600},
                    {"signature": b, "err": None, "blockTime": start + i * DAY}]
    rpc.sigs[good] = list(reversed(history))
    rpc.sigs[bot] = [{"signature": f"x{i}", "err": None, "blockTime": now - i * 300} for i in range(300)]
    rpc.sigs[small] = [{"signature": f"y{i}", "err": None, "blockTime": now - i * DAY} for i in range(10)]
    return rpc, good, bot, small, tiny


def test_onchain_search_finds_and_checks_traders_without_any_key(db):
    rpc, good, bot, small, tiny = chain_world()
    apply_user_change(db, "discovery", "onchain_evals_per_cycle", 5, actor="dashboard")
    h = HunterAgent(None, db, load_settings(db), jupiter=ChainJupiter(), rpc=rpc, dex=ChainDex())
    assert h.source == "onchain"
    report = run(h.cycle())
    assert report.data["source"] == "onchain" and report.data["discovered"] == 3   # micro-achat ignoré
    assert db.get_candidate(tiny) is None
    roster = db.list_traders(active_only=True)
    assert [t["address"] for t in roster] == [good] and roster[0]["source"] == SOURCE_AUTO
    assert roster[0]["score"] >= 60 and roster[0]["stats"]["avg_win_pct"] is not None
    assert "trop actif" in db.get_candidate(bot)["reason"]
    assert "pas assez" in db.get_candidate(small)["reason"]
    assert report.status == "WARN"                         # 1/5 : la recherche continue
    # passage suivant 2 min plus tard : pas de nouveau scan (toutes les 15 min), rien à réévaluer
    report2 = run(h.cycle(now=datetime.now(UTC) + timedelta(minutes=2)))
    assert report2.data["discovered"] is None and report2.data["evaluated"] == []


def test_source_selection(db):
    settings = load_settings(db)
    assert HunterAgent(FakeGmgn({}), db, settings, jupiter=ChainJupiter(), rpc=ChainRpc(), dex=ChainDex()).source == "gmgn"
    apply_user_change(db, "discovery", "source", "onchain", actor="dashboard")
    settings = load_settings(db)
    assert HunterAgent(FakeGmgn({}), db, settings, jupiter=ChainJupiter(), rpc=ChainRpc(), dex=ChainDex()).source == "onchain"
    assert HunterAgent(None, db, settings).source is None
    assert run(HunterAgent(None, db, settings).cycle()).status == "UNAVAILABLE"


def test_onchain_search_finds_app_users_whose_fees_are_paid_by_a_relayer(db):
    """Cas réel observé : achat en USDC d'un utilisateur d'application ; le relais paie les frais."""
    from tests.conftest import USDC
    from tests.test_swap_parser import make_tx, tb
    rpc = ChainRpc()
    relayer, user = addr(30), addr(31)
    now = int(datetime.now(UTC).timestamp())
    tx = make_tx([relayer, user, "usdc_ata", "tok_ata"], [5_000_000_000, 0, 0, 0], [5_000_000_000 - 5000, 0, 0, 0],
                 pre_tok=[tb(2, USDC, user, 100_000_000, 6)],
                 post_tok=[tb(2, USDC, user, 50_000_000, 6), tb(3, TOKEN, user, 4_893_000_000, 6)], sig="app1")
    tx["blockTime"] = now - 30
    rpc.txs["app1"] = tx
    rpc.sigs[TOKEN] = [{"signature": "app1", "err": None, "blockTime": now - 30}]
    h = HunterAgent(None, db, load_settings(db), jupiter=ChainJupiter(), rpc=rpc, dex=ChainDex())
    found = run(h.scan_onchain(datetime.now(UTC)))
    assert found == 1                                   # 50 USDC à 150 $/SOL = 0,33 SOL >= 0,2
    assert db.get_candidate(user)["status"] == "new"
    assert db.get_candidate(relayer) is None            # le relais n'est pas un trader
