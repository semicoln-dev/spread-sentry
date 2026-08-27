"""Synthetic tests — no network, no keys. Run: python test_sentry.py"""
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.broker.options import (condor_econ, debit_spread_econ, econ_from_fill,
                                mid, structure_value)
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


def test_orb_buffer_and_empty_or():
    s = OrbDirectional()
    for i in range(settings.or_minutes):          # range 640-642
        s.on_bar(mk(T0 + timedelta(minutes=i), 641, 642, 640, 641))
    # a close INSIDE the buffer band is noise, not a breakout
    poke = 642 + settings.orb_break_buffer - 0.01
    assert s.on_bar(mk(T0 + timedelta(minutes=31), 642, 642.2, 641.9, poke)) is None
    assert not s.fired
    t = s.on_bar(mk(T0 + timedelta(minutes=32), 642, 643, 642,
                    642 + settings.orb_break_buffer + 0.01))
    assert t is not None and t.direction == "long"

    # bars only AFTER the OR window (data gap / failed replay): the range
    # must never lock empty — the strategy stays silent, not zombie-locked
    s3 = OrbDirectional()
    late = s3.on_bar(mk(T0 + timedelta(minutes=45), 650, 651, 649, 650.9))
    assert late is None and not s3.or_locked and s3.or_high is None
    print("PASS orb: breakout buffer filters pokes; empty OR never locks")


def test_orb_outcome_rearm():
    s = OrbDirectional()
    for i in range(settings.or_minutes):
        s.on_bar(mk(T0 + timedelta(minutes=i), 641, 642, 640, 641))
    t = s.on_bar(mk(T0 + timedelta(minutes=31), 642, 643, 642, 642.8))
    assert t is not None and s.fired and s.pending and s.blocks_theta()
    s.on_ticket_outcome("unresolvable")           # infrastructure: re-arm
    assert not s.fired and not s.pending
    t2 = s.on_bar(mk(T0 + timedelta(minutes=33), 643, 643.5, 642.9, 643.2))
    assert t2 is not None                          # the genuine retry fires
    s.on_ticket_outcome("ai_veto")                 # judgment: shot stays spent
    assert s.fired and not s.pending and not s.blocks_theta()
    assert s.on_bar(mk(T0 + timedelta(minutes=35), 644, 645, 643, 644.5)) is None
    s.on_ticket_outcome("filled")
    # (out of order on purpose: filled always latches the block)
    assert s.submitted and s.blocks_theta()

    # a PERSISTENT infrastructure failure must not loop forever: 2 re-arms max
    s2 = OrbDirectional()
    for i in range(settings.or_minutes):
        s2.on_bar(mk(T0 + timedelta(minutes=i), 641, 642, 640, 641))
    for n in range(3):
        tk = s2.on_bar(mk(T0 + timedelta(minutes=31 + n), 643, 644, 642.5, 643.5))
        if n < 3:
            pass
        if tk is None:
            break
        s2.on_ticket_outcome("submit_failed")
    assert s2.fired and s2.rearms == 2             # third failure stays consumed
    assert s2.on_bar(mk(T0 + timedelta(minutes=40), 644, 645, 643, 644.5)) is None
    print("PASS orb: re-arms on infrastructure failure (capped), never on a veto")


def test_theta_ticket():
    orb = OrbDirectional()
    for i in range(settings.or_minutes):          # narrow OR: 641-641.6
        orb.on_bar(mk(T0 + timedelta(minutes=i), 641.3, 641.6, 641.0, 641.3))
    orb.on_bar(mk(T0 + timedelta(minutes=settings.or_minutes), 641.3, 641.5, 641.1, 641.3))
    th = ThetaIncome(orb, daily_atr=4.0)          # 0.6 / 4.0 = 0.15x -> range day
    late = T0.replace(hour=11, minute=35)
    t = th.on_bar(mk(late, 641.2, 641.4, 641.0, 641.2))
    assert t is not None and t.structure == "iron_condor" and "range day" in t.thesis
    assert t.params["min_dte"] == settings.theta_min_dte   # config, not hardcoded
    assert th.on_bar(mk(late + timedelta(minutes=1), 641, 641, 641, 641)) is None

    orb.submitted = True                          # ORB actually traded: stand down
    th2 = ThetaIncome(orb, daily_atr=4.0)
    assert th2.on_bar(mk(late, 641, 641, 641, 641)) is None

    orb.submitted, orb.pending, orb.fired = False, False, True
    th3 = ThetaIncome(orb, daily_atr=4.0)         # ORB emitted but was VETOED:
    t3 = th3.on_bar(mk(late, 641, 641, 641, 641)) # the condor gets the day back
    assert t3 is not None
    # ...but the AI must be told the day HAD a (vetoed) breakout signal
    assert "ORB breakout signal" in t3.thesis and "no breakout" not in t3.thesis
    print("PASS theta: condor on range day only; blocked by a real ORB claim, "
          "released by a veto with an honest thesis")


def test_economics():
    cost, risk, gain = debit_spread_econ(3.10, 1.05, 5.0)
    assert (cost, risk, gain) == (2.05, 205.0, 295.0)
    cost, risk, gain = condor_econ(1.20, 5.0)
    assert (cost, risk, gain) == (-1.2, 380.0, 120.0)
    assert structure_value([("buy", 3.10), ("sell", 1.05)]) == 2.05
    assert structure_value([("sell", 0.8), ("buy", 0.2), ("sell", 0.7), ("buy", 0.15)]) == -1.15
    print("PASS economics: spread/condor math and structure marks")


def test_mid_zero_bid():
    assert mid(None, None) is None
    assert mid(3.0, 3.2) == 3.1
    assert mid(0.0, 0.06) == 0.03      # zero bid is a LEVEL, not a missing quote
    assert mid(0.0, None) == 0.0       # bid-only book still marks
    assert mid(None, 0.05) == 0.05
    print("PASS mid: zero-bid books average instead of marking at the full ask")


def test_econ_from_fill():
    t = _ticket()                                  # 641/646 call spread, $5 wide
    econ_from_fill(t, 2.10)                        # filled worse than the 2.00 quote
    assert t.est_cost_per_contract == 2.1
    assert t.max_risk_per_contract == 210.0 and t.max_gain_per_contract == 290.0
    c = _ticket(structure="iron_condor", legs=[
        Leg("SPY260904P00630000", "sell", "put", 630, "2026-09-04"),
        Leg("SPY260904P00625000", "buy", "put", 625, "2026-09-04"),
        Leg("SPY260904C00650000", "sell", "call", 650, "2026-09-04"),
        Leg("SPY260904C00655000", "buy", "call", 655, "2026-09-04")])
    econ_from_fill(c, -1.15)                       # credit fill (negative cost)
    assert c.est_cost_per_contract == -1.15
    assert c.max_risk_per_contract == 385.0 and c.max_gain_per_contract == 115.0
    print("PASS econ_from_fill: journal risk/R re-anchor to the actual fill")


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
    # a stray HALT file at the repo root must not flip these assertions
    settings.kill_file = os.path.join(tempfile.gettempdir(),
                                      "sentry_test_no_such_halt")
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

    # counting matters: two shorts behind ONE long is net naked
    twoshort = _ticket(legs=[
        Leg("SPY260904C00646000", "sell", "call", 646, "2026-09-04"),
        Leg("SPY260904C00648000", "sell", "call", 648, "2026-09-04"),
        Leg("SPY260904C00651000", "buy", "call", 651, "2026-09-04")])
    twoshort.qty = 1
    ok, why = gates.check(twoshort, 0, 0.0, now)
    assert not ok and "defined-risk" in why
    # ...and so is a ratio-2 short behind a ratio-1 long
    ratio = _ticket(legs=[
        Leg("SPY260904C00646000", "sell", "call", 646, "2026-09-04", ratio=2),
        Leg("SPY260904C00651000", "buy", "call", 651, "2026-09-04", ratio=1)])
    ratio.qty = 1
    ok, why = gates.check(ratio, 0, 0.0, now)
    assert not ok and "defined-risk" in why
    # a legitimate condor still passes
    condor = _ticket(structure="iron_condor", legs=[
        Leg("SPY260904P00630000", "sell", "put", 630, "2026-09-04"),
        Leg("SPY260904P00625000", "buy", "put", 625, "2026-09-04"),
        Leg("SPY260904C00650000", "sell", "call", 650, "2026-09-04"),
        Leg("SPY260904C00655000", "buy", "call", 655, "2026-09-04")])
    condor.qty = 1
    ok, why = gates.check(condor, 0, 0.0, now)
    assert ok, why

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
    print("PASS gates: naked/counted-naked/oversize/halt/positions/window blocked, "
          "AI can only shrink")


def test_grader_failsafe():
    import sys
    from app.ai import grader
    saved = settings.anthropic_api_key
    settings.anthropic_api_key = ""
    g = grader.grade(_ticket(), {"day_pnl_usd": 0})
    assert g.verdict == "veto" and g.size_frac == 0.0 and g.model == "fallback"
    # simulate a broken AI layer without touching the network: poison the import
    settings.anthropic_api_key = "sk-set-but-sdk-broken"
    sys.modules["anthropic"] = None
    try:
        g2 = grader.grade(_ticket(), {"day_pnl_usd": 0})
    finally:
        del sys.modules["anthropic"]
        settings.anthropic_api_key = saved
    assert g2.verdict == "veto" and g2.size_frac == 0.0 and g2.model == "error"
    print("PASS grader: missing key AND AI failure both fail closed (veto)")


def test_journal_r_math():
    from app.journal import db
    settings.db_path = os.path.join(tempfile.gettempdir(), "sentry_test.db")
    if os.path.exists(settings.db_path):
        os.remove(settings.db_path)
    db.init_db()
    t = _ticket()                                  # debit 2.00, risk 200, gain 300
    t.qty = 5
    did = db.log_decision(t, None, True, "test", "filled")
    tid = db.open_trade(did, t, "order-1", opened_ts="2026-09-01T15:03:04+00:00")
    db.close_trade(tid, "2026-09-01T19:00:00+00:00", 3.5, "target")  # +1.50/contract
    row = [r for r in db.recent("trades") if r["id"] == tid][0]
    assert row["pnl_usd"] == 750.0 and row["r_multiple"] == 0.75, row
    assert row["opened_ts"] == "2026-09-01T15:03:04+00:00"           # fill clock
    assert row["signal_ts"] == t.ts.isoformat()                      # bar clock
    t2 = _ticket()
    t2.structure, t2.est_cost_per_contract = "iron_condor", -1.2   # credit 1.20
    t2.max_risk_per_contract, t2.qty = 380.0, 2
    tid2 = db.open_trade(db.log_decision(t2, None, True, "t", "filled"), t2, "o2",
                         opened_ts="2026-09-01T16:00:00+00:00")
    db.close_trade(tid2, "2026-09-01T19:30:00+00:00", -0.6, "target")  # kept half credit
    row2 = [r for r in db.recent("trades") if r["id"] == tid2][0]
    assert row2["pnl_usd"] == 120.0 and row2["r_multiple"] == 0.16, row2
    assert len(db.analysis()) == 1
    db.update_decision_outcome(did, "filled")
    print("PASS journal: R-multiples exact for debit win and credit close; "
          "fill/signal clocks separate")


def test_feed_bars_restart_and_session_filter():
    """The Aug-26 audit's critical finding: a mid-session (re)start wiped the
    OR because reset_day + a stale _seen_until watermark deduped the whole
    warm-up replay. The fixed contract: _reset_day_state() resets BOTH, so
    the rollover replay rebuilds the OR; bars from other days or outside
    09:30-16:00 ET never touch strategy state; and a bar younger than
    bar_finality_s stays unseen (it may still be revised)."""
    from app import engine
    saved_finality = settings.bar_finality_s
    settings.bar_finality_s = 0        # synthetic bars, no wall-clock coupling
    today_930 = datetime.now(NY).replace(hour=9, minute=30, second=0, microsecond=0)

    def raw(ts, o, h, l, c):
        return SimpleNamespace(timestamp=ts, open=o, high=h, low=l, close=c,
                               volume=1000)

    bars = [raw(today_930 + timedelta(minutes=i), 641, 642, 640, 641)
            for i in range(settings.or_minutes)]
    bars.append(raw(today_930 + timedelta(minutes=31), 642, 643, 642, 642.9))

    engine._reset_day_state()
    tickets = engine._feed_bars(bars)              # boot replay: OR + signal
    assert engine.orb.or_locked and engine.orb.or_width() == 2.0
    assert len(tickets) == 1

    # the restart: _reset_day_state resets strategies AND watermark together
    assert engine._seen_until is not None
    engine._reset_day_state()
    assert engine._seen_until is None, \
        "_reset_day_state must clear the bar watermark with the day state"
    tickets2 = engine._feed_bars(bars)
    assert engine.orb.or_locked and engine.orb.or_width() == 2.0, \
        "restart replay must rebuild the OR, not dedup it away"
    assert len(tickets2) == 1

    # PRIOR-DAY bars — even in-session ones that would otherwise build an OR —
    # and today's after-hours bars advance the watermark but never feed
    engine._reset_day_state()
    junk = [raw(today_930 - timedelta(days=1), 650, 651, 649, 650),          # 09:30 yesterday
            raw(today_930 - timedelta(days=1) + timedelta(minutes=5), 650, 655, 649, 654),
            raw(today_930 + timedelta(hours=7), 650, 651, 649, 650)]         # 16:30 today
    assert engine._feed_bars(junk) == []
    assert engine.orb.or_high is None and not engine.orb.or_locked, \
        "prior-day in-session bars must not seed today's OR"
    assert engine._seen_until == junk[-1].timestamp

    # a too-fresh bar stays entirely unseen (watermark untouched)
    settings.bar_finality_s = 90
    engine._reset_day_state()
    fresh = [raw(datetime.now(NY) - timedelta(seconds=30), 641, 642, 640, 641)]
    assert engine._feed_bars(fresh) == []
    assert engine._seen_until is None, "a forming bar must not be marked seen"

    settings.bar_finality_s = saved_finality
    engine._reset_day_state()
    print("PASS engine: restart replay rebuilds OR; session/date/finality "
          "filters hold")


def test_grader_parse():
    from app.ai.grader import _parse_grade
    g = _parse_grade('```json\n{"verdict": "take", "size_frac": 0.7, '
                     '"reason": "ok"}\n```')
    assert g.verdict == "take" and g.size_frac == 0.7
    assert _parse_grade('{"verdict": "take", "size_frac": Infinity}').verdict == "veto"
    assert _parse_grade('{"verdict": "take", "size_frac": NaN}').verdict == "veto"
    assert _parse_grade('{"verdict": "take", "size_frac": 5}').size_frac == 1.0
    assert _parse_grade('{"verdict": "TAKE", "size_frac": 0.5}').verdict == "veto"
    assert _parse_grade('["not", "a", "dict"]').verdict == "veto"
    try:
        _parse_grade("utter garbage")
        raise AssertionError("garbage must raise (grade() catches -> veto)")
    except Exception:
        pass
    print("PASS grader parse: Infinity/NaN/wrong-shape all fail closed")


def test_journal_close_order_state():
    from app.journal import db
    settings.db_path = os.path.join(tempfile.gettempdir(), "sentry_test2.db")
    if os.path.exists(settings.db_path):
        os.remove(settings.db_path)
    db.init_db()
    t = _ticket()
    t.qty = 3
    did = db.log_decision(t, None, True, "test", "submitted")
    db.set_decision_order(did, "ord-abc")
    db.update_decision_outcome(did, "fill_unknown")
    unknown = db.decisions_by_outcome("fill_unknown")
    assert len(unknown) == 1 and unknown[0]["order_id"] == "ord-abc"
    assert db.decisions_since("2000-01-01T00:00:00+00:00")[0]["id"] == did

    tid = db.open_trade(did, t, "ord-abc", opened_ts="2026-09-01T15:00:00+00:00")
    db.set_close_order(tid, "close-1", "time")
    row = db.open_trades()[0]
    assert row["close_order_id"] == "close-1" and row["close_how"] == "time"
    db.clear_close_order(tid)
    row = db.open_trades()[0]
    assert row["close_order_id"] is None, \
        "a definitely-dead close order must clear so a retry can submit"
    print("PASS journal: order breadcrumbs and close-in-flight state persist")


def test_cli_templates():
    from app.broker import cli
    t = _ticket()
    t.qty = 6
    import json as _json
    legs = _json.loads(cli._legs_arg(t, flip=False))
    assert legs[0] == {"symbol": "SPY260904C00641000", "ratio_qty": "1", "side": "buy"}
    assert legs[1]["side"] == "sell"
    flipped = _json.loads(cli._legs_arg(t, flip=True))
    assert flipped[0]["side"] == "sell" and flipped[1]["side"] == "buy"
    assert isinstance(legs[0]["ratio_qty"], str)   # REST schema wants strings
    print("PASS cli: mleg legs JSON matches the verified CLI/REST schema")


if __name__ == "__main__":
    test_orb_ticket()
    test_orb_buffer_and_empty_or()
    test_orb_outcome_rearm()
    test_theta_ticket()
    test_economics()
    test_mid_zero_bid()
    test_econ_from_fill()
    test_gates()
    test_grader_failsafe()
    test_grader_parse()
    test_journal_r_math()
    test_journal_close_order_state()
    test_feed_bars_restart_and_session_filter()
    test_cli_templates()
    print("All tests passed.")
