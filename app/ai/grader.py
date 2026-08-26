"""The AI risk officer. Claude receives a resolved TradeTicket plus account
context and returns a verdict. Its powers are deliberately asymmetric:

    - it can VETO a trade the strategies proposed
    - it can SHRINK the size below the risk-gate cap
    - it can NEVER propose a trade, raise size, or touch the hard gates

Fail-closed: no API key -> deterministic fallback (approve at half size);
API error or unparseable reply -> VETO with the error journaled. An AI
outage can only ever make the system MORE conservative.
"""
import json
import logging
from dataclasses import asdict, dataclass

from app.config import settings
from app.strategy.base import TradeTicket

log = logging.getLogger("sentry.grader")

SYSTEM = """You are the risk officer for a defined-risk options trading agent
in a paper-trading competition. Deterministic strategies propose trades; hard
code gates cap size and risk. Your job is judgment the code cannot provide:
does THIS setup, in THIS market context, deserve full size, reduced size, or
a veto?

Tickets reach you UNSIZED by design: contract count is computed after your
verdict as size_frac x the hard risk cap. Judge the setup and its per-contract
economics, not the absence of a quantity.

You cannot create trades or increase size. Reply with ONLY a JSON object:
{"verdict": "take" | "veto",
 "size_frac": 0.0-1.0,
 "reason": "<one or two sentences>"}"""


@dataclass
class Grade:
    verdict: str          # "take" | "veto"
    size_frac: float      # 0..1 of the gate cap
    reason: str
    model: str            # model id, "fallback", or "error"


def _fallback(ticket: TradeTicket) -> Grade:
    return Grade("take", 0.5, "no ANTHROPIC_API_KEY: deterministic fallback "
                 "approves at half size", "fallback")


def grade(ticket: TradeTicket, context: dict) -> Grade:
    """context: {"day_pnl_usd", "open_positions", "equity", "recent_notes"}"""
    if not settings.anthropic_api_key:
        return _fallback(ticket)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        tk = {**asdict(ticket), "ts": ticket.ts.isoformat() if ticket.ts else None}
        tk.pop("qty", None)          # sized after grading; showing 0 misleads
        payload = {"ticket": tk, "account": context}
        msg = client.messages.create(
            model=settings.ai_model,
            max_tokens=300,
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        d = json.loads(text)
        verdict = d.get("verdict", "veto")
        if verdict not in ("take", "veto"):
            verdict = "veto"
        frac = max(0.0, min(float(d.get("size_frac", 0.0)), settings.ai_max_size_frac))
        return Grade(verdict, frac, str(d.get("reason", ""))[:500], settings.ai_model)
    except Exception as e:
        log.exception("grader failed — failing CLOSED (veto)")
        return Grade("veto", 0.0, f"AI grader error, failing closed: {e}", "error")
