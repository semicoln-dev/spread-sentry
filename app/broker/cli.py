"""Official Alpaca CLI execution adapter — HACKATHON REQUIREMENT #2 seam.

The rules require using Alpaca's MCP server OR its CLI. This adapter routes
the whole order lifecycle (submit, fill-poll, close) through the official
`alpaca` CLI (github.com/alpacahq/cli — release binaries; the PyPI package
named "alpaca-cli" is an unrelated third-party project, do NOT install it).

Command shapes verified against the CLI source at v0.0.13:
  - `alpaca order submit --order-class mleg --type market --time-in-force day
     --qty N --legs '<json>'` — internal/cmd/commands.gen.go json.Unmarshals
    --legs straight into the POST /v2/orders body, so legs use the REST
    schema: {"symbol", "ratio_qty" (string), "side"}.
  - `alpaca order get --order-id <id>` — returns the order JSON
    (status, filled_avg_price).
  - Output is JSON by default; --quiet suppresses non-data noise.
  - Auth: the CLI reads ALPACA_API_KEY / ALPACA_SECRET_KEY from the
    environment (paper is its default mode) — the same variables config.py
    loads from .env, so the subprocess inherits the right account.

Flip on with SENTRY_EXECUTOR=cli. main.py refuses to boot in cli mode if the
binary is missing — every ticket dying as submit_failed is a silent no-trade
day, and silence is the one failure mode this project never accepts.
"""
import json
import logging
import shutil
import subprocess

from app.strategy.base import TradeTicket

log = logging.getLogger("sentry.cli")


def cli_available() -> bool:
    return shutil.which("alpaca") is not None


def _run(args: list[str]) -> dict | None:
    exe = shutil.which("alpaca")   # full resolved path: bare names can miss
    if exe is None:                # PATHEXT shims under CreateProcess
        log.error("alpaca CLI not found on PATH — install it or use SENTRY_EXECUTOR=sdk")
        return None
    try:
        out = subprocess.run([exe, *args, "--quiet"], capture_output=True,
                             text=True, timeout=30)
        if out.returncode != 0:
            log.error("alpaca CLI failed: %s", (out.stderr or out.stdout).strip())
            return None
        return json.loads(out.stdout)
    except Exception:
        log.exception("alpaca CLI call failed")
        return None


def _legs_arg(t: TradeTicket, flip: bool) -> str:
    return json.dumps([{
        "symbol": l.symbol,
        "ratio_qty": str(l.ratio),
        "side": (("sell" if l.side == "buy" else "buy") if flip else l.side),
    } for l in t.legs])


def _submit(t: TradeTicket, flip: bool) -> str | None:
    d = _run(["order", "submit",
              "--order-class", "mleg", "--type", "market",
              "--time-in-force", "day", "--qty", str(t.qty),
              "--legs", _legs_arg(t, flip)])
    return d.get("id") if d else None


def submit_mleg(t: TradeTicket) -> str | None:
    return _submit(t, flip=False)


def close_mleg(t: TradeTicket) -> str | None:
    return _submit(t, flip=True)


def order_get(order_id: str) -> dict | None:
    return _run(["order", "get", "--order-id", order_id])
