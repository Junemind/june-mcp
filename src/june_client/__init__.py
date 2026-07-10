"""june_client — the official connector SDK for June AI.

A tiny HTTP client a connecting app installs to talk to a June AI service without
hand-rolling REST calls. It depends only on ``httpx`` — no sqlalchemy, no FastAPI,
nothing from the core — so any app can ``pip install june-ai[client]`` (or vendor
this module) regardless of its stack.

    from june_client import JuneClient, node, edge

    with JuneClient("https://june.example", api_key="june_sk_…") as june:
        june.ingest(
            nodes=[node("…id…", "entity", "Fix login bug", source_app="myapp")],
            proposals=[edge("…src…", "entity", "…tgt…", "identity", "authored_by")],
        )
        g = june.graph()

A connector writes zero graph logic — it only says "here are my nodes and the
edges I propose"; June AI owns storage, bounding, ranking, revocation, serving.
"""
from __future__ import annotations

from june_client.client import JuneClient, edge, node

__all__ = ["JuneClient", "node", "edge"]
