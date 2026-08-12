"""Durable Video Analytics history — raw-SQL repository over ``core.video_analysis``.

Companion to :mod:`services.securevision.analyses`, which is now a write-through
CACHE in front of this table rather than the source of truth. The registry alone
could not answer "show me the history": it lived in one gateway process, so a
restart erased it and a second worker never saw it.

WHAT IS STORED — operational metadata only (see migration 0143): the analysis id,
when it was uploaded and by which account, the camera it is attributed to, the
clip filename, the vendor's frame/zone counters, the outcome and how long it
took. NO face embeddings, NO images, NO person identities or similarity scores.
The I-07 person analyser's payload is fetched live per analysis and persisted
nowhere; that is a deliberate boundary, not an oversight, and the schema is
shaped so crossing it would require a migration.

Every method is BEST-EFFORT: video analysis must keep working when RDS is
briefly unavailable, so failures are logged and the caller falls back to the
in-process cache. A write that fails never fails the upload.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.securevision.repository")

#: Columns returned to callers — the API shape of one history row.
_COLUMNS = (
    "analysis_id, securevision_camera_code, jnpa_camera_id, camera_mapped, "
    "filename, frames_sampled, detection_pass_count, zones_loaded, status, "
    "processing_ms, source, uploaded_by, uploaded_at, deleted_at"
)


def _row(record: Any) -> Dict[str, Any]:
    d = dict(record)
    for key in ("uploaded_at", "deleted_at"):
        value = d.get(key)
        if value is not None and not isinstance(value, str):
            d[key] = value.isoformat()
    # The row IS the durable record — say so, replacing the old `persisted:false`
    # marker the in-memory registry stamped on every entry.
    d["persisted"] = True
    return d


class VideoAnalysisRepository:
    """Reads/writes ``core.video_analysis``. No-ops cleanly without a DSN."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    @property
    def enabled(self) -> bool:
        return bool(self._dsn)

    async def record(self, entry: Dict[str, Any]) -> bool:
        """Persist one analysis. Re-uploading the same id refreshes its row."""
        if not self._dsn or not entry.get("analysis_id"):
            return False
        sql = """
            INSERT INTO core.video_analysis
                (analysis_id, securevision_camera_code, jnpa_camera_id,
                 camera_mapped, filename, frames_sampled, detection_pass_count,
                 zones_loaded, status, processing_ms, source, uploaded_by,
                 uploaded_at)
            VALUES
                (:analysis_id, :sv_code, :jnpa_camera_id, :camera_mapped,
                 :filename, :frames_sampled, :detection_pass_count,
                 :zones_loaded, :status, :processing_ms, :source, :uploaded_by,
                 COALESCE(CAST(:uploaded_at AS timestamptz), now()))
            ON CONFLICT (analysis_id) DO UPDATE SET
                securevision_camera_code = EXCLUDED.securevision_camera_code,
                jnpa_camera_id           = EXCLUDED.jnpa_camera_id,
                camera_mapped            = EXCLUDED.camera_mapped,
                filename                 = EXCLUDED.filename,
                frames_sampled           = EXCLUDED.frames_sampled,
                detection_pass_count     = EXCLUDED.detection_pass_count,
                zones_loaded             = EXCLUDED.zones_loaded,
                status                   = EXCLUDED.status,
                processing_ms            = EXCLUDED.processing_ms,
                uploaded_by              = EXCLUDED.uploaded_by,
                uploaded_at              = EXCLUDED.uploaded_at,
                deleted_at               = NULL
        """
        params = {
            "analysis_id": entry.get("analysis_id"),
            "sv_code": entry.get("securevision_camera_code"),
            "jnpa_camera_id": entry.get("jnpa_camera_id"),
            "camera_mapped": bool(entry.get("camera_mapped")),
            "filename": entry.get("filename"),
            "frames_sampled": entry.get("frames_sampled"),
            "detection_pass_count": entry.get("detection_pass_count"),
            "zones_loaded": entry.get("zones_loaded"),
            "status": entry.get("status") or "COMPLETED",
            "processing_ms": entry.get("processing_ms"),
            "source": entry.get("source") or "securevision",
            "uploaded_by": entry.get("uploaded_by"),
            "uploaded_at": entry.get("uploaded_at"),
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                await conn.execute(text(sql), params)
            return True
        except Exception as exc:  # noqa: BLE001 — never fail an upload on RDS
            log.warning("video_analysis.record_failed",
                        analysis_id=entry.get("analysis_id"), error=str(exc))
            return False

    async def soft_delete(self, analysis_id: str) -> bool:
        """Mark an analysis deleted, keeping the audit trail that it existed."""
        if not self._dsn or not analysis_id:
            return False
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(
                    text("UPDATE core.video_analysis "
                         "SET deleted_at = now(), status = 'DELETED' "
                         "WHERE analysis_id = :a AND deleted_at IS NULL"),
                    {"a": analysis_id})
            return bool(res.rowcount)
        except Exception as exc:  # noqa: BLE001
            log.warning("video_analysis.delete_failed", analysis_id=analysis_id,
                        error=str(exc))
            return False

    async def recent(self, *, limit: int = 50, offset: int = 0,
                     include_deleted: bool = False,
                     jnpa_camera_id: Optional[str] = None,
                     ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        """``(rows, total)`` newest-first, or None when the read failed.

        None is NOT an empty history: it means the durable store could not be
        consulted, and the caller falls back to the in-process cache instead of
        reporting an empty archive it never verified.
        """
        if not self._dsn:
            return None
        conds: List[str] = []
        params: Dict[str, Any] = {"limit": max(1, min(int(limit), 500)),
                                  "offset": max(0, int(offset))}
        if not include_deleted:
            conds.append("deleted_at IS NULL")
        if jnpa_camera_id:
            conds.append("jnpa_camera_id = :cam")
            params["cam"] = jnpa_camera_id
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        try:
            async with get_engine(self._dsn).connect() as conn:
                total = (await conn.execute(
                    text(f"SELECT count(*) FROM core.video_analysis {where}"),
                    params)).scalar()
                rows = (await conn.execute(
                    text(f"SELECT {_COLUMNS} FROM core.video_analysis {where} "
                         "ORDER BY uploaded_at DESC, analysis_id DESC "
                         "LIMIT :limit OFFSET :offset"),
                    params)).mappings().all()
            return [_row(r) for r in rows], int(total or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("video_analysis.list_failed", error=str(exc))
            return None

    async def get(self, analysis_id: str) -> Optional[Dict[str, Any]]:
        if not self._dsn or not analysis_id:
            return None
        try:
            async with get_engine(self._dsn).connect() as conn:
                row = (await conn.execute(
                    text(f"SELECT {_COLUMNS} FROM core.video_analysis "
                         "WHERE analysis_id = :a"), {"a": analysis_id})).mappings().first()
            return _row(row) if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("video_analysis.get_failed", analysis_id=analysis_id,
                        error=str(exc))
            return None


__all__ = ["VideoAnalysisRepository"]
