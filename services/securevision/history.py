"""Video Analytics history — the durable store in front of the process cache.

This is the service the router talks to. It composes two things that already
existed separately:

  * :mod:`services.securevision.repository` — ``core.video_analysis``, the
    DURABLE record (migration 0143). Source of truth.
  * :mod:`services.securevision.analyses` — the in-process registry, now a
    write-through CACHE used only when the durable store cannot be reached.

WHY THE ORDER MATTERS. The registry used to be the source of truth, so history
was scoped to one gateway process: a restart (or a second worker) showed an
empty workbench, which operators correctly reported as "the history is missing".
Reading the table first makes the history survive gateway, container and worker
restarts. The cache is kept as the degraded rung so an RDS blip does not take
the workbench down with it — and, crucially, a failed read is reported as
DEGRADED rather than as an empty archive, because "I could not look" and "there
is nothing" are different answers.

PERSONAL DATA. Only operational metadata crosses into the durable store (see the
repository module and migration 0143): no embeddings, no images, no person
identities or similarity scores. Person-centric analyser results continue to be
fetched live per analysis and are stored nowhere.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from . import analyses
from .repository import VideoAnalysisRepository

log = get_logger("services.securevision.history")

#: Default page size for the workbench list.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class VideoAnalysisHistory:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[VideoAnalysisRepository] = None) -> None:
        self._repo = repository or VideoAnalysisRepository(dsn=dsn)

    # ------------------------------------------------------------------ write
    async def record(self, analysis_id: str, **fields: Any) -> Dict[str, Any]:
        """Remember one upload: cache first (never blocks), then the table.

        Returns the entry as the API reports it, with ``persisted`` telling the
        truth about whether the durable write actually landed.
        """
        entry = analyses.record(analysis_id, **{
            k: fields.get(k) for k in (
                "securevision_camera_code", "jnpa_camera_id", "filename",
                "frames_sampled", "detection_pass_count", "zones_loaded",
                "uploaded_by")
        })
        durable = dict(entry)
        durable["status"] = fields.get("status") or "COMPLETED"
        durable["processing_ms"] = fields.get("processing_ms")
        durable["source"] = fields.get("source") or "securevision"
        stored = await self._repo.record(durable)
        entry["persisted"] = stored
        entry["status"] = durable["status"]
        entry["processing_ms"] = durable["processing_ms"]
        if not stored and self._repo.enabled:
            log.warning("video_analysis.not_persisted", analysis_id=analysis_id)
        return entry

    async def forget(self, analysis_id: str) -> None:
        """Delete: soft-delete the durable row, drop it from the cache."""
        await self._repo.soft_delete(analysis_id)
        analyses.forget(analysis_id)

    # ------------------------------------------------------------------- read
    async def recent(self, *, limit: int = DEFAULT_LIMIT, offset: int = 0,
                     jnpa_camera_id: Optional[str] = None) -> Dict[str, Any]:
        """One page of history, newest first, plus where it came from."""
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))

        result = await self._repo.recent(limit=limit, offset=offset,
                                         jnpa_camera_id=jnpa_camera_id)
        if result is not None:
            rows, total = result
            return {
                "analyses": rows,
                "count": len(rows),
                "total": total,
                "limit": limit,
                "offset": offset,
                "persisted": True,
                "degraded": False,
                "source": "database",
                "note": ("Video analysis history for this deployment. Detection "
                         "results are fetched from SecureVision per analysis."),
            }

        # Durable store unavailable (or no DSN configured) — serve the process
        # cache and SAY SO. Never present this as the complete history.
        cached = analyses.recent(limit=limit + offset)[offset:offset + limit]
        degraded = self._repo.enabled
        return {
            "analyses": cached,
            "count": len(cached),
            "total": len(analyses.recent(limit=analyses.MAX_ANALYSES)),
            "limit": limit,
            "offset": offset,
            "persisted": False,
            "degraded": degraded,
            "source": "process-cache",
            "note": ("History store unavailable — showing only the analyses this "
                     "gateway process performed."
                     if degraded else
                     "No history store is configured for this deployment; showing "
                     "the analyses this gateway process performed."),
        }


__all__ = ["VideoAnalysisHistory", "DEFAULT_LIMIT", "MAX_LIMIT"]
