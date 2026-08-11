"""In-process registry of the SecureVision analyses THIS gateway created.

**Persistence decision (recorded, not assumed).** SecureVision publishes no
incident-history API, and its analyses are ephemeral upstream (a vendor restart
evicts the sampled frames and the stream answers 409). Persisting normalised
incidents into RDS was therefore evaluated and deliberately NOT implemented in
this change: it needs a schema migration, a retention rule, and — for I-07, which
carries person names and face similarities — a DPDP retention decision that has
not been made. Inventing a historical store without those answers would be worse
than not having one.

What this registry IS: the small amount of context the gateway itself owns about
uploads it performed — who uploaded, when, which camera, what the vendor
answered. It exists for two concrete reasons:

  * **Wall-clock timestamps.** Incident ``timestamp`` values are *seconds into
    the clip*, not instants. Without the upload time there is no way to place a
    detection on any existing JNPA timeline.
  * **An honest workbench list.** The operator needs to see the analyses from
    this session without the UI pretending a searchable history exists.

What it is NOT: durable storage. It is per-process, bounded, and lost on
restart — exactly like the vendor state it describes. Every response carries
``persisted: false`` so no screen can imply otherwise.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

#: Hard cap on remembered analyses. A control-room session runs a handful of
#: clips; anything beyond this is stale and evicted oldest-first.
MAX_ANALYSES = 200

_lock = Lock()
_analyses: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def record(
    analysis_id: str,
    *,
    securevision_camera_code: Optional[str],
    jnpa_camera_id: Optional[str],
    filename: Optional[str],
    frames_sampled: Optional[int],
    detection_pass_count: Optional[int],
    zones_loaded: Optional[int],
    uploaded_by: Optional[str],
) -> Dict[str, Any]:
    """Remember one upload and return its record."""
    entry = {
        "analysis_id": analysis_id,
        "securevision_camera_code": securevision_camera_code,
        "jnpa_camera_id": jnpa_camera_id,
        "camera_mapped": bool(jnpa_camera_id),
        "filename": filename,
        "frames_sampled": frames_sampled,
        "detection_pass_count": detection_pass_count,
        "zones_loaded": zones_loaded,
        "uploaded_by": uploaded_by,
        "uploaded_at": _now_iso(),
        # Loud, machine-readable statement that this is session state.
        "persisted": False,
    }
    with _lock:
        _analyses[analysis_id] = entry
        _analyses.move_to_end(analysis_id)
        while len(_analyses) > MAX_ANALYSES:
            _analyses.popitem(last=False)
    return entry


def get(analysis_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        entry = _analyses.get(analysis_id)
        return dict(entry) if entry else None


def forget(analysis_id: str) -> None:
    with _lock:
        _analyses.pop(analysis_id, None)


def recent(limit: int = 50) -> List[Dict[str, Any]]:
    """Newest first. Only analyses uploaded through THIS gateway process."""
    with _lock:
        rows = list(_analyses.values())
    return [dict(r) for r in reversed(rows)][: max(0, limit)]


def reset() -> None:
    """Drop everything (tests)."""
    with _lock:
        _analyses.clear()


def uploaded_at(analysis_id: str) -> Optional[str]:
    entry = get(analysis_id)
    return entry.get("uploaded_at") if entry else None


__all__ = ["record", "get", "forget", "recent", "reset", "uploaded_at",
           "MAX_ANALYSES"]
