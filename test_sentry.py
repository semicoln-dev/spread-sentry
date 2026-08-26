"""Synthetic tests — no network, no keys. Run: python test_sentry.py"""
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.broker.options import condor_econ, debit_spread_econ, structure_value
from app.config import settings
from app.risk import gates
from app.strategy.base import Bar, Leg, TradeTicket
from app.strategy.orb_directional import OrbDirectional
from app.strategy.theta_income import ThetaIncome

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 9, 1, 9, 30, tzinfo=NY)  # a Tuesday


def mk(ts, o, h, l, c, v=1000):
    return Bar("SPY", ts, o, h, l, c, v)


def test_orb_ticket():
    s = OrbDirectional()
    for i in range(settings.or_minutes):          # 09:30-10:00: range 640-642
        assert s.on_bar(mk(T0 + timedelta(minutes=i), 641, 642, 640, 641)) is None
    t = s.on_bar(mk(T0 + timedelta(minutes=settings.or_minutes), 642, 643, 642, 642.8))
    assert t is not None and t.structure == "call_debit_spread" and t.direction == "long"
    assert "broke up" in t.thesis and t.params["width"] == settings.orb_spread_width
    assert s.on_bar(mk(T0 + timedelta(minutes=40), 643, 644, 643, 643.5)) is None  # one shot
    s2 = OrbDirectional()
    for i in range(settings.or_minutes):
        s2.on_bar(mk(T0 + timedelta(minutes=i), 641, 642, 640, 641))
    t2 = s2.on_bar(mk(T0 + timedelta(minutes=settings.or_minutes), 640, 640, 639, 639.4))
    assert t2 is not None and t2.structure == "put_debit_spread" and t2.direction == "short"
    print("PASS orb: breakout -> debit spread ticket, one shot per day, both directions")


def test_theta_ticket():
    orb = OrbDirectional()
    for i in range(settings.or_minutes):          # narrow OR: 641-641.6
        orb.on_bar(mk(T0 + timedelta(minutes=i), 641.3, 641.6, 641.0, 641.3))
    orb.on_bar(mk(T0 + timedelta(minutes=settings.or_minutes), 641.3, 641.5, 641.1, 641.3))
    th = ThetaIncome(orb, daily_atr=4.0)          # 0.6 / 4.0 = 0.15x -> range day
    late = T0.replace(hour=11, minute=35)
    t = th.on_bar(mk(late, 641.2, 641.4, 641.0, 641.2))
    assert t is not None and t.structure == "iron_condor" and "range day" in t.thesis
    assert th.on_bar(mk(late + timedelta(minutes=1), 641, 641, 641, 641)) is None
    orb.fired = True                              # directional day: theta stands down
    th2 = ThetaIncome(orb, daily_atr=4.0)
    assert th2.on_bar(mk(late, 641, 641, 641, 641)) is None
    print("PASS theta: condor on range day only, stands down when ORB fired")


def test_economics():
    cost, risk, gain = debit_spread_econ(3.10, 1.05, 5.0)
    assert (cost, risk, gain) == (2.05, 205.0, 295.0)
    cost, risk, gain = condor_econ(1.20, 5.0)
    assert (cost, risk, gain) == (-1.2, 380.0, 120.0)
    assert structure_value([("buy", 3.10), ("sell", 1.05)]) == 2.05
    assert structure_value([("sell", 0.8), ("buy", 0.2), ("sell", 0.7), ("buy", 0.15)]) == -1.15
    print("PASS economics: spread/condor math and structure marks")


def _ticket(structure="call_debit_spread", risk=200.0, legs=None):
    t = TradeTicket(strategy="test", underlying="SPY", structure=structure,
                    direction="long", thesis="test", ts=T0.replace(hour=11))
    t.legs = legs if legs is not None else [
        Leg("SPY260904C00641000", "buy", "call", 641, "2026-09-04"),
        Leg("SPY260904C00646000", "sell", "call", 646, "2026-09-04")]
    t.est_cost_per_contract = 2.0
    t.max_risk_per_contract = risk
    t.max_gain_per_contract = 300.0
    return t


def test_gates():
    now = T0.replace(hour=11)
    t = _ticket()
    t.qty = gates.size_for_cap(t)                 # 2000 / 200 = 10
    assert t.qty == 10
    ok, why = gates.check(t, 0, 0.0, now)
    assert ok, why

    naked = _ticket(legs=[Leg("SPY260904C00646000", "sell", "call", 646, "2026-09-04")])
    naked.qty = 1
    ok, why = gates.check(naked, 0, 0.0, now)
    assert not ok and "defined-risk" in why

    big = _ticket(risk=2500.0)                    # one contract over the cap
    big.qty = 1
    ok, why = gates.check(big, 0, 0.0, now)
    assert not ok and "exceeds per-trade cap" in why
    assert gates.size_for_cap(big) == 0

    ok, why = gates.check(t, 0, -settings.daily_loss_halt_usd, now)
    assert not ok and "daily halt" in why
    ok, why = gates.check(t, settings.max_open_positions, 0.0, now)
    assert not ok and "max open positions" in why
    ok, why = gates.check(t, 0, 0.0, T0.replace(hour=15, minute=45))
    assert not ok and "no entries after" in why

    assert gates.size_for_cap(t, 0.5) == 5        # AI can halve...
    assert gates.size_for_cap(t, 2.0) == 10       # ...but never exceed the cap
    print("PASS gates: naked/oversize/halt/positions/window blocked, AI can only shrink")


def test_grader_failsafe():
    import sys
    from app.ai import grader
    saved = settings.anthropic_api_key
    settings.anthropic_api_key = ""
    g = grader.grade(_ticket(), {"day_pnl_usd": 0})
    assert g.verdict == "take" and g.size_frac == 0.5 and g.model == "fallback"
    # simulate a broken AI layer without touching the network: poison the import
    settings.anthropic_api_key = "sk-set-but-sdk-broken"
    sys.modules["anthropic"] = None
    try:
        g2 = grader.grade(_ticket(), {"day_pnl_usd": 0})
    finally:
        del sys.modules["anthropic"]
        settings.anthropic_api_key = saved
    assert g2.verdict == "veto" and g2.size_frac == 0.0 and g2.model == "error"
    print("PASS grader: no key -> half-size fallback; AI failure -> fail-closed veto")


def test_journal_r_math():
    from app.journal import db
    settings.db_path = os.path.join(tempfile.gettempdir(), "sentry_test.db")
    if os.path.exists(settings.db_path):
        os.remove(settings.db_path)
    db.init_db()
    t = _ticket()                                  # debit 2.00, risk 200, gain 300
    t.qty = 5
    did = db.log_decision(t, None, True, "test", "submitted")
    tid = db.open_trade(did, t, "order-1")
    db.close_trade(tid, "2026-09-01T19:00:00+00:00", 3.5, "target")  # +1.50/contract
    row = [r for r in db.recent("trades") if r["id"] == tid][0]
    assert row["pnl_usd"] == 750.0 and row["r_multiple"] == 0.75, row
    t2 = _ticket()
    t2.structure, t2.est_cost_per_contract = "iron_condor", -1.2   # credit 1.20
    t2.max_risk_per_contract, t2.qty = 380.0, 2
    tid2 = db.open_trade(db.log_decision(t2, None, True, "t", "submitted"), t2, "o2")
    db.close_trade(tid2, "2026-09-01T19:30:00+00:00", -0.6, "target")  # kept half credit
    row2 = [r for r in db.recent("trades") if r["id"] == tid2][0]
    assert row2["pnl_usd"] == 120.0 and row2["r_multiple"] == 0.16, row2
    assert len(db.analysis()) == 1
    print("PASS journal: R-multiples exact for debit win and credit close")


if __name__ == "__main__":
    test_orb_ticket()
    test_theta_ticket()
    test_economics()
    test_gates()
    test_grader_failsafe()
    test_journal_r_math()
    print("All tests passed.")
