# Spread Sentry

**An AI risk officer that gates deterministic options strategies.**
Built for the lablab.ai × Alpaca "AI Trading Agents" hackathon (Aug 28 –
Sep 4, 2026) — and grown out of a month-long, fully journaled paper-trading
experiment ([orb-trader](https://github.com/semicoln-dev/orb-trader)).

## The idea

Most AI trading agents let the model decide *what* to trade. Ours does the
opposite. Deterministic strategies — ported from live playbooks with a
month of R-multiple journal history behind them — propose trades as
structured tickets. Claude sits above them as a **risk officer with
asymmetric powers**:

- it can **veto** a proposed trade,
- it can **shrink** the position below the risk cap,
- it can **never** invent a trade, raise size, or override the hard gates.

Below both sits a layer of **code-enforced risk gates** the AI cannot
touch: defined-risk structures only (every short leg must have a wing),
2% max risk per trade, 3 concurrent positions, a 3% daily loss halt, a
no-entries window before the close, and a kill-switch file. Fail-closed
everywhere: if the AI layer errors, the answer is *no trade* — an outage
can only ever make the system more conservative.

## Strategies (both 100% options, defined-risk)

| | Setup | Structure |
|---|---|---|
| **ORB directional** | 30-min SPY opening-range breakout (ported from the live equity playbook) | ATM call/put **debit spread**, $5 wide, 2-7 DTE |
| **Theta income** | Range day (OR width ≤ 0.6× 14-day ATR, no breakout by 11:30 ET) | 16-delta **iron condor**, $5 wings, 2-4 DTE |

Exits are managed continuously: 50% of max gain, 50% of max risk, or the
15:45 ET time stop — whichever comes first. Every ticket, verdict, gate
reason, and fill lands in a SQLite journal with exact R-multiples
(defined risk makes R exact), served on a live dashboard at `/`.

## Pipeline

```
minute bars (REST poll, no websockets — restart-proof by design)
   └─> strategies emit TradeTicket (structure + thesis, no order yet)
        └─> broker resolves legs & economics from the live option chain
             └─> Claude risk officer: take / veto / shrink   [fail-closed]
                  └─> HARD GATES: defined-risk, caps, halt   [not AI-overridable]
                       └─> multi-leg paper order via Alpaca
                            └─> managed exits + journal + Telegram alerts
```

## Alpaca stack (hackathon requirement #2)

- **Trading API** — option chains, multi-leg (`mleg`) orders, account,
  clock; everything runs against the **paper** environment
  (`TradingClient(..., paper=True)` is hard-coded).
- **CLI** — the whole order lifecycle (submit, fill-poll, close) routes
  through the **official Alpaca CLI** ([github.com/alpacahq/cli](https://github.com/alpacahq/cli))
  with `SENTRY_EXECUTOR=cli`; see `app/broker/cli.py`. Command shapes are
  verified against the CLI source (v0.0.13): `alpaca order submit
  --order-class mleg --legs '<json>'` / `alpaca order get`. The CLI reads
  the same `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` env vars as the app and
  defaults to paper. In cli mode the app **refuses to boot** if the binary
  is missing — no silent submit-failure nights. Install from the official
  release binaries; note the PyPI package named `alpaca-cli` is an
  unrelated third-party project.
- **MCP server** *(optional upgrade)* — point the risk officer at
  `alpaca-mcp-server` so Claude pulls its own account/market context via
  tools during grading. Seam: `app/ai/grader.py` `context` dict.

## Run it

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt   # (Windows; use bin/ on unix)
copy .env.example .env                          # then fill in the keys
venv\Scripts\uvicorn app.main:app
```

Dashboard at http://localhost:8000. Tests (offline, synthetic):
`venv\Scripts\python test_sentry.py`. Kill switch: create a file named
`HALT` next to the app — new entries stop instantly, exits keep managing.

## Hackathon submission checklist

- [ ] **Fresh Alpaca paper account** created for the hackathon only,
      balance set to **$100,000** (reused accounts are disqualified);
      account ID goes in the submission form
- [x] `SENTRY_EXECUTOR=cli` verified working — requirement #2 (live
      supervised round-trip through the official CLI, Aug 28: submit,
      fill-poll, close, flat)
- [ ] Repo public on GitHub, MIT license (done: `LICENSE`)
- [ ] One-page write-up (skeleton below)
- [ ] Video + slide presentation, cover image
- [ ] Up to 5 build-in-public posts on X/LinkedIn tagging @lablabai + @AlpacaHQ
- [ ] Agent hosted/running through US market hours during judging week
      (21:30–04:00 MYT — the PC-off-at-midnight problem: run overnight or
      use a small VM for the week)

## Write-up skeleton (the required one-pager)

1. **AI logic** — Claude as veto-only risk officer; strict JSON contract;
   asymmetric powers; fail-closed on any AI error (quote the grader test).
2. **Risk gates** — defined-risk validator, per-trade/day caps, halt file;
   lessons imported from a month of journaled paper trading (limit-entry
   slippage fix, -3R daily halt, tamper-proof journal).
3. **Alpaca infrastructure** — Trading API for chains + mleg orders, CLI
   executor, paper-only; REST polling for restart resilience.
