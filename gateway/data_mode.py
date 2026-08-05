"""Request-time LIVE / DEMO data-source selector.

The dashboards can show EITHER the JNPA-API-sourced corpus (LIVE) or the
manually-imported corpus (DEMO). Every ingested row carries a ``data_origin``
tag (``'API'`` | ``'MANUAL'``); the front-end sends its choice as the
``X-Data-Mode`` request header (``LIVE`` | ``DEMO``) — or a ``?source=`` query
param — and this dependency normalises it to the ``data_origin`` value the
repositories filter on, or ``None`` (no header → no filter → show everything).

This mirrors the existing Gate-Documents ``source`` precedent
(``services/gate_documents/repository.py``): a router reads an optional
provenance selector, drops it into the ``filters`` mapping, and the repo
``_where`` helper narrows the SQL only when it is present.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request

# Accepted synonyms → canonical data_origin. Anything else (incl. "all") means
# "no filter".
_LIVE = {"live", "api"}
_DEMO = {"demo", "manual"}


def resolve_data_origin(raw: Optional[str]) -> Optional[str]:
    """Map a raw LIVE/DEMO (or API/MANUAL) selector to a data_origin, else None."""
    if not raw:
        return None
    value = raw.strip().lower()
    if value in _LIVE:
        return "API"
    if value in _DEMO:
        return "MANUAL"
    return None


def data_mode(request: Request) -> Optional[str]:
    """FastAPI dependency → ``'API'`` | ``'MANUAL'`` | ``None``.

    Reads the ``X-Data-Mode`` header first, then a ``?source=`` query param as a
    fallback (so a link can pin a mode). Absent/unknown ⇒ ``None`` ⇒ unfiltered.
    """
    raw = request.headers.get("X-Data-Mode") or request.query_params.get("source")
    return resolve_data_origin(raw)


__all__ = ["data_mode", "resolve_data_origin"]
