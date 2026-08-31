# Spread Sentry — an AI risk officer that gates deterministic options strategies

**Alpaca paper account (judged): `PA3J9Y7UA5C8`** · Repo: https://github.com/semicoln-dev/spread-sentry · MIT

## The inversion

Most AI trading agents ask a language model *what to trade*. That puts the least
predictable component in charge of entry, size, and risk — and it is why most of
them cannot be audited after the fact.

Spread Sentry inverts the hierarchy. **Deterministic strategies propose; Claude
may only refuse.** Two mechanical playbooks emit *tickets* (a structure and a
thesis, never an order). Claude sits above them as a **risk officer with
deliberately asymmetric powers**:

- it can **veto** a proposed trade,
- it can **shrink** it below the risk cap,
- it can **never** invent a trade, raise size, or touch a gate.

Below both sits a layer of **code-enforced gates the AI cannot reach**:
defined-risk structures only (a counting validator rejects any group whose short
ratios exceed its long ratios), $2,000 max risk per trade, 3 concurrent
positions, a $3,000 daily-loss halt, a 15:30 ET entry cutoff, and a `HALT`
kill-file. Size is computed *after* the verdict — the model never sees a
quantity it could inflate, and the fraction it returns is clamped twice
independently.

## Fail-closed, demonstrated in production

The design rule is that **every failure makes the system more conservative**. A
missing API key, a network error, a malformed reply, an unparseable number: all
resolve to *no trade*.

This is not a claim; it happened. On Aug 28 the model's JSON verdict was
truncated mid-reason by a token limit. The agent refused to act on a partial
verdict and vetoed — a real breakout signal passed by, no order was sent, and
the reason is in the journal verbatim. The fix (larger budget, an explicit
truncation guard) also drew a distinction the first version lacked: **our
outage is not the risk officer saying no**, so grader-layer failures now re-arm
the strategy, while a genuine veto still consumes the day's shot.

Every approval so far has been a *reduction*: 0.6×, 0.7×, 0.6× of the gate cap.
The risk officer has never once approved at full size.

## What the agent trades

| Strategy | Setup | Structure |
|---|---|---|
| **ORB directional** | 30-min SPY opening-range breakout, $0.10 buffer | ATM call/put **debit spread**, $5 wide, 2–7 DTE |
| **Theta income** | Range day (OR ≤ 0.6× 14-day ATR), no breakout | 16-delta **iron condor**, $5 wings, 2–4 DTE |

Both are defined-risk by construction; the two exclude each other so the agent
never holds contradictory exposure. Exits are software-managed every 60 seconds:
50% of max gain, 50% of max risk, or a 15:45 ET flatten — and the flatten fires
*even when quotes are missing*, because leaving a position unmanaged overnight is
the one outcome the design refuses.

## Alpaca infrastructure

- **Trading API** — option-chain resolution (delta targeting, paginated fetch of
  ~786 contracts per right), multi-leg `mleg` orders, positions, clock. Paper
  only, hard-coded.
- **Official Alpaca CLI** (requirement #2) — the entire order lifecycle runs
  through `alpaca order submit --order-class mleg --legs …` and `alpaca order
  get`, verified by a supervised live round-trip. The app refuses to boot in CLI
  mode if the binary is absent, because a silent no-trade night is worse than no
  boot.
- **Restart-proof by design** — REST polling, no websockets. Day state rebuilds
  once per session, open positions re-adopt from the journal, and startup
  reconciles the journal against live broker positions. This was proven twice in
  anger: an accidental mid-week reboot, and a live migration from a laptop to a
  cloud VM *during* the judged week.

## The journal is the evidence

Every ticket, verdict (reasoning verbatim), gate decision, and fill lands in
SQLite. P&L is booked from **actual fills**, never quote-time marks — orders are
polled to confirmation, and an order whose fate is unknown is parked and
re-polled rather than assumed. Defined risk makes R-multiples exact.

**Judged results (Aug 28 – Sep 4): _[final P&L, trade count, and R-multiples
from the journal — filled in at submission]_.**

The honest summary of this project is not a P&L number. It is that the trades
which happened are explainable line by line, the trades which did not happen have
recorded reasons, and every loss was capped before it was taken.
