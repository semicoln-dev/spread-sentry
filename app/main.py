"""FastAPI entrypoint. Run: uvicorn app.main:app"""
import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import engine
from app.config import settings
from app.journal import db

# console AND a rotating file: an unattended overnight session whose only
# record is a scrolled-away terminal is a session that never happened.
# Rotation at 12:00 local (UTC+8) = midnight ET — between US sessions for
# both the nightly-restart and continuous-VM deployments, so one file maps
# to one trading day.
import logging.handlers
_log_dir = Path(settings.log_dir)
_log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(),
              logging.handlers.TimedRotatingFileHandler(
                  _log_dir / "sentry.log", when="midnight",
                  atTime=dt.time(12, 0), backupCount=30, encoding="utf-8")])


def _startup_checks():
    """Misconfiguration must kill the boot, not the night's trading. A
    half-configured agent that runs all night silently doing nothing (or
    silently doing the WRONG thing) is the failure mode we never accept."""
    if settings.executor == "cli":
        from app.broker.cli import cli_available
        if not cli_available():
            raise RuntimeError(
                "SENTRY_EXECUTOR=cli but the official `alpaca` CLI is not on "
                "PATH — install it (github.com/alpacahq/cli releases) or "
                "unset SENTRY_EXECUTOR. Refusing to boot into a silent "
                "submit_failed loop.")
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing — "
                           "check .env at the repo root")
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing — the grader would fail closed and "
            "veto every ticket all night. Fix .env (or export the key) "
            "before starting a session.")
    if settings.data_feed.lower() not in ("iex", "sip"):
        raise RuntimeError(
            f"SENTRY_DATA_FEED={settings.data_feed!r} is not iex or sip — a "
            "typo here must not silently pick a delayed feed.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_checks()
    db.init_db()
    task = engine.start()
    yield
    task.cancel()


app = FastAPI(title="spread-sentry: gated options alpha agent", lifespan=lifespan)


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html")


@app.get("/health")
def health():
    open_t = db.open_trades()
    eq = db.recent("equity", 1)
    return {"status": "ok",
            "executor": settings.executor,
            "data_feed": settings.data_feed,
            "ai": "claude" if settings.anthropic_api_key else "fail-closed (no key)",
            "halted": os.path.exists(settings.kill_file),
            "open_trades": len(open_t),
            "equity": eq[0]["equity"] if eq else None,
            "day_pnl": eq[0]["day_pnl"] if eq else None}


@app.get("/decisions")
def decisions(limit: int = 100):
    return db.recent("decisions", limit)


@app.get("/trades")
def trades(limit: int = 100):
    return db.recent("trades", limit)


@app.get("/analysis")
def analysis():
    return db.analysis()
