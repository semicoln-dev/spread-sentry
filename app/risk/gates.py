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
    """Every SELL leg must be paired with a BUY leg of the same right and
    expiry at a different strike — a long of the same right caps the payoff
    beyond the strikes on either side (debit spread: long below the short;
    credit spread/condor wing: long beyond it). Assumes equal ratios, which
    holds for every structure this agent trades. No naked shorts, ever."""
    sells = [l for l in t.legs if l.side == "sell"]
    buys = [l for l in t.legs if l.side == "buy"]
    return all(
        any(b.right == s.right and b.expiry == s.expiry and b.strike != s.strike
            for b in buys)
        for s in sells)


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
