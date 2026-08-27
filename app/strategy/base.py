"""Shared strategy interface. Strategies consume bars and emit TradeTickets.

A ticket is a PROPOSAL, not an order: strategies describe the structure they
want (direction, width, target deltas); the broker resolves it against the
live option chain; the risk gates and the AI risk officer decide whether it
trades at all. Strategies never talk to the broker or the AI directly.
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Leg:
    """One resolved option leg."""
    symbol: str          # OCC option symbol, e.g. SPY260904C00645000
    side: str            # "buy" | "sell"
    right: str           # "call" | "put"
    strike: float
    expiry: str          # YYYY-MM-DD
    ratio: int = 1


@dataclass
class TradeTicket:
    strategy: str
    underlying: str
    structure: str                     # call_debit_spread | put_debit_spread | iron_condor
    direction: str                     # long | short | neutral
    thesis: str                        # human-readable, journaled + shown to the AI
    ts: datetime | None = None
    params: dict = field(default_factory=dict)   # width, target deltas, dte...
    # filled in by broker.resolve():
    legs: list[Leg] = field(default_factory=list)
    est_cost_per_contract: float = 0.0   # debit paid (+) or credit received (-)
    max_risk_per_contract: float = 0.0   # $ per 1-lot, defined risk
    max_gain_per_contract: float = 0.0
    # filled in by gates/grader:
    qty: int = 0


class StrategyBase:
    name: str = "base"

    def on_bar(self, bar: Bar) -> TradeTicket | None:
        raise NotImplementedError

    def reset_day(self):
        pass

    def on_ticket_outcome(self, outcome: str):
        """Engine callback after one of this strategy's tickets is decided.
        outcome: filled | entry_rejected | ai_veto | gate_reject |
        unresolvable | submit_failed | skipped_conflict | warmup_replay.
        Lets a strategy re-arm after infrastructure failures without
        re-arming after judgment calls (a veto stays vetoed)."""
        pass
