"""Alpaca CLI execution adapter — HACKATHON REQUIREMENT #2 seam.

The rules require using Alpaca's MCP server OR its CLI. This adapter routes
order placement through the `alpaca` CLI (structured JSON in/out, built for
agent sessions). Flip it on with SENTRY_EXECUTOR=cli once you have:

    1. installed the CLI and run `alpaca configure` with the HACKATHON
       account keys (paper),
    2. VERIFIED the command templates below against the current CLI docs
       (https://docs.alpaca.markets — "Trading CLI") — they are written
       from the docs but not yet exercised against a live install.

Until then the SDK path (default) keeps the agent trading, and this file is
the honest TODO. The AI grader's MCP hookup is the alternative route to the
same requirement — see README "Requirement #2".
"""
import json
import logging
import shutil
import subprocess

from app.strategy.base import TradeTicket

log = logging.getLogger("sentry.cli")


def _run(args: list[str]) -> dict | None:
    if shutil.which("alpaca") is None:
        log.error("alpaca CLI not found on PATH — install it or use SENTRY_EXECUTOR=sdk")
        return None
    try:
        out = subprocess.run(["alpaca", *args], capture_output=True, text=True,
                             timeout=30)
        if out.returncode != 0:
            log.error("alpaca CLI failed: %s", out.stderr.strip())
            return None
        return json.loads(out.stdout)
    except Exception:
        log.exception("alpaca CLI call failed")
        return None


def _legs_arg(t: TradeTicket, flip: bool) -> str:
    return json.dumps([{
        "symbol": l.symbol,
        "ratio_qty": l.ratio,
        "side": (("sell" if l.side == "buy" else "buy") if flip else l.side),
    } for l in t.legs])


def submit_mleg(t: TradeTicket) -> str | None:
    d = _run(["orders", "create", "--json",
              "--order-class", "mleg", "--type", "market",
              "--time-in-force", "day", "--qty", str(t.qty),
              "--legs", _legs_arg(t, flip=False)])
    return d.get("id") if d else None


def close_mleg(t: TradeTicket) -> str | None:
    d = _run(["orders", "create", "--json",
              "--order-class", "mleg", "--type", "market",
              "--time-in-force", "day", "--qty", str(t.qty),
              "--legs", _legs_arg(t, flip=True)])
    return d.get("id") if d else None
