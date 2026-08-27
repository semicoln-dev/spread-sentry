"""Alpaca options broker: chain resolution + PAPER execution (alpaca-py).

Pure pricing/selection helpers live at module level (unit-testable, no
network). Everything that talks to Alpaca lives in OptionsBroker. PAPER ONLY:
TradingClient(..., paper=True) stays, exactly as in orb-trader.
"""
import datetime as dt
import logging
import time as _time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.strategy.base import Leg, TradeTicket

log = logging.getLogger("sentry.broker")
NY = ZoneInfo("America/New_York")


# ---------- pure helpers (no network) ----------

def mid(bid: float | None, ask: float | None) -> float | None:
    """Quote mid. A numeric 0.0 bid is a real level (deep-OTM wings quote
    0.00 x 0.05 all day), not a missing quote — averaging it in keeps condor
    marks honest instead of marking a worthless wing at the full ask."""
    if bid is None and ask is None:
        return None
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 2)
    return bid if bid is not None else ask


def nearest(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda s: abs(s - target))


def debit_spread_econ(long_mid: float, short_mid: float, width: float) -> tuple[float, float, float]:
    """-> (est_cost, max_risk_per_contract, max_gain_per_contract) in $."""
    cost = round(long_mid - short_mid, 2)
    return cost, round(cost * 100, 2), round((width - cost) * 100, 2)


def condor_econ(credit: float, wing_width: float) -> tuple[float, float, float]:
    """-> (est_cost, max_risk, max_gain); est_cost is NEGATIVE (credit in)."""
    return round(-credit, 2), round((wing_width - credit) * 100, 2), round(credit * 100, 2)


def structure_value(leg_mids: list[tuple[str, float]]) -> float:
    """Mark of the whole structure per 1-lot: buys add, sells subtract.
    Comparable to TradeTicket.est_cost_per_contract for both debit (+) and
    credit (-) structures, so pnl = (value_now - est_cost) * 100 * qty."""
    return round(sum(m if side == "buy" else -m for side, m in leg_mids), 2)


def econ_from_fill(t: TradeTicket, fill_cost: float) -> None:
    """Re-anchor the ticket's economics to the ACTUAL entry fill (Alpaca mleg
    filled_avg_price: debit positive, credit negative — same sign convention
    as est_cost_per_contract). Widths come from the resolved legs, so this is
    exact, not an estimate. Journaled risk/R-multiples are then fill-true."""
    fill_cost = round(fill_cost, 2)
    t.est_cost_per_contract = fill_cost
    if t.structure in ("call_debit_spread", "put_debit_spread"):
        width = abs(t.legs[0].strike - t.legs[1].strike)
        t.max_risk_per_contract = round(fill_cost * 100, 2)
        t.max_gain_per_contract = round((width - fill_cost) * 100, 2)
    elif t.structure == "iron_condor":
        credit = -fill_cost
        widths = []
        for right in ("put", "call"):
            ks = [l.strike for l in t.legs if l.right == right]
            if len(ks) == 2:
                widths.append(abs(ks[0] - ks[1]))
        wing = max(widths) if widths else 0.0
        t.max_risk_per_contract = round((wing - credit) * 100, 2)
        t.max_gain_per_contract = round(credit * 100, 2)


def today_et() -> date:
    """The US trading calendar date. Never use date.today() for market math:
    this box runs on UTC+8, where 'today' flips a US day early at 12:00 ET."""
    return dt.datetime.now(NY).date()


# ---------- network ----------

class OptionsBroker:
    def __init__(self):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.trading.client import TradingClient
        self.trading = TradingClient(settings.alpaca_api_key,
                                     settings.alpaca_secret_key, paper=True)
        self.stocks = StockHistoricalDataClient(settings.alpaca_api_key,
                                                settings.alpaca_secret_key)
        self.options = OptionHistoricalDataClient(settings.alpaca_api_key,
                                                  settings.alpaca_secret_key)

    def _feed(self):
        from alpaca.data.enums import DataFeed
        f = settings.data_feed.lower()
        if f == "iex":
            return DataFeed.IEX
        if f == "sip":
            return DataFeed.SIP
        # a typo must not silently fall back to the 15-min-delayed feed that
        # caused the Aug-26 late-entry incident
        raise ValueError(f"SENTRY_DATA_FEED={settings.data_feed!r}: use iex or sip")

    # -- market context --------------------------------------------------
    def clock(self):
        return self.trading.get_clock()

    def account(self) -> dict:
        a = self.trading.get_account()
        return {"equity": float(a.equity), "cash": float(a.cash),
                "last_equity": float(a.last_equity)}

    def minute_bars(self, symbol: str, start, end=None):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=[symbol],
                               timeframe=TimeFrame.Minute, start=start, end=end,
                               feed=self._feed())
        return self.stocks.get_stock_bars(req).data.get(symbol, [])

    def latest_spot(self, symbol: str) -> float | None:
        """Real-time last trade (works on the free plan via IEX). Strike
        selection must anchor to THIS, not to a possibly-stale signal bar."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol,
                                          feed=DataFeed.IEX)
            trade = self.stocks.get_stock_latest_trade(req).get(symbol)
            return float(trade.price) if trade else None
        except Exception:
            log.exception("latest_spot failed — falling back to bar close")
            return None

    def daily_atr(self, symbol: str, days: int = 14) -> float | None:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=[symbol],
                               timeframe=TimeFrame.Day,
                               start=today_et() - timedelta(days=days * 2 + 5),
                               feed=self._feed())
        bars = self.stocks.get_stock_bars(req).data.get(symbol, [])
        # a mid-session call must not fold today's PARTIAL daily bar into ATR
        bars = [b for b in bars if b.timestamp.astimezone(NY).date() < today_et()]
        if len(bars) < 2:
            return None
        trs = []
        for prev, cur in zip(bars[:-1], bars[1:]):
            trs.append(max(cur.high - cur.low, abs(cur.high - prev.close),
                           abs(cur.low - prev.close)))
        return round(sum(trs[-days:]) / min(days, len(trs)), 2)

    # -- chain resolution ------------------------------------------------
    def _chain(self, underlying: str, right: str, min_dte: int, max_dte: int) -> list:
        from alpaca.trading.enums import AssetStatus, ContractType
        from alpaca.trading.requests import GetOptionContractsRequest
        out, token = [], None
        while True:      # SPY 2-7 DTE per right exceeds one 500-row page:
            req = GetOptionContractsRequest(   # silent truncation would quietly
                underlying_symbols=[underlying],    # shift expiry/strike choice
                status=AssetStatus.ACTIVE,
                type=ContractType.CALL if right == "call" else ContractType.PUT,
                expiration_date_gte=today_et() + timedelta(days=min_dte),
                expiration_date_lte=today_et() + timedelta(days=max_dte),
                limit=500,
                page_token=token,
            )
            resp = self.trading.get_option_contracts(req)
            out.extend(resp.option_contracts or [])
            token = getattr(resp, "next_page_token", None)
            if not token or len(out) >= 5000:
                break
        return out

    def _snapshots(self, symbols: list[str]) -> dict:
        from alpaca.data.requests import OptionSnapshotRequest
        out = {}
        for i in range(0, len(symbols), 100):   # endpoint caps at 100 symbols
            out.update(self.options.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=symbols[i:i + 100])))
        return out

    def _mid_of(self, snap) -> float | None:
        q = getattr(snap, "latest_quote", None)
        return mid(getattr(q, "bid_price", None), getattr(q, "ask_price", None)) if q else None

    def _delta_of(self, snap) -> float | None:
        g = getattr(snap, "greeks", None)
        return getattr(g, "delta", None) if g else None

    def resolve(self, t: TradeTicket) -> TradeTicket | None:
        """Fill ticket.legs + economics from the live chain. None = no viable
        contracts (ticket dies quietly, journaled as unresolvable)."""
        try:
            # strike targeting anchors to the live tape; the signal-bar close
            # (t.params["spot"]) stays in the thesis and is the fallback only
            live = self.latest_spot(t.underlying)
            if live is not None:
                t.params["spot"] = live
            if t.structure in ("call_debit_spread", "put_debit_spread"):
                return self._resolve_debit(t)
            if t.structure == "iron_condor":
                return self._resolve_condor(t)
            log.error("unknown structure %s", t.structure)
        except Exception:
            log.exception("resolve failed for %s", t.structure)
        return None

    def _resolve_debit(self, t: TradeTicket) -> TradeTicket | None:
        right = "call" if t.structure == "call_debit_spread" else "put"
        spot, width = t.params["spot"], t.params["width"]
        chain = self._chain(t.underlying, right, t.params["min_dte"], t.params["max_dte"])
        if not chain:
            return None
        expiry = min(c.expiration_date for c in chain)          # nearest allowed
        by_strike = {float(c.strike_price): c for c in chain
                     if c.expiration_date == expiry}
        long_k = nearest(list(by_strike), spot)                  # ~ATM long
        short_k = nearest(list(by_strike),
                          long_k + width if right == "call" else long_k - width)
        if short_k == long_k:
            return None
        legs_c = [(by_strike[long_k], "buy", long_k), (by_strike[short_k], "sell", short_k)]
        snaps = self._snapshots([c.symbol for c, _, _ in legs_c])
        mids = [self._mid_of(snaps.get(c.symbol)) for c, _, _ in legs_c]
        if any(m is None for m in mids):
            return None
        cost, risk, gain = debit_spread_econ(mids[0], mids[1], abs(short_k - long_k))
        if cost <= 0 or gain <= 0:
            return None                                          # broken quote
        t.legs = [Leg(c.symbol, side, right, k, str(expiry))
                  for (c, side, k) in legs_c]
        t.est_cost_per_contract, t.max_risk_per_contract, t.max_gain_per_contract = cost, risk, gain
        return t

    def _resolve_condor(self, t: TradeTicket) -> TradeTicket | None:
        spot = t.params["spot"]
        wing, want_d = t.params["wing_width"], t.params["short_delta"]
        chains = {r: self._chain(t.underlying, r, t.params["min_dte"], t.params["max_dte"])
                  for r in ("put", "call")}
        if not chains["put"] or not chains["call"]:
            return None
        # both rights MUST share one expiry — a put side expiring Thursday
        # under a call side expiring Friday is a different (undefined) trade
        common = ({c.expiration_date for c in chains["put"]}
                  & {c.expiration_date for c in chains["call"]})
        if not common:
            return None
        expiry = min(common)
        legs, credit, widths = [], 0.0, []
        for right in ("put", "call"):
            # shorts land within a few % of spot; keep a band wide enough for
            # them plus their wings, instead of snapshotting the whole chain
            band = spot * 0.05 + wing
            cands = {float(c.strike_price): c for c in chains[right]
                     if c.expiration_date == expiry
                     and abs(float(c.strike_price) - spot) <= band}
            if not cands:
                return None
            snaps = self._snapshots([c.symbol for c in cands.values()])
            # short strike: closest to target |delta|; fallback: ~2% OTM
            best_k, best_err = None, 9e9
            for k, c in cands.items():
                d = self._delta_of(snaps.get(c.symbol))
                if d is None:
                    continue
                err = abs(abs(d) - want_d)
                if err < best_err:
                    best_k, best_err = k, err
            if best_k is None:
                best_k = nearest(list(cands),
                                 spot * (0.98 if right == "put" else 1.02))
            wing_k = nearest(list(cands),
                             best_k - wing if right == "put" else best_k + wing)
            if wing_k == best_k:
                return None
            widths.append(abs(wing_k - best_k))
            for k, side in ((best_k, "sell"), (wing_k, "buy")):
                m = self._mid_of(snaps.get(cands[k].symbol))
                if m is None:
                    return None
                credit += m if side == "sell" else -m
                legs.append(Leg(cands[k].symbol, side, right, k, str(expiry)))
        if credit <= 0:
            return None
        t.legs = legs
        # risk from the strikes we actually chose, not the configured wish:
        # nearest() can land a wider wing, and sizing against the smaller
        # configured width would breach the hard per-trade cap
        t.est_cost_per_contract, t.max_risk_per_contract, t.max_gain_per_contract = \
            condor_econ(round(credit, 2), max(widths))
        return t if t.max_risk_per_contract > 0 else None

    # -- execution -------------------------------------------------------
    def submit(self, t: TradeTicket) -> str | None:
        """Multi-leg market order (paper). Returns order id or None."""
        if settings.executor == "cli":
            from app.broker.cli import submit_mleg
            return submit_mleg(t)
        return self._submit_mleg(t, flip=False)

    def close(self, t: TradeTicket) -> str | None:
        """Close = same structure with every side flipped."""
        if settings.executor == "cli":
            from app.broker.cli import close_mleg
            return close_mleg(t)
        return self._submit_mleg(t, flip=True)

    def _submit_mleg(self, t: TradeTicket, flip: bool) -> str | None:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, OptionLegRequest
        def side(l):
            buy = (l.side == "buy") != flip
            return OrderSide.BUY if buy else OrderSide.SELL
        legs = [OptionLegRequest(symbol=l.symbol, ratio_qty=l.ratio, side=side(l))
                for l in t.legs]
        try:
            o = self.trading.submit_order(MarketOrderRequest(
                qty=t.qty, order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY, legs=legs))
            return str(o.id)
        except Exception:
            log.exception("mleg submit failed (flip=%s)", flip)
            return None

    def wait_fill(self, order_id: str, timeout_s: int | None = None) -> tuple[str, float | None, int]:
        """Poll an order until it leaves the pending states.
        -> (status, filled_avg_price, filled_qty). status: 'filled', a
        definite dead state ('rejected'/'canceled'/'expired'), or 'timeout'
        meaning STILL UNKNOWN — the caller must treat unknown as unknown,
        never as 'nothing happened'. A canceled order can still carry a
        partial fill (filled_qty > 0): that partial is a real position.
        A last-seen 'filled' with no usable avg price still returns 'filled'
        (avg None) — the fill is a fact even when the price echo lags.
        Paper market mlegs fill in milliseconds; slower is a problem."""
        timeout_s = timeout_s or settings.fill_timeout_s
        deadline = _time.monotonic() + timeout_s
        last_status, last_avg, last_qty = "timeout", None, 0
        while True:
            try:
                if settings.executor == "cli":
                    from app.broker.cli import order_get
                    d = order_get(order_id) or {}
                    status = str(d.get("status", "")).lower()
                    avg, fq = d.get("filled_avg_price"), d.get("filled_qty")
                else:
                    o = self.trading.get_order_by_id(order_id)
                    status = str(getattr(o.status, "value", o.status)).lower()
                    avg, fq = o.filled_avg_price, o.filled_qty
                if status:
                    last_status = status
                    last_avg = float(avg) if avg not in (None, "", 0, "0") else None
                    last_qty = int(float(fq)) if fq else 0
                if status == "filled" and last_avg is not None:
                    return "filled", last_avg, last_qty
                if status in ("rejected", "canceled", "expired"):
                    return status, last_avg, last_qty
            except Exception:
                log.exception("wait_fill poll failed")
            if _time.monotonic() >= deadline:
                break
            _time.sleep(1)
        if last_status == "filled":
            return "filled", last_avg, last_qty     # fill certain, price missing
        return "timeout", None, last_qty

    def cancel(self, order_id: str) -> bool:
        try:
            self.trading.cancel_order_by_id(order_id)
            return True
        except Exception:
            log.exception("cancel failed for %s", order_id)
            return False

    def position_qty(self) -> dict[str, int]:
        """Signed live position quantities by OCC symbol (short = negative).
        The journal is the plan; THIS is the truth it reconciles against."""
        out = {}
        for p in self.trading.get_all_positions():
            out[p.symbol] = int(float(p.qty))
        return out

    def structure_mark(self, t: TradeTicket) -> float | None:
        """Current per-1-lot mark of an open structure (see structure_value)."""
        snaps = self._snapshots([l.symbol for l in t.legs])
        pairs = []
        for l in t.legs:
            m = self._mid_of(snaps.get(l.symbol))
            if m is None:
                return None
            pairs.append((l.side, m))
        return structure_value(pairs)
