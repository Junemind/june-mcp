"""June MCP runtime — configuration, logging, client construction, error mapping.

Everything the stdio server needs to run *safely* inside an agent host lives here
as plain, testable functions (importing this module needs no MCP runtime — same
lazy posture as ``server.py``). Phase-MC design invariants, each structural:

* **Fail-closed configuration.** The server refuses to start without an explicit
  workspace canvas (``JUNE_CANVAS``) and an API key (``JUNE_API_KEY``), so a
  misconfigured agent host can never read or write an unintended workspace. A
  keyless local dev service is an explicit opt-in (``JUNE_ALLOW_ANON=1``), never
  a silent default.
* **stderr-only diagnostics.** With stdio transport *stdout is the wire* — one
  stray byte corrupts JSON-RPC framing and kills the session. All logging is
  configured onto stderr; nothing in this module writes to stdout.
* **Errors are redacted by construction.** Agent-visible error text is built
  ONLY from structured fields (exception type, HTTP status code) — the raw
  exception message, which may embed URLs, headers or key material, is never
  interpolated. There is no filter to bypass because there is nothing to filter.
* **Per-verb timeouts.** Read verbs (search/context/graph) default to 15 s;
  answer-class verbs carry an LLM call and get their own budget (default 120 s,
  consumed by the tool layer). Both env-tunable, never one global knob.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from june_client import JuneClient

# ── environment contract (the only configuration surface) ────────────────────
ENV_BASE_URL = "JUNE_BASE_URL"
ENV_API_KEY = "JUNE_API_KEY"
ENV_CANVAS = "JUNE_CANVAS"
ENV_ALLOW_ANON = "JUNE_ALLOW_ANON"          # "1" → keyless local dev opt-in
ENV_READONLY = "JUNE_READONLY"              # "1" → hide write/maintenance tools
ENV_TIMEOUT_READ = "JUNE_TIMEOUT_READ"      # seconds; search/context/graph verbs
ENV_TIMEOUT_ANSWER = "JUNE_TIMEOUT_ANSWER"  # seconds; answer-class verbs (LLM inside)
ENV_LLM_KEY = "JUNE_LLM_KEY"                # optional; forwarded per-request, never logged
ENV_LOG_LEVEL = "JUNE_LOG_LEVEL"

DEFAULT_TIMEOUT_READ = 15.0
DEFAULT_TIMEOUT_ANSWER = 120.0
_CONNECT_TIMEOUT = 5.0

log = logging.getLogger("june_mcp")


class ConfigError(Exception):
    """Raised by :func:`load_config` with EVERY problem found (not just the first),
    so a misconfigured host is fixed in one round-trip."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class McpConfig:
    """Validated runtime configuration (immutable — nothing mutates config later)."""

    base_url: str
    api_key: str
    canvas: str
    allow_anon: bool = False
    readonly: bool = False
    timeout_read: float = DEFAULT_TIMEOUT_READ
    timeout_answer: float = DEFAULT_TIMEOUT_ANSWER
    llm_key: str = ""


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(env: Mapping[str, str] | None = None) -> McpConfig:
    """Read + validate the environment. Fail-closed: missing required settings
    raise :class:`ConfigError` listing every problem; there are no unsafe defaults."""
    e = os.environ if env is None else env
    problems: list[str] = []

    base_url = e.get(ENV_BASE_URL, "").strip()
    if not base_url:
        problems.append(f"{ENV_BASE_URL} is required (e.g. http://localhost:8000)")
    elif not base_url.startswith(("http://", "https://")):
        problems.append(f"{ENV_BASE_URL} must be an http(s) URL")

    canvas = e.get(ENV_CANVAS, "").strip()
    if not canvas:
        problems.append(
            f"{ENV_CANVAS} is required — the server never runs against an implicit "
            "workspace; create/choose a canvas and set it explicitly")

    allow_anon = _flag(e, ENV_ALLOW_ANON)
    api_key = e.get(ENV_API_KEY, "").strip()
    if not api_key and not allow_anon:
        problems.append(
            f"{ENV_API_KEY} is required (or set {ENV_ALLOW_ANON}=1 explicitly for a "
            "keyless local dev service)")

    def _seconds(name: str, default: float) -> float:
        raw = e.get(name, "").strip()
        if not raw:
            return default
        try:
            v = float(raw)
        except ValueError:
            problems.append(f"{name} must be a number of seconds (got {raw!r})")
            return default
        if v <= 0:
            problems.append(f"{name} must be positive (got {raw!r})")
            return default
        return v

    timeout_read = _seconds(ENV_TIMEOUT_READ, DEFAULT_TIMEOUT_READ)
    timeout_answer = _seconds(ENV_TIMEOUT_ANSWER, DEFAULT_TIMEOUT_ANSWER)

    if problems:
        raise ConfigError(problems)
    return McpConfig(
        base_url=base_url, api_key=api_key, canvas=canvas, allow_anon=allow_anon,
        readonly=_flag(e, ENV_READONLY), timeout_read=timeout_read,
        timeout_answer=timeout_answer, llm_key=e.get(ENV_LLM_KEY, "").strip(),
    )


def configure_logging(env: Mapping[str, str] | None = None) -> None:
    """Route ALL diagnostics to stderr (stdout is the JSON-RPC wire)."""
    e = os.environ if env is None else env
    level = getattr(logging, e.get(ENV_LOG_LEVEL, "INFO").upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stderr, level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")


def make_client(cfg: McpConfig) -> JuneClient:
    """Build the JuneClient the tools run over: read-verb timeout in the transport,
    answer-verb budget + optional BYO LLM key carried BY the client (it owns wire
    shapes and wire budgets — the tool layer never does raw HTTP)."""
    http = httpx.Client(
        base_url=cfg.base_url,
        timeout=httpx.Timeout(
            connect=_CONNECT_TIMEOUT, read=cfg.timeout_read,
            write=cfg.timeout_read, pool=cfg.timeout_read))
    return JuneClient(cfg.base_url, cfg.api_key, client=http, canvas=cfg.canvas,
                      answer_timeout=cfg.timeout_answer, llm_key=cfg.llm_key)


# ── agent-visible error mapping (redaction by construction) ──────────────────
_STATUS_HINTS = {
    400: "the request was invalid",
    401: "the API key was rejected",
    403: "access to this workspace/canvas is denied",
    404: "unknown route or node",
    409: "the write conflicted with existing state",
    413: "the payload is too large",
    422: "the arguments failed validation",
    429: "rate-limited — slow down and retry",
    500: "the service hit an internal error",
    503: "the service is temporarily unavailable",
}


def map_error(exc: BaseException) -> str:
    """Turn any tool failure into a short, actionable, SECRET-FREE message.

    The message is assembled purely from the exception's *type* and (for HTTP
    errors) the *status code* — never from ``str(exc)``, which for transport
    errors can embed the request URL, headers, or key material. Unknown
    exception types collapse to their class name only.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ("June request timed out — the service may be busy or the question "
                "very broad. Retry, or narrow the request.")
    if isinstance(exc, httpx.ConnectError):
        return ("June service is unreachable — check that the service is running "
                "and the configured base URL is correct.")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return f"June returned HTTP {code}: {_STATUS_HINTS.get(code, 'request failed')}."
    if isinstance(exc, KeyError):
        # Raised by run_tool for unknown tool names / missing required args; the
        # payload is tool/arg identifiers (agent-supplied), never service secrets.
        detail = exc.args[0] if exc.args else "unknown key"
        return f"Tool error: {detail}"
    if isinstance(exc, (TypeError, ValueError)):
        return (f"Tool arguments were invalid ({type(exc).__name__}) — check the "
                "tool's input schema and retry.")
    return f"June tool failed ({type(exc).__name__})."


__all__ = [
    "ConfigError", "McpConfig", "configure_logging", "load_config",
    "make_client", "map_error",
    "DEFAULT_TIMEOUT_READ", "DEFAULT_TIMEOUT_ANSWER",
]
