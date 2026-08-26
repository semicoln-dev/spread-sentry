"""SQLite journal — the audit trail the judges (and the write-up) get.

Every ticket, every AI verdict (reasoning verbatim), every gate decision,
every fill and exit with R-multiples. R is exact here: defined-risk
structures make max risk a known number at entry. Rows are never deleted —
the journal is the evidence.
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict

from app.config import settings
from app.strategy.base import TradeTicket

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, strategy TEXT NOT NULL, structure TEXT NOT NULL,
    direction TEXT NOT NULL, thesis TEXT, legs TEXT,
    est_cost REAL, max_risk REAL, max_gain REAL, qty INTEGER,
    ai_verdict TEXT, ai_size_frac REAL, ai_reason TEXT, ai_model TEXT,
    gates_ok INTEGER, gates_reason TEXT, outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER, opened_ts TEXT NOT NULL, strategy TEXT NOT NULL,
    structure TEXT NOT NULL, direction TEXT NOT NULL, legs TEXT NOT NULL,
    qty INTEGER NOT NULL, est_cost REAL NOT NULL, max_risk REAL NOT NULL,
    max_gain REAL NOT NULL, order_id TEXT,
    closed_ts TEXT, exit_value REAL, exit_how TEXT,
    pnl_usd REAL, r_multiple REAL
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY, equity REAL NOT NULL, day_pnl REAL NOT NULL
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(settings.db_path)
    try:
        c.row_factory = sqlite3.Row
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)


def _legs_json(t: TradeTicket) -> str:
    return json.dumps([asdict(l) for l in t.legs])


def log_decision(t: TradeTicket, grade, gates_ok: bool, gates_reason: str,
                 outcome: str) -> int:
    """outcome: submitted | ai_veto | gate_reject | unresolvable | submit_failed"""
    with conn() as c:
        cur = c.execute(
            """INSERT INTO decisions (ts, strategy, structure, direction, thesis,
               legs, est_cost, max_risk, max_gain, qty, ai_verdict, ai_size_frac,
               ai_reason, ai_model, gates_ok, gates_reason, outcome)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.ts.isoformat() if t.ts else None, t.strategy, t.structure,
             t.direction, t.thesis, _legs_json(t), t.est_cost_per_contract,
             t.max_risk_per_contract, t.max_gain_per_contract, t.qty,
             grade.verdict if grade else None,
             grade.size_frac if grade else None,
             grade.reason if grade else None,
             grade.model if grade else None,
             int(gates_ok), gates_reason, outcome))
        return cur.lastrowid


def open_trade(decision_id: int, t: TradeTicket, order_id: str) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO trades (decision_id, opened_ts, strategy, structure,
               direction, legs, qty, est_cost, max_risk, max_gain, order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (decision_id, t.ts.isoformat() if t.ts else None, t.strategy,
             t.structure, t.direction, _legs_json(t), t.qty,
             t.est_cost_per_contract, t.max_risk_per_contract,
             t.max_gain_per_contract, order_id))
        return cur.lastrowid


def close_trade(trade_id: int, closed_ts: str, exit_value: float, how: str):
    with conn() as c:
        row = c.execute("SELECT est_cost, max_risk, qty FROM trades WHERE id=?",
                        (trade_id,)).fetchone()
        pnl = round((exit_value - row["est_cost"]) * 100 * row["qty"], 2)
        r = round(pnl / (row["max_risk"] * row["qty"]), 2) if row["max_risk"] else 0.0
        c.execute("""UPDATE trades SET closed_ts=?, exit_value=?, exit_how=?,
                     pnl_usd=?, r_multiple=? WHERE id=?""",
                  (closed_ts, exit_value, how, pnl, r, trade_id))


def open_trades() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE closed_ts IS NULL ORDER BY opened_ts")]


def snapshot_equity(ts: str, equity: float, day_pnl: float):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO equity (ts, equity, day_pnl) VALUES (?,?,?)",
                  (ts, equity, day_pnl))


def recent(table: str, limit: int = 100) -> list[dict]:
    assert table in ("decisions", "trades", "equity")
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?"
            if table != "equity" else
            "SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,))]


def analysis() -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT strategy, COUNT(*) AS closed,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(pnl_usd), 2) AS pnl_usd,
                   ROUND(AVG(r_multiple), 3) AS avg_r,
                   ROUND(SUM(r_multiple), 2) AS total_r
            FROM trades WHERE closed_ts IS NOT NULL GROUP BY strategy""").fetchall()
        return [dict(r) for r in rows]
