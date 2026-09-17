"""What this June engine can actually do for this connection — resolved ONCE at startup.

Design decision D1 (2026-09-17, tool-surface design §12): the connector lists tools by what the
engine SERVES and what the key is ENTITLED to, never by a tier string alone. Two failures drove it:

* N1 — ``__main__`` set ``pro = (tier == "pro")``, so paying ``pro-trial``, ``power`` and ``team``
  customers lost agent page authoring even though the engine grants them the Pro feature set.
* Hosted (``api.june.januraine.ai``) serves no pages at all (``/v1/pages`` → 404: pages need
  ``JUNE_PAGES=1``), yet 0.4.1 listed all 30 tools there — 11 of them could never succeed.

Resolution order, most authoritative first:

1. ``/v1/whoami`` ``capabilities`` (a list of strings) — reserved for engines that advertise
   ``pages`` / ``agent_pages`` explicitly. Not served by any engine today; honoured if present so
   the engine can take over without a connector release.
2. Entitlement: ``features`` non-empty (the engine's Pro feature set) or ``tier`` in the engine's
   own paid tiers (``entitlements.KNOWN_TIERS`` minus free).
3. Served surface: one ``GET /v1/pages?limit=1`` probe — 404 means the engine does not serve
   pages, so every page-backed tool is absent (hidden AND refused, the ``Tool.available`` shape).

FAIL-OPEN, as before: an unreachable or legacy whoami never locks a paying user out; only an
explicit signal removes a tool.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("june_mcp")

# Mirrors the engine's entitlements.KNOWN_TIERS minus free. Kept here (not imported) because the
# connector never depends on engine packages; a test in the engine repo pins the two together.
PRO_TIERS: frozenset[str] = frozenset({"pro", "pro-trial", "power", "team"})

# Tools that cannot work on an engine that serves no pages: page authoring, page reads, and the
# agent docs (which are pages in the docs canvas) including the repo-sync tools built on them.
NEEDS_PAGES: frozenset[str] = frozenset({
    "june_page_list", "june_page_get", "june_page_create", "june_page_write", "june_page_append",
    "june_page_update", "june_page_delete",
    "june_docs_refresh", "june_doc_list", "june_doc_get", "june_doc_save", "june_doc_delete",
    "june_learn", "june_docs_export", "june_page_export", "june_page_import",
})


@dataclass(frozen=True)
class Capabilities:
    pro: bool = True                 # agent page authoring entitled
    pages: bool = True               # engine serves /v1/pages
    tier: str = ""                   # as reported (display)
    features: tuple[str, ...] = ()   # as reported (display)
    edition_tag: str = ""            # as reported (display)
    source: str = "fail-open"        # which rule decided: capabilities | entitlement | probe | fail-open

    @property
    def absent(self) -> frozenset[str]:
        """Tool names this connection must neither list nor run."""
        return NEEDS_PAGES if not self.pages else frozenset()

    def banner(self) -> str:
        pro = "Pro" if self.pro else "not Pro"
        pages = "pages served" if self.pages else "NO pages on this engine (page/doc tools hidden)"
        return f"{pro} · {pages} · via {self.source}"


def _probe_pages(client) -> bool | None:
    """True/False when the engine answered, None when it could not be asked (fail-open)."""
    try:
        client.list_pages(limit=1)
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return False
        return True          # 401/403/5xx: the route exists; entitlement/health are other questions
    except Exception:        # noqa: BLE001 - transport trouble is not "no pages"
        return None


def resolve(client) -> Capabilities:
    """One whoami (+ one pages probe when whoami does not settle it). Never raises."""
    try:
        who = client.whoami() or {}
    except Exception:  # noqa: BLE001
        return Capabilities(source="fail-open")
    tier = str(who.get("tier") or "").strip().lower()
    tag = str(who.get("edition_tag") or "").strip()
    features = tuple(sorted(str(f) for f in (who.get("features") or []) if f))
    caps = who.get("capabilities")
    if isinstance(caps, list) and caps:
        caps_set = {str(c).strip().lower() for c in caps}
        return Capabilities(pro="agent_pages" in caps_set or "pro" in caps_set,
                            pages="pages" in caps_set, tier=tier, features=features,
                            edition_tag=tag, source="capabilities")
    # Entitlement: only an EXPLICIT free/unknown tier with no Pro feature turns Pro off.
    pro = bool(features) or tier in PRO_TIERS or tier == ""
    probed = _probe_pages(client)
    pages = True if probed is None else probed
    return Capabilities(pro=pro, pages=pages, tier=tier, features=features, edition_tag=tag,
                        source="probe" if probed is not None else "entitlement")


__all__ = ["Capabilities", "NEEDS_PAGES", "PRO_TIERS", "resolve"]
