"""The AI risk officer. Claude receives a resolved TradeTicket plus account
context and returns a verdict. Its powers are deliberately asymmetric:

    - it can VETO a trade the strategies proposed
    - it can SHRINK the size below the risk-gate cap
    - it can NEVER propose a trade, raise size, or touch the hard gates

Fail-closed, with no exceptions: a missing API key, an API error, and an
unparseable reply all VETO with the reason journaled. An absent risk officer
does not approve trades — that asymmetry is the whole point of the project,
and a misconfigured .env on a fresh VM must not quietly trade without it.
"""
import json
import logging
import math
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


def _parse_grade(text: str) -> Grade:
    """Pure parse of the model's reply — anything malformed, non-JSON,
    non-finite, or out of contract collapses to a veto. json.loads accepts
    Infinity/NaN literals, so finiteness is checked explicitly: a non-finite
    size_frac must never clamp into a full-size take."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    d = json.loads(text)
    if not isinstance(d, dict):
        return Grade("veto", 0.0, "AI reply was not a JSON object — failing "
                     "closed", settings.ai_model)
    verdict = d.get("verdict", "veto")
    if verdict not in ("take", "veto"):
        verdict = "veto"
    frac = float(d.get("size_frac", 0.0))
    if not math.isfinite(frac):
        return Grade("veto", 0.0, f"non-finite size_frac {frac!r} — failing "
                     "closed", settings.ai_model)
    frac = max(0.0, min(frac, settings.ai_max_size_frac))
    return Grade(verdict, frac, str(d.get("reason", ""))[:500], settings.ai_model)


def grade(ticket: TradeTicket, context: dict) -> Grade:
    """context: {"day_pnl_usd", "open_positions", "equity", "recent_notes"}"""
    if not settings.anthropic_api_key:
        return Grade("veto", 0.0, "no ANTHROPIC_API_KEY: the risk officer is "
                     "absent, failing closed", "fallback")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        tk = {**asdict(ticket), "ts": ticket.ts.isoformat() if ticket.ts else None}
        tk.pop("qty", None)          # sized after grading; showing 0 misleads
        payload = {"ticket": tk, "account": context}
        msg = client.messages.create(
            model=settings.ai_model,
            max_tokens=1024,   # 300 clipped a reply mid-JSON on Aug 28 —
                               # a truncated verdict is an error, but a budget
                               # that never truncates is better than a veto
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        if msg.stop_reason == "max_tokens":
            raise ValueError("AI reply truncated at max_tokens — refusing to "
                             "parse a clipped verdict")
        text = "".join(b.text for b in msg.content if b.type == "text")
        return _parse_grade(text)
    except Exception as e:
        log.exception("grader failed — failing CLOSED (veto)")
        return Grade("veto", 0.0, f"AI grader error, failing closed: {e}", "error")
