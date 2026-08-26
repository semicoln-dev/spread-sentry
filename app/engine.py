"""The autonomous loop: poll -> scan -> grade -> gate -> execute -> manage.

Pipeline per ticket (each stage can kill it, every death is journaled):
    strategy ticket -> broker.resolve (live chain) -> AI risk officer
    -> hard gates -> submit -> managed exits (profit / stop / time).

Deliberately REST-polling, no websockets: options decisions here are
minute-scale, and polling keeps the agent trivially restartable (the
orb-trader lesson: design for restarts, they will happen).
"""
import asyncio
import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

from app.ai import grader
from app.broker.options import OptionsBroker
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


def handle_ticket(t: TradeTicket, day_pnl: float, n_open: int, equity: float):
    resolved = broker.resolve(t)
    if resolved is None:
        db.log_decision(t, None, False, "no viable contracts/quotes", "unresolvable")
        return
    g = grader.grade(t, {"day_pnl_usd": day_pnl, "open_positions": n_open,
                         "equity": equity})
    if g.verdict != "take":
        db.log_decision(t, g, False, "not gate-checked (AI veto)", "ai_veto")
        _alert(f"⛔ VETO [{t.strategy}] {t.structure}\n{t.thesis}\nAI: {g.reason}")
        return
    t.qty = gates.size_for_cap(t, g.size_frac)
    ok, reason = gates.check(t, n_open, day_pnl)
    if not ok:
        db.log_decision(t, g, False, reason, "gate_reject")
        _alert(f"⛔ GATE [{t.strategy}] {t.structure}\n{t.thesis}\n{reason}")
        return
    order_id = broker.submit(t)
    outcome = "submitted" if order_id else "submit_failed"
    did = db.log_decision(t, g, True, reason, outcome)
    if order_id:
        db.open_trade(did, t, order_id)
        _alert(f"✅ OPEN [{t.strategy}] {t.structure} x{t.qty}\n{t.thesis}\n"
               f"risk ${t.qty * t.max_risk_per_contract:.0f} "
               f"(AI size {g.size_frac:.0%}: {g.reason})")


def _ticket_from_row(row: dict) -> TradeTicket:
    t = TradeTicket(strategy=row["strategy"], underlying=settings.underlying,
                    structure=row["structure"], direction=row["direction"],
                    thesis="", qty=row["qty"])
    t.legs = [Leg(**l) for l in json.loads(row["legs"])]
    t.est_cost_per_contract = row["est_cost"]
    t.max_risk_per_contract = row["max_risk"]
    t.max_gain_per_contract = row["max_gain"]
    return t


def manage_positions(now_et: dt.datetime):
    h, m = map(int, settings.time_exit_et.split(":"))
    force_time = now_et.time() >= dt.time(h, m)
    for row in db.open_trades():
        t = _ticket_from_row(row)
        mark = broker.structure_mark(t)
        if mark is None:
            continue
        pnl_per = (mark - t.est_cost_per_contract) * 100
        how = None
        if pnl_per >= settings.profit_take_pct * t.max_gain_per_contract:
            how = "target"
        elif pnl_per <= -settings.stop_loss_pct * t.max_risk_per_contract:
            how = "stop"
        elif force_time:
            how = "time"
        if how and broker.close(t):
            db.close_trade(row["id"], dt.datetime.now(dt.timezone.utc).isoformat(),
                           mark, how)
            _alert(f"🏁 CLOSE [{t.strategy}] {t.structure} via {how} "
                   f"@ mark {mark:.2f} ({pnl_per * t.qty:+.0f}$)")


def _feed_bars(bars):
    global _seen_until
    tickets = []
    for b in bars:
        if _seen_until and b.timestamp <= _seen_until:
            continue
        bar = Bar(settings.underlying, b.timestamp, float(b.open), float(b.high),
                  float(b.low), float(b.close), float(b.volume))
        for s in strategies:
            tk = s.on_bar(bar)
            if tk:
                tickets.append(tk)
        _seen_until = b.timestamp
    return tickets


def warm_up():
    """Rebuild today's OR state + daily ATR after a (re)start."""
    theta.daily_atr = broker.daily_atr(settings.underlying)
    start = dt.datetime.now(NY).replace(hour=9, minute=30, second=0, microsecond=0)
    if dt.datetime.now(NY) > start:
        for t in _feed_bars(broker.minute_bars(settings.underlying, start)):
            db.log_decision(t, None, False, "signal during warm-up replay", "unresolvable")
    log.info("warm: ATR=%s OR=%s-%s locked=%s", theta.daily_atr,
             orb.or_low, orb.or_high, orb.or_locked)


async def run_forever():
    global _day
    while True:
        try:
            clock = broker.clock()
            now_et = dt.datetime.now(NY)
            if now_et.date() != _day:
                for s in strategies:
                    s.reset_day()
                _day = now_et.date()
                warm_up()
            if clock.is_open:
                acct = broker.account()
                day_pnl = acct["equity"] - acct["last_equity"]
                db.snapshot_equity(dt.datetime.now(dt.timezone.utc).isoformat(),
                                   acct["equity"], round(day_pnl, 2))
                start = _seen_until or now_et.replace(hour=9, minute=30,
                                                      second=0, microsecond=0)
                for t in _feed_bars(broker.minute_bars(settings.underlying, start)):
                    handle_ticket(t, day_pnl, len(db.open_trades()), acct["equity"])
                manage_positions(now_et)
        except Exception:
            log.exception("engine tick failed — continuing")
        await asyncio.sleep(settings.poll_seconds)


def start() -> asyncio.Task:
    global broker
    broker = OptionsBroker()
    warm_up()
    return asyncio.create_task(run_forever())
