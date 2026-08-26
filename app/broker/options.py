"""Alpaca options broker: chain resolution + PAPER execution (alpaca-py).

Pure pricing/selection helpers live at module level (unit-testable, no
network). Everything that talks to Alpaca lives in OptionsBroker. PAPER ONLY:
TradingClient(..., paper=True) stays, exactly as in orb-trader.
"""
import logging
from datetime import date, timedelta

from app.config import settings
from app.strategy.base import Leg, TradeTicket

log = logging.getLogger("sentry.broker")


# ---------- pure helpers (no network) ----------

def mid(bid: float | None, ask: float | None) -> float | None:
    if not bid and not ask:
        return None
    if bid and ask:
        return round((bid + ask) / 2, 2)
    return bid or ask


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
                               timeframe=TimeFrame.Minute, start=start, end=end)
        return self.stocks.get_stock_bars(req).data.get(symbol, [])

    def daily_atr(self, symbol: str, days: int = 14) -> float | None:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=[symbol],
                               timeframe=TimeFrame.Day,
                               start=date.today() - timedelta(days=days * 2 + 5))
        bars = self.stocks.get_stock_bars(req).data.get(symbol, [])
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
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            type=ContractType.CALL if right == "call" else ContractType.PUT,
            expiration_date_gte=date.today() + timedelta(days=min_dte),
            expiration_date_lte=date.today() + timedelta(days=max_dte),
            limit=500,
        )
        return list(self.trading.get_option_contracts(req).option_contracts or [])

    def _snapshots(self, symbols: list[str]) -> dict:
        from alpaca.data.requests import OptionSnapshotRequest
        return self.options.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=symbols))

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
        legs, mids_used, credit = [], [], 0.0
        for right in ("put", "call"):
            chain = self._chain(t.underlying, right, t.params["min_dte"], t.params["max_dte"])
            if not chain:
                return None
            expiry = min(c.expiration_date for c in chain)
            cands = {float(c.strike_price): c for c in chain
                     if c.expiration_date == expiry}
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
            for k, side in ((best_k, "sell"), (wing_k, "buy")):
                m = self._mid_of(snaps.get(cands[k].symbol))
                if m is None:
                    return None
                credit += m if side == "sell" else -m
                legs.append(Leg(cands[k].symbol, side, right, k, str(expiry)))
                mids_used.append(m)
        if credit <= 0:
            return None
        t.legs = legs
        t.est_cost_per_contract, t.max_risk_per_contract, t.max_gain_per_contract = \
            condor_econ(round(credit, 2), wing)
        return t if t.max_risk_per_contract > 0 else None

    # -- execution -------------------------------------------------------
    def submit(self, t: TradeTicket) -> str | None:
        """Multi-leg market order (paper). Returns order id or None."""
        if settings.executor == "cli":
            from app.broker.cli import submit_mleg
            return submit_mleg(t)
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, OptionLegRequest
        legs = [OptionLegRequest(
                    symbol=l.symbol, ratio_qty=l.ratio,
                    side=OrderSide.BUY if l.side == "buy" else OrderSide.SELL)
                for l in t.legs]
        try:
            o = self.trading.submit_order(MarketOrderRequest(
                qty=t.qty, order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY, legs=legs))
            return str(o.id)
        except Exception:
            log.exception("submit failed")
            return None

    def close(self, t: TradeTicket) -> str | None:
        """Close = same structure with every side flipped."""
        if settings.executor == "cli":
            from app.broker.cli import close_mleg
            return close_mleg(t)
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, OptionLegRequest
        legs = [OptionLegRequest(
                    symbol=l.symbol, ratio_qty=l.ratio,
                    side=OrderSide.SELL if l.side == "buy" else OrderSide.BUY)
                for l in t.legs]
        try:
            o = self.trading.submit_order(MarketOrderRequest(
                qty=t.qty, order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY, legs=legs))
            return str(o.id)
        except Exception:
            log.exception("close failed")
            return None

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
