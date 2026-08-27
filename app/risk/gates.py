"""HARD risk gates. Code-enforced, checked AFTER the AI grades a ticket —
the AI can veto or shrink a trade, it can never bypass these. Every rejection
returns the reason string that gets journaled.
"""
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.config import settings
from app.strategy.base import TradeTicket

NY = ZoneInfo("America/New_York")


def _structure_is_defined_risk(t: TradeTicket) -> bool:
    """No naked short exposure, counted properly. Within each (right, expiry)
    group: total BUY ratio must cover total SELL ratio (two shorts hiding
    behind one long, or a ratio-2 short behind a ratio-1 long, are net naked
    even though every short 'has' a long), and every SELL leg must have a
    BUY leg at a different strike (same-strike pairs cancel, they don't cap
    a payoff). Debit spreads and condor wings pass; anything net short in
    any group is rejected. No naked shorts, ever."""
    groups: dict[tuple, dict] = {}
    for l in t.legs:
        g = groups.setdefault((l.right, l.expiry),
                              {"buy": 0, "sell": 0, "buys": [], "sells": []})
        g[l.side] += l.ratio
        g[l.side + "s"].append(l)
    for g in groups.values():
        if g["sell"] > g["buy"]:
            return False
        for s in g["sells"]:
            if not any(b.strike != s.strike for b in g["buys"]):
                return False
    return True


def check(t: TradeTicket, open_positions: int, day_pnl_usd: float,
          now: datetime | None = None) -> tuple[bool, str]:
    """Returns (allowed, reason). Sizing must already be set (t.qty)."""
    if os.path.exists(settings.kill_file):
        return False, f"kill switch: {settings.kill_file} file present"

    if day_pnl_usd <= -settings.daily_loss_halt_usd:
        return False, (f"daily halt: day P&L {day_pnl_usd:+.0f} <= "
                       f"-{settings.daily_loss_halt_usd:.0f}")

    if open_positions >= settings.max_open_positions:
        return False, f"max open positions ({settings.max_open_positions}) reached"

    now = now or datetime.now(NY)
    h, m = map(int, settings.no_entries_after_et.split(":"))
    if now.astimezone(NY).time() > time(h, m):
        return False, f"no entries after {settings.no_entries_after_et} ET"

    if not t.legs:
        return False, "ticket has no resolved legs"

    if settings.defined_risk_only and not _structure_is_defined_risk(t):
        return False, "REJECTED: structure is not defined-risk (naked short leg)"

    if t.max_risk_per_contract <= 0:
        return False, "max risk per contract is not positive — refuse to size"

    if t.qty < 1:
        return False, "size rounds to zero contracts under the risk cap"

    total_risk = t.qty * t.max_risk_per_contract
    if total_risk > settings.max_risk_per_trade_usd:
        return False, (f"risk {total_risk:.0f} exceeds per-trade cap "
                       f"{settings.max_risk_per_trade_usd:.0f}")

    return True, (f"gates ok: qty {t.qty}, risk "
                  f"{total_risk:.0f}/{settings.max_risk_per_trade_usd:.0f}, "
                  f"positions {open_positions}/{settings.max_open_positions}")


def size_for_cap(t: TradeTicket, size_frac: float = 1.0) -> int:
    """Contracts that fit the per-trade cap, scaled by the AI's fraction
    (AI can only shrink: frac is clamped to [0, 1])."""
    frac = max(0.0, min(size_frac, 1.0))
    if t.max_risk_per_contract <= 0:
        return 0
    return int((settings.max_risk_per_trade_usd * frac) // t.max_risk_per_contract)
