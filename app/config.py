"""Central config. ALL knobs live here; never hardcode in logic files."""
import os
from dataclasses import dataclass
from pathlib import Path

# Everything the app persists or reads (journal, kill file, .env, logs) is
# anchored to the repo root, NOT the cwd — launching from the wrong directory
# must never orphan open positions or disarm the kill switch.
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = BASE_DIR / ".env"):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


@dataclass
class Settings:
    alpaca_api_key: str = os.environ.get("ALPACA_API_KEY", "")
    alpaca_secret_key: str = os.environ.get("ALPACA_SECRET_KEY", "")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")

    # --- Instruments ---
    underlying: str = "SPY"            # most liquid US options chain

    # --- Market data feed ---
    # "iex": real-time on the free plan, but thin single-venue prints define
    #        the OR and breakouts (no closing-auction bar).
    # "sip": full consolidated tape, but the free plan silently serves it
    #        ~15 min delayed — every bar-driven entry executes that late.
    # Aug 26 dev session ran on the implicit SIP default: the 14:06 signal
    # submitted at 14:21. Deliberate choice, overridable per-run via env.
    data_feed: str = os.environ.get("SENTRY_DATA_FEED", "iex")

    # --- ORB directional (ported from orb-trader; expressed as debit spreads) ---
    or_minutes: int = 30               # opening range 09:30-10:00 ET
    orb_last_entry_et: str = "14:00"   # no new directional entries after this
    orb_spread_width: float = 5.0      # $ between long and short strikes
    orb_min_dte: int = 2               # skip 0-1 DTE gamma; nearest expiry >= this
    orb_max_dte: int = 7
    orb_break_buffer: float = 0.10     # $ beyond the OR edge before a close counts
                                       # as a breakout (a $0.02 poke is noise, and
                                       # IEX prints are thin) — set 0 to disable

    # --- Theta income (iron condor on range days) ---
    theta_entry_et: str = "11:30"      # only if no ORB fired and day is range-y
    theta_range_max_ratio: float = 0.6 # OR width vs 14-day ATR: below = range day
    theta_short_delta: float = 0.16    # target |delta| of short strikes
    theta_wing_width: float = 5.0      # $ protection wing beyond each short strike
    theta_min_dte: int = 2             # was hardcoded 1 in the strategy: 0-1 DTE
    theta_max_dte: int = 4             # short gamma is exactly what we skip

    # --- Exits (managed every poll) ---
    profit_take_pct: float = 0.5       # debit: +50% of max gain; credit: keep 50% of credit
    stop_loss_pct: float = 0.5         # exit at 50% of max risk lost
    time_exit_et: str = "15:45"        # flatten everything by this time

    # --- HARD RISK GATES (code-enforced; the AI cannot override these) ---
    max_risk_per_trade_usd: float = 2000.0   # 2% of the $100k competition account
    max_open_positions: int = 3
    daily_loss_halt_usd: float = 3000.0      # 3% daily halt, orb-trader style
    no_entries_after_et: str = "15:30"
    defined_risk_only: bool = True           # every short leg must have a wing
    kill_file: str = str(BASE_DIR / "HALT")  # touch this file -> no new entries

    # --- AI risk officer ---
    ai_model: str = "claude-sonnet-5"
    ai_max_size_frac: float = 1.0      # AI may size DOWN from gate cap, never up

    # --- Execution seam: "sdk" (alpaca-py, default) or "cli" (official Alpaca CLI,
    # github.com/alpacahq/cli). Hackathon requires MCP server or CLI usage — see
    # README "Requirement #2". In cli mode the app refuses to start if the
    # binary is missing (a silent submit_failed loop is worse than no boot).
    executor: str = os.environ.get("SENTRY_EXECUTOR", "sdk")

    # --- Order lifecycle ---
    fill_timeout_s: int = 30           # market mleg on paper fills in ms; a fill
                                       # not confirmed inside this window goes to
                                       # the pending-unknown queue (cancel + poll)
    bar_finality_s: int = 90           # ignore bars younger than this (measured
                                       # from bar START): a forming/revisable bar
                                       # must not fire a breakout it later unwinds

    # --- Engine ---
    poll_seconds: int = 60             # REST polling; deliberately no websockets
    db_path: str = str(BASE_DIR / "journal.db")
    log_dir: str = str(BASE_DIR / "logs")


settings = Settings()
