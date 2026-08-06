"""FOIS Train Intimation consumer (group ``rail-fois``).

Thin over the pure :mod:`services.rail.parsers.fois_csv` + the
:class:`services.rail.repository.RailRepository`. Owns the standard upload seam
``import_file(content, filename, uploaded_by)`` with sha256 ledger dedup and
SUCCESS / PARTIAL / SKIPPED_DUPLICATE / REJECTED / FAILED outcomes — the same
mould as :class:`services.cfs_ecy.upload_service.CfsEcyUploadService`, so the
JNPA sync router feeds it byte-for-byte like every other consumer.
"""
from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from .parsers import ParseResult
from .parsers import fois_csv
from .repository import RailRepository

log = get_logger("services.rail.fois_service")

_FEED = "FOIS"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fmt(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return "XLSX"
    if name.endswith(".xls"):
        return "XLS"
    if name.endswith(".pdf"):
        return "PDF"
    if name.endswith(".txt"):
        return "TXT"
    if name.endswith(".csv"):
        return "CSV"
    return "OTHER"


class RailFoisService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[RailRepository] = None) -> None:
        self._repo = repository or RailRepository(dsn)

    async def import_file(self, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        sha = _sha256(content)
        size = len(content)
        physical_format = _fmt(filename)
        try:
            res = fois_csv.parse(content, filename)
        except Exception as exc:  # noqa: BLE001 — surface as REJECTED, never crash
            res = ParseResult(feed=_FEED)
            res.rejected = True
            res.reason = "parse_error"
            res.err(None, None, "parse_error", f"could not read file: {exc}")

        if res.unsupported or res.rejected:
            reason = res.reason or ("UNSUPPORTED_FORMAT" if res.unsupported
                                    else "rejected")
            detail = (res.errors[0]["error_detail"] if res.errors
                      else "no importable rows")
            file_id = await self._repo.record_rejected(
                feed=res.feed or _FEED, physical_format=physical_format,
                source_file=filename, source_sha256=sha, file_size=size,
                uploaded_by=uploaded_by, detail=detail, reason=reason,
                errors=res.errors)
            log.info("rail_fois.rejected", extra={"filename": filename,
                                                  "reason": reason})
            return {"status": "REJECTED", "reason": reason, "file_id": file_id,
                    "imported": 0, "skipped": 0, "invalid": res.invalid_count,
                    "feed": res.feed or _FEED}

        feed = res.feed or _FEED
        result = await self._repo.persist(
            feed, res.rows, source_file=filename, source_sha256=sha,
            physical_format=physical_format, file_size=size,
            uploaded_by=uploaded_by)
        file_id = result.get("file_id")
        status = result["import_status"]
        if status == "SUCCESS" and res.invalid_count and file_id:
            await self._repo.add_row_errors(file_id, res.errors)
            await self._repo.mark_partial(file_id, error_count=res.invalid_count)
            status = "PARTIAL"

        log.info("rail_fois.import", extra={
            "filename": filename, "status": status, "file_id": file_id,
            "imported": result["imported_count"], "invalid": res.invalid_count,
            "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"status": status, "file_id": file_id, "feed": feed,
                "imported": result["imported_count"],
                "skipped": result.get("duplicate_count", 0),
                "invalid": res.invalid_count,
                "duplicate_file": result.get("duplicate", False)}


__all__ = ["RailFoisService"]
