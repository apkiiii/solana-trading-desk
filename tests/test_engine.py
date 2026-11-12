"""Moteur de copy trading de bout en bout, avec un faux réseau (aucun appel réel, aucun argent)."""
from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from config.settings import apply_user_change
from data.models import PriceInfo, SignatureNotice
from engine.copy_trading import CopyTradingEngine
from security.trading_wallet import TradingWalletStore
from tests.conftest import SOL, TOKEN, WALLET
from tests.test_agents import buy_tx, mint_info, pair, quote, rug, sell_tx
from tests.test_trading_wallet import b58encode, unsigned_tx

EURC = "HzwqbKZw8HxMN6bF2yFZNrht3c2iXXzpKcFu7uBEDKtr"


def run(coro):
    return asyncio.run(coro)


def now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


class Rpc:
    def __init__(self):
        self.txs: dict = {}
        self.balance = 10**9
        self.token_balance = 0

    async def get_transaction(self, signature, critical=False, **_):
        return self.txs.get(signature)

    async def get_signatures_for_address(self, address, limit=100, before=None, **_):
        return []

    async def get_mint_info(self, mint):
        return mint_info()

    async def get_token_largest_accounts(self, mint):
        return []

    async def get_balance(self, address):
        return self.balance

    async def get_token_balance(self, owner, mint):
        return self.token_balance


class Jupiter:
    def __init__(self, taker_tx=None):
        self.token_usd = 0.01
        self.sell_ok = True
        self.taker_tx = taker_tx
        self.orders: list = []
        self.executed: list = []

    async def get_prices(self, mints):
        table = {SOL: 150.0, EURC: 1.08, TOKEN: self.token_usd}
        return {m: PriceInfo(m, table[m], 1, 6, None, None) for m in mints if m in table}

    async def get_order(self, input_mint, output_mint, amount, *, taker=None, slippage_bps=None,
                        priority_fee_lamports=None):
        self.orders.append((input_mint, output_mint, amount, taker))
        if input_mint == SOL:   # 1 SOL = 150 $ ; 1 token = token_usd $ ; 6 décimales
            out = int(amount / 1e9 * 150 / self.token_usd * 1e6 * 0.995)
            q = quote(SOL, output_mint, amount, out, impact=0.5)
        else:
            out = int(amount / 1e6 * self.token_usd / 150 * 1e9 * 0.995)
            q = quote(input_mint, SOL, amount, out, impact=0.6, ok=self.sell_ok)
        if taker and self.taker_tx:
            q = SimpleNamespace(**{**q.__dict__, "transaction_b64": self.taker_tx, "request_id": "req-1"})
            q.ok = True
        return q

    async def execute(self, signed, request_id):
        self.executed.append((signed, request_id))
        _, _, amount, _ = self.orders[-1]
        out = 1_234_567 if self.orders[-1][0] == SOL else 14_000_000
        return {"status": "Success", "signature": f"sig{len(self.executed)}", "code": 0,
                "input_amount": amount, "output_amount": out, "error": None}


class Dex:
    def __init__(self):
        self.p = pair()

    async def best_pair(self, mint):
        return self.p


class Rug:
    async def get_report(self, mint):
        return rug()


def make_engine(db, *, jupiter=None, wallet=None):
    providers = SimpleNamespace(rpc=Rpc(), jupiter=jupiter or Jupiter(), dex=Dex(), rug=Rug(), ws_url="wss://x")

    async def no_sleep(_s):
        return None

    return CopyTradingEngine(db, providers, wallet_store=wallet, sleep=no_sleep, wait_retry_seconds=0)


@pytest.fixture
def ready(db):
    """Trader suivi et bien noté, bot démarré en compte démo, planning désactivé (test indépendant de l'heure)."""
    db.upsert_trader(WALLET, "pro")
    db.update_trader_score(WALLET, 78.0, {
        "evaluated_at": datetime.now(UTC).isoformat(), "closed_trades": 40, "win_rate": 0.75,
        "realized_pnl_sol": 10.0, "max_drawdown_pct": 10.0, "avg_win_pct": 40.0, "median_buy_sol": 1.0,
        "score": 78.0, "notes": []})
    apply_user_change(db, "schedule", "enabled", False, actor="dashboard")
    db.set_control(actor="dashboard", bot_state="RUNNING", manual_stop=False, mode="PAPER")
    return db


def notice(sig):
    return SignatureNotice(wallet=WALLET, signature=sig, slot=1, received_at=datetime.now(UTC))


def test_demo_buy_is_judged_then_executed_with_a_real_quote(ready):
    engine = make_engine(ready)
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    outcome = run(engine.handle_notice(notice("b1")))
    assert outcome.stage == "EXECUTED", outcome.detail
    decision = ready.get_decision("BUY:b1")
    assert decision["decision"] == "APPROVED"
    position = ready.open_positions("PAPER")[0]
    assert position["token_mint"] == TOKEN and position["source_signature"] == "b1"
    assert 1.5 <= position["cost_eur"] <= 2.1                       # 2 € maximum + frais réseau
    assert position["entry_price_usd"] == pytest.approx(0.01, rel=0.02)
    execution = ready.query_one("SELECT * FROM executions")
    assert execution["status"] == "CONFIRMED" and execution["mode"] == "PAPER" and execution["position_id"]
    assert engine.providers.jupiter.executed == []                  # compte démo : rien n'est envoyé
    # même transaction reçue deux fois : jamais copiée deux fois
    assert run(engine.handle_notice(notice("b1"))).stage == "IGNORED"
    assert len(ready.open_positions()) == 1


def test_nothing_is_bought_when_stopped_or_in_emergency(ready):
    engine = make_engine(ready)
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    ready.set_control(actor="dashboard", bot_state="STOPPED", manual_stop=True)
    assert run(engine.handle_notice(notice("b1"))).stage == "SKIPPED"
    ready.set_control(actor="dashboard", bot_state="RUNNING", manual_stop=False, emergency_stop=True)
    engine.providers.rpc.txs["b2"] = buy_tx("b2", ts=now_ts() - 3)
    outcome = run(engine.handle_notice(notice("b2")))
    assert outcome.stage == "REJECTED" and "urgence" in outcome.detail.lower()
    assert ready.open_positions() == []


def test_unsellable_token_and_old_transaction_are_rejected(ready):
    jupiter = Jupiter()
    jupiter.sell_ok = False
    engine = make_engine(ready, jupiter=jupiter)
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    assert run(engine.handle_notice(notice("b1"))).stage == "REJECTED"
    engine.providers.rpc.txs["old"] = buy_tx("old", ts=now_ts() - 600)
    outcome = run(engine.handle_notice(notice("old")))
    assert outcome.stage == "REJECTED" and "transaction source" in outcome.detail
    assert ready.open_positions() == []


def test_live_is_refused_unless_armed_in_this_session(ready):
    ready.set_control(actor="dashboard", mode="LIVE")
    engine = make_engine(ready)
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    outcome = run(engine.handle_notice(notice("b1")))
    assert outcome.stage == "REJECTED" and "LIVE non armé" in outcome.detail
    assert ready.open_positions() == []


def test_stop_loss_sells_the_demo_position_and_emergency_suspends_it(ready):
    engine = make_engine(ready)
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    assert run(engine.handle_notice(notice("b1"))).stage == "EXECUTED"
    engine.providers.jupiter.token_usd = 0.006                      # -40 % : stop-loss
    ready.set_control(actor="dashboard", emergency_stop=True)
    result = run(engine.review_exits())
    assert result[0]["stage"] == "WAIT" and len(ready.open_positions()) == 1
    ready.set_control(actor="dashboard", emergency_stop=False, bot_state="STOPPED", manual_stop=True)
    result = run(engine.review_exits())                             # STOPPER n'empêche pas de protéger
    assert result[0]["stage"] == "SOLD" and result[0]["pnl_eur"] < -0.5
    assert ready.open_positions() == []
    assert ready.query_one("SELECT realized_pnl_eur FROM positions")["realized_pnl_eur"] < 0
    assert ready.get_decision("SELL:1")["decision"] == "APPROVED"


def test_trader_sell_is_followed(ready):
    engine = make_engine(ready)
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    assert run(engine.handle_notice(notice("b1"))).stage == "EXECUTED"
    engine.providers.rpc.txs["s1"] = sell_tx("s1", ts=now_ts())
    run(engine.handle_notice(notice("s1")))
    closed = ready.query_one("SELECT * FROM positions")
    assert closed["status"] == "CLOSED"


def test_daily_loss_limit_blocks_new_buys(ready):
    engine = make_engine(ready)
    ready.execute("INSERT INTO positions (token_mint, status, mode, opened_at, closed_at, cost_eur, "
                  "realized_pnl_eur) VALUES (?, 'CLOSED', 'PAPER', ?, ?, 2.0, -1.6)",
                  (TOKEN, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(timespec="seconds")))
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    outcome = run(engine.handle_notice(notice("b1")))
    assert outcome.stage == "REJECTED" and "perte du jour" in outcome.detail


def test_live_buy_and_sell_are_signed_locally_by_the_dedicated_wallet(ready, tmp_path):
    kp = Keypair()
    wallet = TradingWalletStore(tmp_path, use_keyring=False)
    wallet.import_key(b58encode(bytes(kp)))
    jupiter = Jupiter(taker_tx=unsigned_tx(kp.pubkey()))
    engine = make_engine(ready, jupiter=jupiter, wallet=wallet)
    ready.set_control(actor="dashboard", mode="LIVE")
    engine.live_armed = True
    engine.providers.rpc.txs["b1"] = buy_tx("b1", ts=now_ts() - 3)
    outcome = run(engine.handle_notice(notice("b1")))
    assert outcome.stage == "EXECUTED", outcome.detail
    position = ready.open_positions("LIVE")[0]
    assert int(position["amount_raw"]) == 1_234_567
    signed, request_id = jupiter.executed[0]
    assert request_id == "req-1" and signed != jupiter.taker_tx and base64.b64decode(signed)
    assert all(o[3] == str(kp.pubkey()) for o in jupiter.orders if o[3])
    # vente : on revend ce que le wallet détient réellement
    engine.providers.rpc.token_balance = 1_000_000
    jupiter.token_usd = 0.02                                       # +100 % : prise de profit
    result = run(engine.review_exits())
    assert result[0]["stage"] == "SOLD"
    assert jupiter.orders[-1][2] == 1_000_000
    # après un redémarrage (nouveau moteur), le compte réel n'est plus armé
    assert make_engine(ready, wallet=wallet).live_armed is False


def test_run_loop_starts_monitor_and_stops_cleanly(ready):
    seen = {}

    class Monitor:
        def __init__(self, url, wallets, on_notice, rpc=None):
            seen["wallets"] = wallets
            self.on_notice = on_notice

        def seed_last_signatures(self, last):
            seen["seed"] = last

        def set_wallets(self, wallets):
            seen["set"] = wallets

        def status(self):
            return {"connected": True}

        async def run(self, stop):
            await stop.wait()

    engine = make_engine(ready)
    engine.monitor_factory = Monitor
    engine.exit_interval = 0.01

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(engine.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)

    run(go())
    assert seen["wallets"] == [WALLET] and seen["set"] == [WALLET]
    assert engine.status()["monitor"] == {"connected": True}
    assert ready.query_one("SELECT status FROM heartbeats WHERE component = 'engine'")["status"] == "STOPPED"
