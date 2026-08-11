"""Short-lived stream tickets for the MJPEG replay.

The problem: the annotated replay is ``multipart/x-mixed-replace``, which a
browser renders natively in an ``<img>`` tag — and an ``<img>`` tag cannot carry
an ``Authorization`` header. That is the same constraint that already made
``/api/evidence`` a public route (see gateway/auth.py ``_PUBLIC``), but camera
footage is not an acceptable thing to serve unauthenticated.

The solution used here is the one this codebase already reaches for when a
browser API cannot send a header: a token in the URL, exactly like the WebSocket
handshake (``/api/ws?token=``, web/src/hooks/useGatewaySocket.ts). The difference
is that this ticket is *not* the caller's JWT — it is an opaque, unguessable,
minutes-long credential that grants exactly one thing: viewing one analysis's
replay.

  * Minted only by an authenticated, RBAC-checked POST.
  * Bound to a single ``analysis_id`` — a leaked ticket cannot browse others.
  * Expires in ``SECUREVISION_STREAM_TICKET_TTL_S`` seconds (default 120).
  * Records the actor, so a stream open is attributable in the logs.

Not single-use: a browser may re-request an ``<img>`` source (reconnect, cache
revalidation, a second monitor showing the same board), and burning the ticket
on first read would break the ordinary case while adding little — the TTL is
already short and the scope is already one analysis.

In-process, like everything else about an analysis. A gateway restart
invalidates outstanding tickets; the UI simply mints another.
"""
from __future__ import annotations

import os
import secrets
import time
from threading import Lock
from typing import Dict, Optional

DEFAULT_TTL_S = 120.0
#: Bound on outstanding tickets, so a scripted caller cannot grow the map.
MAX_TICKETS = 500

_lock = Lock()
_tickets: Dict[str, Dict[str, object]] = {}


def ttl_seconds() -> float:
    raw = (os.environ.get("SECUREVISION_STREAM_TICKET_TTL_S") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TTL_S
    except ValueError:
        return DEFAULT_TTL_S
    return value if value > 0 else DEFAULT_TTL_S


def _purge(now: float) -> None:
    """Drop expired tickets. Called on every mint/redeem, so the map stays
    proportional to live viewers rather than to total views."""
    expired = [key for key, row in _tickets.items()
               if float(row["expires_at"]) <= now]  # type: ignore[arg-type]
    for key in expired:
        _tickets.pop(key, None)


def issue(analysis_id: str, *, actor: Optional[str] = None) -> Dict[str, object]:
    """Mint a ticket for one analysis. Returns ``{ticket, expires_in, ...}``."""
    now = time.time()
    ttl = ttl_seconds()
    token = secrets.token_urlsafe(32)
    with _lock:
        _purge(now)
        if len(_tickets) >= MAX_TICKETS:
            # Evict the soonest-to-expire rather than refusing: the cap is a
            # memory bound, not a rate limit.
            oldest = min(_tickets, key=lambda k: _tickets[k]["expires_at"])  # type: ignore[index]
            _tickets.pop(oldest, None)
        _tickets[token] = {
            "analysis_id": analysis_id,
            "actor": actor,
            "expires_at": now + ttl,
        }
    return {"ticket": token, "analysis_id": analysis_id, "expires_in": int(ttl)}


def redeem(token: Optional[str], analysis_id: str) -> Optional[Dict[str, object]]:
    """Validate a ticket for this analysis. Returns the record, or None when the
    ticket is unknown, expired, or minted for a different analysis."""
    if not token:
        return None
    now = time.time()
    with _lock:
        _purge(now)
        row = _tickets.get(token)
        if not row:
            return None
        if row["analysis_id"] != analysis_id:
            return None
        return dict(row)


def revoke(token: str) -> None:
    with _lock:
        _tickets.pop(token, None)


def reset() -> None:
    """Drop every outstanding ticket (tests)."""
    with _lock:
        _tickets.clear()


def outstanding() -> int:
    with _lock:
        _purge(time.time())
        return len(_tickets)


__all__ = ["issue", "redeem", "revoke", "reset", "outstanding", "ttl_seconds",
           "DEFAULT_TTL_S", "MAX_TICKETS"]
