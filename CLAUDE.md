# CLAUDE.md — spread-sentry

Hackathon entry: lablab.ai × Alpaca "AI Trading Agents" (Aug 28 – Sep 4,
2026, $6k pool, judged on paper-account P&L + tech + creativity +
presentation). Concept: **an AI risk officer that gates deterministic
options strategies** — the inverse of "let the LLM trade". Sibling of
[orb-trader](../orb-trader) (owner's journaled BTC/stock paper system);
lessons imported, accounts strictly separate. Owner day-trades part-time
(Kuala Lumpur, UTC+8). US options session = 21:30–04:00 MYT.

## Architecture
- `app/strategy/orb_directional.py` — 30-min SPY opening-range breakout → ATM call/put debit spread ($5 wide, 2-7 DTE)
- `app/strategy/theta_income.py` — range day (OR ≤ 0.6× 14d ATR, no ORB by 11:30 ET) → 16-delta iron condor, $5 wings
- `app/strategy/base.py` — Bar/Leg/TradeTicket dataclasses; strategies emit TICKETS (proposals), never orders
- `app/broker/options.py` — chain resolution (delta targeting, snapshot batching ≤100 symbols, strike band around spot), mleg orders, marks; pure econ helpers at module top (unit-tested)
- `app/broker/cli.py` — Alpaca CLI executor seam (`SENTRY_EXECUTOR=cli`) — hackathon requirement #2, command templates NOT yet verified
- `app/ai/grader.py` — Claude risk officer (claude-sonnet-5), strict JSON verdict; tickets reach it UNSIZED by design
- `app/risk/gates.py` — hard gates the AI cannot touch (see rules)
- `app/engine.py` — poll loop (REST only, no websockets — restart-proof): scan → resolve → grade → gate → submit → manage exits; open positions re-adopted from journal on restart
- `app/journal/db.py` — SQLite: decisions (AI reasoning verbatim), trades with exact R-multiples, equity snapshots; rows never deleted
- `app/main.py` + `app/static/dashboard.html` — FastAPI, dashboard at `/`
- `test_sentry.py` — offline synthetic tests; run after ANY change

## Non-negotiable rules
1. PAPER ONLY. `TradingClient(..., paper=True)` stays.
2. Defined-risk structures ONLY — the gate validator rejects any short leg
   without a same-right/same-expiry long. Never weaken it.
3. The AI is VETO-ONLY: it may reject or shrink, never create trades,
   raise size, or bypass gates. Fail-closed: any AI error → no trade.
   Keep this asymmetry — it is the project's core thesis and pitch.
4. Hard gates (per-trade $2k, 3 positions, $3k daily halt, 15:30 ET entry
   cutoff, HALT kill-file) are code-enforced; nothing routes around them.
5. NEVER stop the app with an open position: exits are software-managed,
   there are NO server-side stops at Alpaca (unlike orb-trader). Worst
   case is capped (defined risk) but unmanaged. Flatten first (time-exit
   runs 15:45 ET / 03:45 MYT) or accept the capped ride.
6. Account discipline: DEV account for all testing. The COMPETITION
   account (create fresh at kickoff, set to $100,000) trades ONLY during
   the judged week — one stray dev trade on it = disqualified. Never use
   the orb-trader Alpaca accounts here.
7. Never commit `.env` (Alpaca + Anthropic + Telegram keys). Gitignored.
8. Run `venv\Scripts\python test_sentry.py` after any change.

## Hackathon requirements (submission = Sep 4, 23:00 MYT)
- Fresh paper account, $100k, account ID in submission — P&L is judged
- MUST use Alpaca MCP server or CLI (req #2) — **NOT YET SATISFIED**:
  cli.py templates unverified; verify or wire MCP before submitting
- Public GitHub repo (MIT — LICENSE done), one-page write-up (skeleton in
  README: AI logic / risk gates / infra), video + slides + cover image
- Optional: ≤5 build-in-public posts on X/LinkedIn tagging @lablabai + @AlpacaHQ
- Judged week ≈ 5 sessions (Fri 28 partial from 23:00 MYT kickoff, Mon–Thu
  full, Fri 4 until 23:00 MYT). Agent must run 21:15–04:00 MYT nightly —
  overnight PC or small VM: still undecided.

## Status (update this as things change)
- Verified against live paper API (Aug 26): account/clock/bars/ATR, BOTH
  structures resolve with sane economics, real Claude grades (vetoed a
  thesis-less ticket, took a real condor at 0.6 size), gate sizing, app
  boot + dashboard, Telegram alerts. Offline suite: all pass.
- NOT yet verified: live multi-leg FILLS (first supervised run planned
  Wed Aug 26, 21:15 MYT, dev account) and the CLI executor (req #2).
- Repo is LOCAL ONLY (2 commits) — owner publishes to GitHub on
  submission day, not before.
- Known quirks: options snapshot endpoint caps at 100 symbols (batched);
  grader must not see qty (sized after verdict); equity in /health is
  null until the first market-hours snapshot.

## Conventions
- Run: `venv\Scripts\Activate.ps1; uvicorn app.main:app` (dashboard :8000)
- Windows/PowerShell: no `&&` chaining; use `;`
- ALL tunables in `app/config.py`; never hardcode in logic files
- Owner prefers concise plain-language explanations, one change at a
  time, validated (here: by tests + the decisions journal) before the next
