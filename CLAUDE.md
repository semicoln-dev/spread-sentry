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
- `app/broker/cli.py` — official Alpaca CLI executor seam (`SENTRY_EXECUTOR=cli`) — hackathon requirement #2 — SATISFIED: verified against CLI source v0.0.13, binary installed, supervised live round-trip done Aug 28
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
- MUST use Alpaca MCP server or CLI (req #2) — **DONE** (official CLI,
  supervised live round-trip Aug 28; judged week runs SENTRY_EXECUTOR=cli)
- Public GitHub repo (MIT — LICENSE done), one-page write-up (skeleton in
  README: AI logic / risk gates / infra), video + slides + cover image
- Optional: ≤5 build-in-public posts on X/LinkedIn tagging @lablabai + @AlpacaHQ
- Judged week ≈ 5 sessions (Fri 28 partial from 23:00 MYT kickoff, Mon–Thu
  full, Fri 4 until 23:00 MYT). Agent runs 21:15–04:00 MYT nightly on the
  Oracle Cloud VM (see "Judged week — live status").

## Status (update this as things change)
- First overnight run (Aug 26 US session, dev acct): 1 ORB call spread
  767/772, both mleg FILLS verified against Alpaca (+$84 actual), engine
  ran 383 ticks with zero gaps. Live multi-leg fills: VERIFIED.
- Aug 27: 15-agent audit of that run found ~20 real defects; ALL fixed,
  then a 14-agent adversarial review of the fixes found a second wave
  (also fixed): UNKNOWN order state is now never treated as definite —
  unknown entries park in a pending queue (decisions.order_id breadcrumb,
  resolved by later ticks/next boot), a close in flight persists on the
  trade row (trades.close_order_id) and is POLLED, never re-submitted,
  until definitely dead (re-closing a closed spread would open a reverse
  position); restarts also restore each strategy's daily claims from
  today's decision rows; chain fetch paginates (SPY 2-7 DTE = 786
  contracts/right, old code silently truncated at 500); condor risk uses
  actual resolved wing widths; re-arms capped 2/day; bars younger than
  90s not consumed (revision guard); grader rejects Infinity/NaN
  size_frac; boot hard-fails on missing ANTHROPIC key or bad feed name.
  Re-verified: offline suite 14/14, live read-only smoke, real boot
  (reconcile agreed with broker, warm-up rebuilt OR mid-day, claims
  restored). Big ones from the first wave:
  mid-session (re)start used to wipe the OR and kill both strategies
  (fatal for the 23:00-MYT kickoff boot — fixed: rollover owns warm-up,
  `_seen_until` resets with day state); bars were 15-min-delayed SIP
  (now `SENTRY_DATA_FEED=iex` real-time — NOTE: IEX prints differ from
  SIP, last night's 2-cent breakout wouldn't have fired on IEX; a $0.10
  breakout buffer now filters such pokes); time-exit no longer skipped
  when a mark is missing; entries/exits journal ACTUAL FILLS (not
  quote-time marks) after wait_fill confirmation; startup reconciles
  journal vs live positions; no-API-key grader now VETOES (was
  approve-at-half-size); theta DTE 2-4 from config; validator counts
  coverage/ratios; paths anchored to repo root; per-day file logs in
  logs/.
- Req #2: cli.py rewritten against the OFFICIAL CLI source
  (github.com/alpacahq/cli v0.0.13 — `alpaca order submit --order-class
  mleg --legs '<json>'`; env-var auth, paper default; PyPI "alpaca-cli"
  is an UNRELATED third-party package, never install it). Aug 27: binary
  INSTALLED (%LOCALAPPDATA%\alpaca-cli, on user PATH, SHA256 verified vs
  official checksums.txt) and VERIFIED read-only: doctor all green,
  account get = dev PA3VLF2RWGLC (options level 3), `order get --quiet`
  returns exactly the status/filled_qty/filled_avg_price fields wait_fill
  reads, and `order submit --dry-run` reproduces last night's successful
  REST body byte-for-byte. cli_available() + executor=cli startup checks
  pass. Aug 28 22:20 MYT: supervised live round-trip DONE (dev acct,
  1-lot 771/776 call spread, entry fill 1.78 / close 1.72, flat after) —
  **req #2 FULLY SATISFIED**, judged week runs SENTRY_EXECUTOR=cli.
  Same night: grader max_tokens 300 truncated a live verdict mid-JSON
  (fail-closed veto worked, but the signal was lost) — now 1024 +
  stop_reason guard, and grader-layer failures journal as "ai_error"
  and re-arm (capped) instead of consuming the daily shot like a real
  AI veto.
- Dev paper account has orb-trader-style stock/BTC activity in its
  history (that's why equity ≠ 100k). Fine for dev; competition account
  must be freshly created and single-purpose (rule 6).
- Repo is PUBLIC: https://github.com/semicoln-dev/spread-sentry (MIT).
- Known quirks: options snapshot endpoint caps at 100 symbols (batched);
  grader must not see qty (sized after verdict); equity in /health is
  null until the first market-hours snapshot; journal pnl vs account
  day-pnl differ by ~$0.025/contract Alpaca paper fees.

## Judged week — live status (update nightly)
- Host: **Oracle Cloud VM 149.118.129.83** (Ubuntu 24.04, systemd unit
  `spread-sentry`, Restart=always, timezone Asia/Kuala_Lumpur, executor=cli).
  SSH: `ssh -i ~/.ssh/spread-sentry-vm ubuntu@149.118.129.83` (key lives on the
  owner's Windows box + laptop). Dashboard only via tunnel `-L 8000:localhost:8000`
  — port 8000 is NEVER exposed publicly (no auth). Kill switch:
  `touch ~/spread-sentry/HALT`. Pull journal: `scp ubuntu@...:~/spread-sentry/journal.db .`
  Local PC no longer runs the agent (migrated Mon Sep 1, 21:39 MYT, mid-session,
  no state lost). **The VM's journal.db is now the authoritative one.**
- Competition account **PA3J9Y7UA5C8**, started $100,000.
  - Session 1 (Fri Aug 28): iron condor, AI 0.6 size, time exit, **-$114 (-0.13R)**.
  - Session 2 (Mon Aug 31): put debit spread, AI 0.4 size, time exit, **-$200 (-0.24R)**.
    AI's stated reason (weak follow-through) proved correct; its size cut saved
    ~$280 vs the gate cap of 12 contracts.
  - Running: 2 trades, **-$314**, equity ~$99,685.
- Observation, NOT acted on: all 4 trades to date (incl. dev) exited via the
  15:45 time stop — none reached +50% max gain or the 50% stop. Do NOT retune
  mid-judged-week (consistency matters more than a guess); note it in the
  write-up's "what I'd ship next".
- Submission drafts: `WRITEUP.md` (in repo, P&L placeholder to fill Sep 4) and a
  submission kit (video script/slides/cover/checklist) in the owner's scratchpad.
  Deadline **Sep 4, 15:00 UTC = 23:00 MYT**. Owner overseas Sep 2-4 (holiday, can
  submit from laptop).

## Conventions
- Run: `venv\Scripts\Activate.ps1; uvicorn app.main:app` (dashboard :8000)
- Windows/PowerShell: no `&&` chaining; use `;`
- ALL tunables in `app/config.py`; never hardcode in logic files
- Owner prefers concise plain-language explanations, one change at a
  time, validated (here: by tests + the decisions journal) before the next
