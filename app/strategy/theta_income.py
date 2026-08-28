"""Theta income: on range-bound days (narrow opening range vs recent ATR) and
only if the ORB strategy does NOT hold a real claim on the day (a fill or a
ticket in flight — a vetoed ORB releases the day), sell a defined-risk iron
condor — short strangle at ~16-delta with protective wings. The
mean-reversion side of the house, ported from orb-trader's regime thinking:
collect time value when the day has no directional energy.
"""
from datetime import time
from zoneinfo import ZoneInfo

from app.config import settings
from app.strategy.base import Bar, StrategyBase, TradeTicket

NY = ZoneInfo("America/New_York")


class ThetaIncome(StrategyBase):
    name = "theta_income"

    def __init__(self, orb, daily_atr: float | None = None):
        self.orb = orb                     # peek at ORB state: OR width + claim
        self.daily_atr = daily_atr         # 14-day ATR of the underlying, set at warm-up
        h, m = map(int, settings.theta_entry_et.split(":"))
        self.entry_t = time(h, m)
        self.reset_day()

    def reset_day(self):
        self.fired = False
        self.pending = False
        self.rearms = 0

    def on_ticket_outcome(self, outcome: str):
        self.pending = False
        if outcome in ("unresolvable", "submit_failed", "entry_rejected",
                       "ai_error") and self.rearms < 2:
            self.rearms += 1
            self.fired = False             # infrastructure failure: re-arm
                                           # (capped: no per-bar retry loop)

    def on_bar(self, bar: Bar) -> TradeTicket | None:
        t = bar.ts.astimezone(NY).time()
        if self.fired or self.orb.blocks_theta() or t < self.entry_t:
            return None
        orw = self.orb.or_width()
        if orw is None or not self.daily_atr:
            return None
        ratio = orw / self.daily_atr
        if ratio > settings.theta_range_max_ratio:
            return None                    # too much energy: not a range day

        self.fired = True
        self.pending = True
        # never tell the AI "no breakout" on a day that HAD one — a vetoed
        # ORB releases the day to theta, but the risk officer judges the
        # setup and deserves the whole truth
        orb_note = (" (note: an ORB breakout signal fired earlier today but "
                    "was vetoed/not filled)" if self.orb.fired else
                    f", no breakout by {settings.theta_entry_et} ET")
        return TradeTicket(
            strategy=self.name,
            underlying=bar.symbol,
            structure="iron_condor",
            direction="neutral",
            ts=bar.ts,
            thesis=(f"range day: OR width {orw:.2f} = {ratio:.2f}x daily ATR "
                    f"({self.daily_atr:.2f}){orb_note} — sell "
                    f"{settings.theta_short_delta:.2f}-delta condor"),
            params={"short_delta": settings.theta_short_delta,
                    "wing_width": settings.theta_wing_width,
                    "min_dte": settings.theta_min_dte,
                    "max_dte": settings.theta_max_dte,
                    "spot": bar.close},
        )
