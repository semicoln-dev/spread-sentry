"""FastAPI entrypoint. Run: uvicorn app.main:app"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import engine
from app.config import settings
from app.journal import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    import os
    return {"status": "ok",
            "executor": settings.executor,
            "ai": "claude" if settings.anthropic_api_key else "fallback",
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
