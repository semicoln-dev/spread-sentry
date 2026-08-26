"""Opening Range Breakout, ported from orb-trader — expressed as defined-risk
debit spreads instead of stock. Break above the 30-min opening range -> call
debit spread; break below -> put debit spread. One shot per day, NY-anchored.
"""
from datetime import time
from zoneinfo import ZoneInfo

from app.config import settings
from app.strategy.base import Bar, StrategyBase, TradeTicket

NY = ZoneInfo("America/New_York")


class OrbDirectional(StrategyBase):
    name = "orb_directional"

    def __init__(self):
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.or_locked = False
        self.fired = False
        h, m = map(int, settings.orb_last_entry_et.split(":"))
        self.last_entry_t = time(h, m)
        self.or_end_t = time(9 + (30 + settings.or_minutes) // 60,
                             (30 + settings.or_minutes) % 60)

    def reset_day(self):
        self.or_high = self.or_low = None
        self.or_locked = self.fired = False

    def or_width(self) -> float | None:
        if self.or_high is None or self.or_low is None:
            return None
        return self.or_high - self.or_low

    def on_bar(self, bar: Bar) -> TradeTicket | None:
        t = bar.ts.astimezone(NY).time()
        if t < time(9, 30):
            return None
        if not self.or_locked:
            if t < self.or_end_t:
                self.or_high = max(self.or_high or bar.high, bar.high)
                self.or_low = min(self.or_low or bar.low, bar.low)
                return None
            self.or_locked = True

        if self.fired or self.or_high is None or t > self.last_entry_t:
            return None

        direction = structure = None
        if bar.close > self.or_high:
            direction, structure = "long", "call_debit_spread"
            edge = self.or_high
        elif bar.close < self.or_low:
            direction, structure = "short", "put_debit_spread"
            edge = self.or_low
        if direction is None:
            return None

        self.fired = True
        return TradeTicket(
            strategy=self.name,
            underlying=bar.symbol,
            structure=structure,
            direction=direction,
            ts=bar.ts,
            thesis=(f"{settings.or_minutes}-min opening range "
                    f"{self.or_low:.2f}-{self.or_high:.2f} broke "
                    f"{'up' if direction == 'long' else 'down'} at {bar.close:.2f} "
                    f"(edge {edge:.2f})"),
            params={"width": settings.orb_spread_width,
                    "min_dte": settings.orb_min_dte,
                    "max_dte": settings.orb_max_dte,
                    "spot": bar.close},
        )
