"""Messages that state only what is known (fix plan S5, 2026-09-23).

The Incidents page calls this class the LYING PROBE: a message that names one cause it cannot
know. "jobs vanish on restart" for a 404 that a typo produces just as well; "the service may be
busy" for a timeout whose phase says the connection was never made; "receipts are off" when the
beacon that would say so could not be read; "access denied" in place of the engine's own reason.
An agent acts on the one cause it is given — restarts things, waits, gives up — and the real
cause goes unfixed.

The rule every function here keeps:

* say WHAT happened, from facts in hand: the status, the phase, the budget, the id, the canvas;
* when the cause is not known, list the POSSIBLE causes as possibilities, never one as certain;
* prefer the ENGINE'S OWN reason (its ``detail``) over a generic hint for the status code;
* for anything that may have written, say it may have been applied and give the read that settles
  it BEFORE any retry;
* every suggested next call carries the canvas the failed call used.

Everything returned is composed from literals, status codes, timeout numbers, identifiers the
agent supplied, and the engine's ``detail`` after ``_clean`` — never from ``str(exc)``, which for
a transport error can carry URLs, headers or key material. That is why ``runtime.map_error`` may
pass these texts through verbatim.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

import httpx

_DETAIL_MAX = 300
# Anything that looks like a credential is cut from engine text before it reaches an agent. The
# engine should never put one in a `detail`; this is the belt for the day it does.
_SECRETISH = re.compile(r"(june_sk_\w+|sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|api[_-]?key\s*[=:]\s*\S+)",
                        re.IGNORECASE)
_URL = re.compile(r"https?://\S+")

_STATUS_WORDS = {
    400: "the request was invalid",
    401: "the API key was rejected",
    403: "the request was refused for this key or canvas",
    404: "the thing addressed was not found",
    409: "the write conflicted with the current state",
    413: "the payload is too large",
    422: "the arguments failed validation",
    429: "too many requests (rate limit)",
    500: "the engine hit an internal error",
    502: "a gateway in front of the engine failed",
    503: "the engine is not available right now",
    504: "a gateway in front of the engine timed out",
}


def _clean(text: Any) -> str:
    """Engine text made safe to show an agent: credentials and URLs cut, whitespace folded,
    length bounded. Returns '' for anything that is not a non-empty string."""
    if not isinstance(text, str):
        return ""
    t = _URL.sub("<url>", _SECRETISH.sub("<redacted>", text))
    t = " ".join(t.split())
    return t[:_DETAIL_MAX] + ("…" if len(t) > _DETAIL_MAX else "")


def possible(causes: Iterable[str]) -> str:
    """'possible causes: a; b; or c' — possibilities, in the order given, none asserted."""
    c = [x for x in causes if x]
    if not c:
        return ""
    if len(c) == 1:
        return f"possible cause: {c[0]}"
    return "possible causes: " + "; ".join(c[:-1]) + f"; or {c[-1]}"


def call(tool: str, canvas: str | None = None, **arguments: Any) -> str:
    """A suggested next call, spelled the way an agent writes it, carrying the canvas."""
    args = {**arguments}
    if canvas and canvas != "home":
        args["canvas"] = canvas
    inner = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{tool}({inner})"


def engine_reason(response: httpx.Response | None) -> str:
    """The engine's own ``detail`` for a failed response, cleaned — or '' when it gave none (or
    gave the framework's generic 'Not Found', which says nothing the status does not)."""
    if response is None:
        return ""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is not a reason
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):                  # FastAPI validation errors
        parts = []
        for d in detail[:3]:
            if isinstance(d, dict):
                loc = ".".join(str(x) for x in (d.get("loc") or [])[1:])
                parts.append(f"{loc}: {d.get('msg')}" if loc else str(d.get("msg")))
        detail = "; ".join(parts)
    text = _clean(detail)
    return "" if text.lower() in ("not found", "") else text


def not_found(kind: str, ident: str, canvas: str | None, causes: Iterable[str],
              next_call: str | None = None) -> str:
    """A 404 for something the caller named: what was looked for, where, the possibilities."""
    where = f"canvas {canvas}" if canvas and canvas != "home" else "the home workspace"
    msg = f"no {kind} {ident!r} in {where}; {possible(causes)}."
    if next_call:
        msg += f" To see what exists: {next_call}."
    return msg


def timeout_phase(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect"
    if isinstance(exc, httpx.WriteTimeout):
        return "write"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool"
    if isinstance(exc, httpx.ReadTimeout):
        return "read"
    return "unknown"


def _budget(exc: BaseException, phase: str) -> float | None:
    try:
        t = exc.request.extensions.get("timeout") or {}   # type: ignore[union-attr]
        v = t.get(phase)
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


_PHASE_MEANS = {
    "connect": "the connection to the engine was never established",
    "write": "sending the request to the engine did not finish",
    "pool": "no free connection to the engine became available",
    "read": "the engine did not answer in time after receiving the request",
    "unknown": "the request did not complete in time",
}


def timeout(tool: str, exc: BaseException, *, writes: bool, canvas: str | None = None,
            read_call: str | None = None, budget_class: str | None = None,
            budget_env: str | None = None) -> str:
    """A timeout, by phase and budget, and what it means for a retry.

    ``budget_class`` / ``budget_env`` (S6) are the tool's declared time-budget class and the
    variable that sets it - facts of the connector's own configuration, so the message can name
    the knob. A retrieval-class read is told that narrowing may not help, because that verb
    reranks every candidate the canvas yields whatever the request asks for (a property of the
    code path, not a guess about this failure)."""
    phase = timeout_phase(exc)
    b = _budget(exc, phase)
    if b is not None:
        budget = f" ({b:g} s budget" + (f", set by {budget_env}" if budget_env else "") + ")"
    else:
        budget = f" (budget set by {budget_env})" if budget_env else ""
    msg = f"{tool} timed out in the {phase} phase{budget}: {_PHASE_MEANS[phase]}."
    if writes and phase in ("read", "unknown"):
        msg += (" The engine received this write and may have APPLIED it. Check before retrying"
                + (f": {read_call}" if read_call else " (read what it would have changed)")
                + " — a blind retry can apply it twice.")
    elif writes and phase in ("connect", "pool"):
        msg += " Nothing reached the engine, so the write was not applied; retrying is safe."
    elif writes:
        msg += " The engine may have received part of the request; check before retrying."
    elif budget_class == "retrieval" and phase in ("read", "unknown"):
        msg += (" A read is safe to retry. This verb reranks every candidate the canvas yields, "
                "whatever token_budget or max_items asks for, so narrowing the request may not "
                "shorten it" + (f"; if it keeps timing out, raise {budget_env}" if budget_env else "")
                + ", or use june_search, which does not rerank.")
    else:
        msg += (" A read is safe to retry; if it keeps timing out, narrow the request."
                if phase in ("read", "unknown") else " A read is safe to retry.")
    return msg


def http_failure(tool: str, exc: httpx.HTTPStatusError, *, writes: bool,
                 canvas: str | None = None) -> str:
    """Any HTTP failure: the status, the engine's own reason when it gave one, and — for a 404
    with no reason — the possibilities, never one of them as the cause."""
    status = exc.response.status_code
    reason = engine_reason(exc.response)
    words = _STATUS_WORDS.get(status, "the request failed")
    msg = f"{tool}: June returned HTTP {status} ({words})"
    if reason:
        msg += f" — the engine says: {reason}"
    elif status == 404:
        msg += "; " + possible([
            "the id or name does not exist in this canvas",
            "it was deleted",
            "the canvas itself no longer exists",
            "this engine does not serve that route (an older or differently configured engine)",
        ])
    msg += "."
    if writes and status >= 500:
        msg += " A server error after a write does not prove nothing was written; read before retrying."
    return msg


__all__ = ["call", "engine_reason", "http_failure", "not_found", "possible", "timeout",
           "timeout_phase"]
