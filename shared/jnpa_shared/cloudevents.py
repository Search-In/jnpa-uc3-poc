"""CloudEvents 1.0 envelope for the JNPA UC-III event backbone.

Why this exists
---------------
The PoC's faithfulness requirement is that *simulated* events flow through the
exact same pipeline as *live* events, and the dashboard cannot tell them apart
**except** via an explicit mode badge. To make that distinction machine-readable
without changing any payload, every event published to Kafka is wrapped in a
CloudEvents 1.0 *structured-mode* JSON envelope that carries two extension
attributes:

* ``sourcesystem`` — ``SIM`` or ``LIVE`` (lower-cased attribute name per the
  CloudEvents naming rule; the *value* is upper-case for readability).
* ``rawref`` — an opaque reference to the originating raw artefact (e.g. the
  replay clip frame, the seed stream offset, or the upstream txn id) so an
  operator can trace a SIM event back to what produced it.

The envelope is opt-in (``settings.cloudevents_enabled``) and fully
back-compatible: consumers that predate it call :func:`unwrap`, which returns
the inner payload unchanged for a bare (non-CloudEvents) message. So a mixed
fleet — some producers wrapping, some not — keeps working during rollout.

This module deliberately has **no third-party dependency**: a CloudEvents
structured JSON envelope is just a dict with reserved keys, and keeping it
dependency-light means any service can ``from jnpa_shared import cloudevents``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

# CloudEvents 1.0 spec version string (the ``specversion`` context attribute).
SPEC_VERSION = "1.0"

# Default ``source`` URI-reference for events that don't supply one. CloudEvents
# requires ``source`` to be a non-empty URI-reference; this is a stable scheme.
DEFAULT_SOURCE = "/jnpa/uc3"


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def wrap(
    payload: Any,
    *,
    event_type: str,
    source_system: str = "SIM",
    raw_ref: Optional[str] = None,
    source: str = DEFAULT_SOURCE,
    subject: Optional[str] = None,
    event_id: Optional[str] = None,
    time_iso: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Wrap ``payload`` in a CloudEvents 1.0 structured-mode envelope.

    Parameters
    ----------
    payload:
        The inner event. A pydantic model (anything with ``model_dump``) is
        serialised to a JSON-mode dict; dicts/primitives pass through.
    event_type:
        The ``type`` context attribute, e.g. ``jnpa.anpr.detection``.
    source_system:
        ``"SIM"`` or ``"LIVE"`` — surfaced as the ``sourcesystem`` extension.
    raw_ref:
        Optional opaque pointer to the originating artefact (``rawref``).
    event_id / time_iso:
        Override the auto-generated id / timestamp (e.g. for deterministic
        replay, pass a seed-derived id so the same run yields the same id).
    """
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")
    else:
        data = payload

    envelope: Dict[str, Any] = {
        "specversion": SPEC_VERSION,
        "id": event_id or uuid4().hex,
        "source": source,
        "type": event_type,
        "time": time_iso or _utcnow_iso(),
        "datacontenttype": "application/json",
        # --- extension attributes (lower-case names per CE naming rule) ---
        "sourcesystem": (source_system or "SIM").upper(),
        "data": data,
    }
    if subject is not None:
        envelope["subject"] = subject
    if raw_ref is not None:
        envelope["rawref"] = raw_ref
    if extra:
        envelope.update(extra)
    return envelope


def is_cloudevent(obj: Any) -> bool:
    """True if ``obj`` looks like a CloudEvents structured envelope."""
    return (
        isinstance(obj, dict)
        and obj.get("specversion") == SPEC_VERSION
        and "type" in obj
        and "data" in obj
    )


def unwrap(obj: Any) -> Any:
    """Return the inner payload from a CloudEvents envelope, else ``obj`` as-is.

    Back-compat shim: a consumer can call ``unwrap`` on every message and get
    the payload whether or not the producer wrapped it.
    """
    if is_cloudevent(obj):
        return obj.get("data")
    return obj


def source_system_of(obj: Any) -> Optional[str]:
    """Return the ``sourcesystem`` extension (``SIM``/``LIVE``) if present."""
    if isinstance(obj, dict):
        return obj.get("sourcesystem")
    return None


def raw_ref_of(obj: Any) -> Optional[str]:
    """Return the ``rawref`` extension if present."""
    if isinstance(obj, dict):
        return obj.get("rawref")
    return None


__all__ = [
    "SPEC_VERSION",
    "DEFAULT_SOURCE",
    "wrap",
    "unwrap",
    "is_cloudevent",
    "source_system_of",
    "raw_ref_of",
]
