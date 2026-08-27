"""The autonomous loop: poll -> scan -> grade -> gate -> execute -> manage.

Pipeline per ticket (each stage can kill it, every death is journaled):
    strategy ticket -> broker.resolve (live chain) -> AI risk officer
    -> hard gates -> submit -> CONFIRMED FILL -> managed exits
    (profit / stop / time).

Deliberately REST-polling, no websockets: options decisions here are
minute-scale, and polling keeps the agent trivially restartable (the
orb-trader lesson: design for restarts, they will happen).

Two invariants this file enforces everywhere:
  * An UNKNOWN order state is never treated as a definite one. Unknown
    entries go to a pending queue (strategy stays latched); a close in
    flight is persisted on the trade row and polled — never re-submitted —
    until its fate is definite. Re-closing an already-closed structure
    would OPEN a reverse position.
  * A (re)start reconstructs the day: day state rebuilds exactly once per
    ET day inside the loop (never at import, which double-initialized and
    wiped it), open positions re-adopt from the journal, each strategy's
    daily claims (shot spent? position filled?) restore from today's
    decision rows, and the journal reconciles against the broker's live
    positions at boot.
"""
import asyncio
import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

from app.ai import grader
from app.broker.options import OptionsBroker, econ_from_fill
from app.config import settings
from app.journal import db
from app.risk import gates
from app.strategy.base import Bar, Leg, TradeTicket
from app.strategy.orb_directional import OrbDirectional
from app.strategy.theta_income import ThetaIncome

log = logging.getLogger("sentry.engine")
NY = ZoneInfo("America/New_York")

broker: OptionsBroker | None = None
orb = OrbDirectional()
theta = ThetaIncome(orb)
strategies = [orb, theta]
_seen_until: dt.datetime | None = None
_day: dt.date | None = None
_pending_entries: list[tuple[int, TradeTicket, str]] = []  # (decision_id, ticket, order_id)
_mark_alerted: set[int] = set()          # trade ids alerted for missing marks
_stuck_close_alerted: set[int] = set()   # trade ids alerted for stuck closes


def _alert(text: str):
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            data=json.dumps({"chat_id": settings.telegram_chat_id, "text": text,
                             "parse_mode": "HTML"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        log.exception("telegram alert failed")


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _strategy(name: str):
    for s in strategies:
        if s.name == name:
            return s
    return None


def _outcome(t: TradeTicket, outcome: str):
    """Deliver a ticket's fate back to its strategy (re-arm vs stay consumed)."""
    s = _strategy(t.strategy)
    if s:
        s.on_ticket_outcome(outcome)


# ---------- entry ----------

def handle_ticket(t: TradeTicket, day_pnl: float, equity: float):
    open_rows = db.open_trades()
    other = {orb.name: theta.name, theta.name: orb.name}.get(t.strategy)
    if other and any(r["strategy"] == other for r in open_rows):
        # a condor's short call loses on exactly the move a call spread
        # needs — never hold both sides of that argument, in either order
        db.log_decision(t, None, False,
                        f"conflicting {other} position is open", "skipped_conflict")
        _outcome(t, "skipped_conflict")
        return
    resolved = broker.resolve(t)
    if resolved is None:
        db.log_decision(t, None, False, "no viable contracts/quotes", "unresolvable")
        _outcome(t, "unresolvable")
        return
    g = grader.grade(t, {"day_pnl_usd": day_pnl, "open_positions": len(open_rows),
                         "equity": equity})
    if g.verdict != "take":
        db.log_decision(t, g, False, "not gate-checked (AI veto)", "ai_veto")
        _outcome(t, "ai_veto")
        _alert(f"⛔ VETO [{t.strategy}] {t.structure}\n{t.thesis}\nAI: {g.reason}")
        return
    t.qty = gates.size_for_cap(t, g.size_frac)
    ok, reason = gates.check(t, len(open_rows), day_pnl)
    if not ok:
        db.log_decision(t, g, False, reason, "gate_reject")
        _outcome(t, "gate_reject")
        _alert(f"⛔ GATE [{t.strategy}] {t.structure}\n{t.thesis}\n{reason}")
        return
    # the decision row exists BEFORE the order: a crash mid-submit leaves a
    # breadcrumb (order_id lands right after) for reconcile to chase
    did = db.log_decision(t, g, True, reason, "submitted")
    order_id = broker.submit(t)
    if not order_id:
        db.update_decision_outcome(did, "submit_failed")
        _outcome(t, "submit_failed")
        _alert(f"⚠️ SUBMIT FAILED [{t.strategy}] {t.structure}\n{t.thesis}")
        return
    db.set_decision_order(did, order_id)
    status, avg, fq = broker.wait_fill(order_id)
    if status == "timeout":
        # cancel; if the cancel raced a fill, one more short poll may settle it
        broker.cancel(order_id)
        status, avg, fq = broker.wait_fill(order_id, timeout_s=3)
    _finish_entry(did, t, order_id, status, avg, fq)


def _finish_entry(did: int, t: TradeTicket, order_id: str,
                  status: str, avg: float | None, fq: int):
    """Land an entry order's now-known (or still-unknown) fate."""
    if status == "timeout":
        # UNKNOWN is not 'nothing happened': park it, keep the strategy
        # latched (no hook -> pending stays True: no re-fire, theta blocked),
        # and resolve on later ticks / next boot via the journal breadcrumb
        db.update_decision_outcome(did, "fill_unknown")
        _pending_entries.append((did, t, order_id))
        _alert(f"⚠️ ORDER FATE UNKNOWN [{t.strategy}] {t.structure} — "
               f"order {order_id}: polling until it settles; no re-entry "
               f"until then")
        return
    if status == "filled" or fq > 0:      # canceled-with-partial is a position
        if 0 < fq < t.qty:
            _alert(f"⚠️ PARTIAL FILL [{t.strategy}] {t.structure}: {fq}/{t.qty} "
                   f"— managing the {fq} we own")
            t.qty = fq
        if avg is not None:
            econ_from_fill(t, avg)        # journal what HAPPENED, not the quote
        else:
            _alert(f"⚠️ fill price unavailable for {order_id} — journaling "
                   f"quote-based economics")
        total_risk = t.qty * t.max_risk_per_contract
        if total_risk > settings.max_risk_per_trade_usd:
            _alert(f"⚠️ fill slippage: risk ${total_risk:.0f} exceeds the "
                   f"${settings.max_risk_per_trade_usd:.0f} cap post-fill")
        try:
            db.update_decision_outcome(did, "filled")
            db.open_trade(did, t, order_id, opened_ts=_now_utc())
        except Exception:
            log.exception("journal write failed after a confirmed fill")
            _alert(f"🚨 JOURNAL WRITE FAILED for filled order {order_id} — "
                   f"position is LIVE; boot reconcile will re-adopt it")
        _outcome(t, "filled")
        _alert(f"✅ OPEN [{t.strategy}] {t.structure} x{t.qty}"
               f"{f' @ fill {avg:.2f}' if avg is not None else ''}\n{t.thesis}\n"
               f"risk ${t.qty * t.max_risk_per_contract:.0f}")
        return
    # definite dead state, nothing filled
    db.update_decision_outcome(did, "entry_rejected")
    _outcome(t, "entry_rejected")
    _alert(f"⚠️ ENTRY NOT FILLED ({status}) [{t.strategy}] {t.structure} — "
           f"order {order_id}: no position")


def _resolve_pending_entries():
    for item in list(_pending_entries):
        did, t, oid = item
        status, avg, fq = broker.wait_fill(oid, timeout_s=2)
        if status == "timeout":
            continue                       # still unknown; stay parked
        _pending_entries.remove(item)
        _finish_entry(did, t, oid, status, avg, fq)


# ---------- open positions ----------

def _ticket_from_row(row: dict) -> TradeTicket:
    t = TradeTicket(strategy=row["strategy"], underlying=settings.underlying,
                    structure=row["structure"], direction=row["direction"],
                    thesis="", qty=row["qty"])
    t.legs = [Leg(**l) for l in json.loads(row["legs"])]
    t.est_cost_per_contract = row["est_cost"]
    t.max_risk_per_contract = row["max_risk"]
    t.max_gain_per_contract = row["max_gain"]
    return t


def _ticket_from_decision(row: dict) -> TradeTicket:
    t = TradeTicket(strategy=row["strategy"], underlying=settings.underlying,
                    structure=row["structure"], direction=row["direction"],
                    thesis=row["thesis"] or "", qty=row["qty"] or 0)
    t.legs = [Leg(**l) for l in json.loads(row["legs"] or "[]")]
    t.est_cost_per_contract = row["est_cost"] or 0.0
    t.max_risk_per_contract = row["max_risk"] or 0.0
    t.max_gain_per_contract = row["max_gain"] or 0.0
    return t


def _journal_close(row: dict, t: TradeTicket, avg: float | None, how: str,
                   mark: float | None):
    if avg is not None:
        exit_value = round(-avg, 2)   # the close order flips sides, so its
                                      # fill sign mirrors the entry's
    else:
        exit_value = mark if mark is not None else t.est_cost_per_contract
        _alert(f"⚠️ close fill price unavailable — journaling "
               f"{'mark' if mark is not None else 'entry cost'} instead")
    db.close_trade(row["id"], _now_utc(), exit_value, how)
    pnl = round((exit_value - t.est_cost_per_contract) * 100 * t.qty, 2)
    _alert(f"🏁 CLOSE [{t.strategy}] {t.structure} via {how} "
           f"@ {exit_value:.2f} ({pnl:+.0f}$)")


def manage_positions(now_et: dt.datetime):
    h, m = map(int, settings.time_exit_et.split(":"))
    force_time = now_et.time() >= dt.time(h, m)
    for row in db.open_trades():
        t = _ticket_from_row(row)
        try:
            mark = broker.structure_mark(t)
        except Exception:
            log.exception("structure_mark failed — treating as no mark")
            mark = None

        if row["close_order_id"]:
            # a close is already in flight: poll IT — never submit another
            status, avg, fq = broker.wait_fill(row["close_order_id"], timeout_s=5)
            if status == "filled":
                _journal_close(row, t, avg, row["close_how"] or "time", mark)
                _stuck_close_alerted.discard(row["id"])
            elif status in ("rejected", "canceled", "expired") and fq == 0:
                db.clear_close_order(row["id"])   # definitely dead: retry allowed
                log.info("close order %s dead (%s) — will retry",
                         row["close_order_id"], status)
            elif status != "timeout":             # terminal WITH a partial fill
                if row["id"] not in _stuck_close_alerted:
                    _stuck_close_alerted.add(row["id"])
                    _alert(f"🚨 PARTIAL CLOSE ({status}, {fq} filled) "
                           f"[{t.strategy}] {t.structure} — auto-retry blocked, "
                           f"manual attention (HALT + flatten by hand)")
            continue

        how = None
        if mark is not None:
            _mark_alerted.discard(row["id"])
            pnl_per = (mark - t.est_cost_per_contract) * 100
            if pnl_per >= settings.profit_take_pct * t.max_gain_per_contract:
                how = "target"
            elif pnl_per <= -settings.stop_loss_pct * t.max_risk_per_contract:
                how = "stop"
        if force_time:
            how = how or "time"   # flatten needs no mark: the close is a
                                  # market order; a missing quote must never
                                  # leave a position unmanaged overnight
        elif mark is None:
            if row["id"] not in _mark_alerted:
                _mark_alerted.add(row["id"])
                _alert(f"⚠️ no mark for open {t.structure} — target/stop blind "
                       f"until quotes return; time-exit still armed")
            continue
        if not how:
            continue
        close_id = broker.close(t)
        if not close_id:
            _alert(f"🚨 CLOSE SUBMIT FAILED [{t.strategy}] {t.structure} "
                   f"({how}) — retrying next tick")
            continue
        db.set_close_order(row["id"], close_id, how)
        status, avg, fq = broker.wait_fill(close_id)
        if status == "filled":
            _journal_close(row, t, avg, how, mark)
        elif status in ("rejected", "canceled", "expired") and fq == 0:
            db.clear_close_order(row["id"])
        # anything else (timeout / partial): close_order_id stays set, the
        # next tick polls it — resubmitting could reverse the position


# ---------- restart safety ----------

def reconcile():
    """The journal says what SHOULD be open; the broker says what IS. First
    chase any order whose fate was unknown when we died, then net every
    journal-open leg against live positions. Drift alerts; the one
    provably-safe case (journal open, broker completely flat) self-heals."""
    for row in db.decisions_by_outcome("fill_unknown"):
        if not row["order_id"]:
            db.update_decision_outcome(row["id"], "entry_rejected")
            continue
        t = _ticket_from_decision(row)
        status, avg, fq = broker.wait_fill(row["order_id"], timeout_s=3)
        if status == "timeout":
            _alert(f"⚠️ order {row['order_id']} STILL unknown at boot — "
                   f"leaving parked")
            _pending_entries.append((row["id"], t, row["order_id"]))
            continue
        _finish_entry(row["id"], t, row["order_id"], status, avg, fq)

    live = broker.position_qty()
    problems = []
    rows = db.open_trades()
    expected: dict[str, int] = {}
    for row in rows:
        t = _ticket_from_row(row)
        for l in t.legs:
            signed = l.ratio * t.qty * (1 if l.side == "buy" else -1)
            expected[l.symbol] = expected.get(l.symbol, 0) + signed
    for sym, q in expected.items():
        if live.get(sym, 0) != q:
            problems.append(f"{sym}: journal {q:+d} vs broker {live.get(sym, 0):+d}")
    for sym, q in live.items():
        if sym not in expected:
            problems.append(f"{sym}: {q:+d} at broker, unknown to journal")
    for row in rows:
        t = _ticket_from_row(row)
        if t.legs and all(live.get(l.symbol, 0) == 0 for l in t.legs):
            try:
                mark = broker.structure_mark(t)
            except Exception:
                mark = None
            exit_value = mark if mark is not None else t.est_cost_per_contract
            db.close_trade(row["id"], _now_utc(), exit_value, "reconciled_flat")
            _alert(f"♻️ reconcile: journal trade {row['id']} ({t.structure}) "
                   f"has no legs at the broker — marked closed "
                   f"(exit_how=reconciled_flat, P&L approximate)")
    if problems:
        msg = "🚨 RECONCILE MISMATCH — manual attention:\n" + "\n".join(problems)
        log.error(msg)
        _alert(msg)
    else:
        log.info("reconcile: journal and broker agree (%d open, %d live legs)",
                 len(rows), len(live))


def _restore_claims():
    """A restart must not forget what the day already decided: a filled or
    in-flight ticket keeps its strategy latched (and ORB's claim keeps theta
    out); a vetoed/gate-rejected shot stays spent. Rebuilt from today's
    decision rows — the journal, not memory, is the source of truth."""
    midnight_et = dt.datetime.now(NY).replace(hour=0, minute=0, second=0,
                                              microsecond=0)
    since = midnight_et.astimezone(dt.timezone.utc).isoformat()
    for row in db.decisions_since(since):
        s = _strategy(row["strategy"])
        if s is None:
            continue
        if row["outcome"] in ("filled", "fill_unknown", "ai_veto", "gate_reject"):
            s.fired = True
            if row["outcome"] in ("filled", "fill_unknown") and hasattr(s, "submitted"):
                s.submitted = True
    for row in db.open_trades():
        s = _strategy(row["strategy"])
        if s is not None:
            s.fired = True
            if hasattr(s, "submitted"):
                s.submitted = True


def _reset_day_state():
    """Strategies and the bar watermark reset TOGETHER — resetting one
    without the other is exactly the bug that silently killed the OR."""
    global _seen_until
    for s in strategies:
        s.reset_day()
    _seen_until = None
    _mark_alerted.clear()
    _stuck_close_alerted.clear()


def _feed_bars(bars):
    global _seen_until
    tickets = []
    now_utc = dt.datetime.now(dt.timezone.utc)
    today = dt.datetime.now(NY).date()
    for b in bars:
        if (now_utc - b.timestamp).total_seconds() < settings.bar_finality_s:
            break             # a too-fresh bar may still be revised: leave it
                              # unseen so the next poll gets the final print
        if _seen_until and b.timestamp <= _seen_until:
            continue
        _seen_until = b.timestamp
        ts_et = b.timestamp.astimezone(NY)
        # regular session, current ET day only: yesterday's after-hours prints
        # must never seed (or lock) today's opening range
        if ts_et.date() != today or not (dt.time(9, 30) <= ts_et.time() < dt.time(16, 0)):
            continue
        bar = Bar(settings.underlying, b.timestamp, float(b.open), float(b.high),
                  float(b.low), float(b.close), float(b.volume))
        for s in strategies:
            tk = s.on_bar(bar)
            if tk:
                tickets.append(tk)
    return tickets


def warm_up():
    """Rebuild today's OR state + daily ATR mid-day (start or day rollover).
    Signals from replayed bars are journaled but never traded — a breakout
    that happened before we were watching is not ours to chase. Claims are
    then restored from the journal so the replay cannot resurrect a shot the
    day already spent."""
    theta.daily_atr = broker.daily_atr(settings.underlying)
    start = dt.datetime.now(NY).replace(hour=9, minute=30, second=0, microsecond=0)
    if dt.datetime.now(NY) > start:
        for t in _feed_bars(broker.minute_bars(settings.underlying, start)):
            db.log_decision(t, None, False, "signal during warm-up replay",
                            "warmup_replay")
            _outcome(t, "warmup_replay")
    _restore_claims()
    log.info("warm: ATR=%s OR=%s-%s locked=%s orb(fired=%s submitted=%s) "
             "theta(fired=%s)", theta.daily_atr, orb.or_low, orb.or_high,
             orb.or_locked, orb.fired, orb.submitted, theta.fired)


# ---------- the loop ----------

async def run_forever():
    global _day
    while True:
        now_et = dt.datetime.now(NY)
        try:
            if now_et.date() != _day:
                _reset_day_state()
                warm_up()           # raises -> _day stays unset -> retried;
                _day = now_et.date()    # position management below still runs
        except Exception:
            log.exception("day-rollover warm-up failed — retrying next tick")
        try:
            clock = broker.clock()
            if clock.is_open:
                acct = broker.account()
                day_pnl = acct["equity"] - acct["last_equity"]
                db.snapshot_equity(_now_utc(), acct["equity"], round(day_pnl, 2))
                _resolve_pending_entries()
                if _day == now_et.date():      # only scan with valid day state
                    start = _seen_until or now_et.replace(hour=9, minute=30,
                                                          second=0, microsecond=0)
                    for t in _feed_bars(broker.minute_bars(settings.underlying, start)):
                        handle_ticket(t, day_pnl, acct["equity"])
                manage_positions(now_et)       # even when warm-up is failing
        except Exception:
            log.exception("engine tick failed — continuing")
        await asyncio.sleep(settings.poll_seconds)


def start() -> asyncio.Task:
    """No warm_up here: run_forever's day-rollover branch owns day state
    (a second boot-time warm-up is exactly the double-init that used to wipe
    it). Reconcile is best-effort — a failure alarms, never blocks the loop."""
    global broker
    broker = OptionsBroker()
    log.info("executor=%s data_feed=%s", settings.executor, settings.data_feed)
    try:
        reconcile()
    except Exception:
        log.exception("startup reconcile failed — continuing, journal unverified")
        _alert("⚠️ startup reconcile failed — journal vs broker unverified")
    return asyncio.create_task(run_forever())
