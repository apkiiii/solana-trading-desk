"""Tests obligatoires du cahier des charges (JUDGE, STOP, planning, LIVE)."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agents.base import BLOCK, OK, UNAVAILABLE, WARN, AgentReport
from agents.judge import APPROVED, REJECTED, WAIT, Judge, SystemState
from config.settings import load_settings
from data.models import SwapEvent
from engine.scheduler import entries_window_open, next_change
from tests.conftest import SOL, TOKEN, WALLET

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)   # 13:00 à Paris, un jeudi


def state(**overrides) -> SystemState:
    base = SystemState(now=NOW, mode="PAPER", bot_running=True, manual_stop=False, emergency_stop=False,
                       emergency_file=False, entries_window_open=True, live_armed=False, open_positions=0,
                       open_mints=frozenset(), invested_eur=0.0, daily_loss_eur=0.0, fee_reserve_ok=True)
    return replace(base, **overrides)


def rep(agent, status=OK, data=None, reasons=None, summary="ok", age=1):
    return AgentReport(agent, "ref", status, summary, reasons or [], data or {}, 1.0,
                       created_at=NOW - timedelta(seconds=age))


RISK_CHECKS = [
    {"name": "liquidité", "passed": True, "detail": "50 000 $"},
    {"name": "impact prix à l'achat", "passed": True, "detail": "0,4 %"},
    {"name": "revente possible", "passed": True, "detail": "ok"},
    {"name": "impact prix à la revente", "passed": True, "detail": "0,5 %"},
]


def reports(**overrides):
    base = {
        "SCOUT": rep("SCOUT", data={"signature": "src", "wallet": WALLET, "side": "BUY", "token_mint": TOKEN,
                                    "confirmed": True, "age_seconds": 5, "block_time": int(NOW.timestamp()) - 5}),
        "HEAD": rep("HEAD", data={"score": 75.0}, age=3600),
        "RISK": rep("RISK", data={"checks": [dict(c) for c in RISK_CHECKS]}),
        "WHALE": rep("WHALE"),
        "SNIPER": rep("SNIPER", data={"amount_eur": 2.0, "estimated_fees_pct": 1.2, "mint": TOKEN,
                                      "source_signature": "src"}),
    }
    base.update(overrides)
    return base


@pytest.fixture
def judge(db):
    return Judge(db, load_settings(db))


def decide(judge, key="k1", st=None, **report_overrides):
    return judge.evaluate_buy(proposal_key=key, reports=reports(**report_overrides), state=st or state())


# ------------------------------------------------------------- tests obligatoires
def test_valid_buy_is_approved_and_recorded_with_evidence(judge, db):
    decision = decide(judge)
    assert decision.decision == APPROVED, decision.reasons
    assert len(decision.checks) == 14 and all(c["passed"] for c in decision.checks)
    saved = db.get_decision("k1")
    assert saved["decision"] == APPROVED and len(saved["evidence"]["checks"]) == 14
    assert saved["evidence"]["reports"]["RISK"]["status"] == OK


def test_risky_token_is_rejected(judge):
    risky = rep("RISK", BLOCK, {"checks": RISK_CHECKS}, ["autorité de gel : active"])
    decision = decide(judge, RISK=risky)
    assert decision.decision == REJECTED and any("risque du token" in r for r in decision.reasons)


def test_insufficient_liquidity_blocks(judge):
    checks = [dict(c) for c in RISK_CHECKS]
    checks[0] = {"name": "liquidité", "passed": False, "detail": "3 000 $ (minimum 20 000 $)"}
    decision = decide(judge, RISK=rep("RISK", BLOCK, {"checks": checks}))
    assert decision.decision == REJECTED and any("liquidité" in r for r in decision.reasons)


def test_budget_exceeded_blocks(judge):
    assert decide(judge, "a", state(invested_eur=16.0)).decision == REJECTED
    too_big = rep("SNIPER", data={"amount_eur": 5.0, "estimated_fees_pct": 1.0, "mint": TOKEN})
    assert decide(judge, "b", SNIPER=too_big).decision == REJECTED
    assert decide(judge, "c", state(fee_reserve_ok=False)).decision == REJECTED


def test_daily_max_loss_blocks_new_entries(judge):
    decision = decide(judge, st=state(daily_loss_eur=1.5))
    assert decision.decision == REJECTED and any("perte du jour" in r for r in decision.reasons)


def test_duplicate_transaction_is_refused(judge, db):
    assert decide(judge, "dup").decision == APPROVED
    again = decide(judge, "dup")
    assert again.decision == APPROVED and "déjà rendue" in again.reasons[0]   # jamais rejouée
    db.execute("INSERT INTO positions (token_mint, status, mode, source_signature, opened_at) "
               "VALUES (?, 'OPEN', 'PAPER', 'src', ?)", (SOL, NOW.isoformat()))
    copied = decide(judge, "dup2")
    assert copied.decision == REJECTED and any("déjà copiée" in r for r in copied.reasons)


def test_rejection_is_final(judge):
    assert decide(judge, "r", state(manual_stop=True)).decision == REJECTED
    # même avec des conditions devenues parfaites, la proposition reste refusée
    assert decide(judge, "r").decision == REJECTED


def test_unavailable_agent_blocks_then_rejects_after_timeout(judge):
    missing = decide(judge, "w", WHALE=rep("WHALE", UNAVAILABLE, summary="délai dépassé"))
    assert missing.decision == WAIT and not missing.approved
    later = judge.evaluate_buy(proposal_key="w", reports=reports(WHALE=None),
                               state=state(now=NOW + timedelta(seconds=31)))
    assert later.decision == REJECTED and "toujours manquantes" in later.reasons[0]
    assert decide(judge, "w2", HEAD=None).decision == WAIT


def test_stop_button_blocks_new_entries_immediately(judge):
    assert decide(judge, "s1", state(manual_stop=True)).decision == REJECTED
    assert decide(judge, "s2", state(bot_running=False)).decision == REJECTED
    assert decide(judge, "s3", state(emergency_stop=True)).decision == REJECTED
    assert decide(judge, "s4", state(emergency_file=True)).decision == REJECTED
    assert decide(judge, "s5", state(entries_window_open=False)).decision == REJECTED


def test_live_requires_arming_in_the_current_session(judge):
    assert decide(judge, "l1", state(mode="LIVE", live_armed=False)).decision == REJECTED
    assert decide(judge, "l2", state(mode="LIVE", live_armed=True)).decision == APPROVED


def test_other_rules(judge, db):
    assert decide(judge, "o1", state(open_positions=2)).decision == REJECTED
    assert decide(judge, "o2", state(open_mints=frozenset({TOKEN}))).decision == REJECTED
    assert decide(judge, "o3", HEAD=rep("HEAD", BLOCK, {"score": 40})).decision == REJECTED
    old = rep("SCOUT", data={**reports()["SCOUT"].data, "age_seconds": 120})
    assert decide(judge, "o4", SCOUT=old).decision == REJECTED
    assert decide(judge, "o5", WHALE=rep("WHALE", BLOCK, summary="gros vendeurs")).decision == REJECTED
    assert decide(judge, "o6", WHALE=rep("WHALE", WARN, reasons=["achat groupé"])).decision == APPROVED
    expensive = rep("SNIPER", data={"amount_eur": 2.0, "estimated_fees_pct": 9.0, "mint": TOKEN})
    assert decide(judge, "o7", SNIPER=expensive).decision == REJECTED
    assert decide(judge, "o8", RISK=rep("RISK", data={"checks": RISK_CHECKS}, age=300)).decision == REJECTED
    # le trader a déjà revendu
    db.insert_source_event(SwapEvent(signature="sell1", wallet=WALLET, slot=1, block_time=int(NOW.timestamp()),
                                     side="SELL", token_mint=TOKEN, token_amount_raw=1, token_decimals=6,
                                     quote_mint=SOL, quote_amount_raw=1, quote_decimals=9, fee_lamports=0,
                                     fee_paid_by_wallet=True))
    sold = decide(judge, "o9")
    assert sold.decision == REJECTED and any("revendu" in r for r in sold.reasons)


def test_exits_continue_after_stop_and_only_emergency_suspends_them(judge):
    exit_report = AgentReport("EXIT", "1", WARN, "stop-loss", ["stop-loss : -30 %"],
                              {"position_id": 1, "token_mint": TOKEN, "action": "SELL"})
    stopped = state(manual_stop=True, bot_running=False, entries_window_open=False, daily_loss_eur=5)
    assert judge.evaluate_sell(exit_report=exit_report, state=stopped).decision == APPROVED
    exit_report.data["position_id"] = 2
    assert judge.evaluate_sell(exit_report=exit_report, state=state(emergency_stop=True)).decision == WAIT


# ----------------------------------------------------------------------- planning
SCHEDULE = {"enabled": True, "start": "08:00", "stop": "23:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat",
                                                                          "sun"], "timezone": "Europe/Paris"}


def paris(year, month, day, hour, minute=0):
    from zoneinfo import ZoneInfo
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Paris"))


def test_schedule_opens_and_closes_at_the_right_times():
    assert not entries_window_open(SCHEDULE, paris(2026, 11, 12, 7, 59))
    assert entries_window_open(SCHEDULE, paris(2026, 11, 12, 8, 0))
    assert entries_window_open(SCHEDULE, paris(2026, 11, 12, 22, 59))
    assert not entries_window_open(SCHEDULE, paris(2026, 11, 12, 23, 0))
    assert next_change(SCHEDULE, paris(2026, 11, 12, 22, 30)) == paris(2026, 11, 12, 23, 0)


def test_schedule_handles_midnight_days_and_dst():
    night = {**SCHEDULE, "start": "20:00", "stop": "02:00", "days": ["fri"]}
    assert entries_window_open(night, paris(2026, 11, 13, 21, 0))       # vendredi soir
    assert entries_window_open(night, paris(2026, 11, 14, 1, 30))       # nuit de vendredi à samedi
    assert not entries_window_open(night, paris(2026, 11, 14, 21, 0))   # samedi soir : non
    # Passage à l'heure d'été (29 mars 2026) : 8 h locale reste 8 h locale
    assert entries_window_open(SCHEDULE, paris(2026, 3, 29, 8, 0))
    assert not entries_window_open(SCHEDULE, datetime(2026, 3, 29, 5, 30, tzinfo=UTC))   # 7 h 30 à Paris
    assert entries_window_open(SCHEDULE, datetime(2026, 3, 29, 6, 0, tzinfo=UTC))        # 8 h à Paris
    assert entries_window_open({**SCHEDULE, "enabled": False}, paris(2026, 11, 12, 3, 0))
    with pytest.raises(ValueError):
        entries_window_open(SCHEDULE, datetime(2026, 1, 1, 10, 0))
