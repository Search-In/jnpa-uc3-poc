"""CFS-ECY UPLOAD service — validate & import orchestration (module 13).

Thin over the pure :mod:`upload_parsers` + the EXISTING :class:`CfsEcyRepository`.
Owns the validate → preview → confirm-import workflow and upload-history reads.
Strictly additive: it writes ONLY the new ledger tables (cfs_ecy_import_files /
cfs_ecy_import_errors) and inserts movement rows through the SAME
``repository.persist`` (the (facility_type, container_number, event_ts, mode) UNIQUE
key → idempotent, duplicate-safe, never overwrites).

Facility (CFS / ECY) is supplied by the upload's selector — it is NOT a column in the
JNPA CODECO files. A per-row Facility column, if present, overrides the selector.
"""
from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from . import upload_parsers as P
from .repository import CfsEcyRepository

log = get_logger("services.cfs_ecy.upload_service")

EVENT_UPLOAD = "cfs_ecy.uploaded"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fmt(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return "XLSX"
    if name.endswith(".xls"):
        return "XLS"
    return "CSV"


class CfsEcyUploadService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[CfsEcyRepository] = None) -> None:
        self._repo = repository or CfsEcyRepository(dsn)

    # ---------------------------------------------------------------- template
    def template(self) -> str:
        return P.template_csv()

    # ---------------------------------------------------------------- parse core
    def _parse(self, facility: str, content: bytes, filename: str) -> "P.ParseResult":
        header, rows = P.read_rows_from_bytes(content, filename)
        return P.parse(header, rows, facility=facility, source_file=filename)

    @staticmethod
    def _summary(res: "P.ParseResult") -> Dict[str, Any]:
        valid = len(res.records)
        return {"rows": res.row_count, "valid": valid, "invalid": res.invalid_count,
                "duplicates": res.duplicate_count, "importable": valid,
                "errors": len(res.errors), "warnings": len(res.warnings),
                "rejected": res.rejected,
                "valid_bool": (not res.rejected and valid > 0)}

    # ---------------------------------------------------------------- validate (dry-run)
    async def validate(self, facility: str, content: bytes, filename: str,
                       uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res = self._parse(facility, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        status = "VALIDATED" if summary["valid_bool"] else "REJECTED"
        log.info("cfs_ecy_upload.validate", extra={"facility": facility, "status": status,
                                                   "valid": summary["valid"],
                                                   "invalid": summary["invalid"],
                                                   "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"facility": facility, "status": status, "valid": summary["valid_bool"],
                "summary": summary, "preview": res.preview,
                "errors": res.errors[:200], "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- import (confirm)
    async def import_file(self, facility: str, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        sha = _sha256(content)
        size = len(content)
        physical_format = _fmt(filename)
        try:
            res = self._parse(facility, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)

        # Structural rejection (bad template / unreadable / zero valid) → FAILED ledger row.
        if res.rejected or not res.records:
            detail = ("rejected — " + (res.errors[0]["error_detail"] if res.errors
                                       else "no importable rows"))
            file_id = await self._repo.record_rejected_upload(
                facility_type=P.facility_ok(facility), physical_format=physical_format,
                source_file=filename, source_sha256=sha, file_size=size,
                uploaded_by=uploaded_by, detail=detail, errors=res.errors)
            return {"file_id": file_id, "status": "REJECTED", "imported": 0, "skipped": 0,
                    "invalid": res.invalid_count, "duplicate_file": False,
                    "summary": summary, "errors": res.errors[:200]}

        result = await self._repo.persist(
            res.records, facility_type=P.facility_ok(facility) or facility,
            source_file=filename, source_sha256=sha, physical_format=physical_format,
            file_size=size, uploaded_by=uploaded_by, source="UPLOAD")

        file_id = result.get("file_id")
        status = result["import_status"]           # SUCCESS | SKIPPED_DUPLICATE | FAILED
        # Attach the skipped/invalid source rows as errors + flag PARTIAL (only for a
        # fresh SUCCESS — a duplicate file already carries its original outcome).
        if status == "SUCCESS" and res.invalid_count and file_id:
            await self._repo.add_row_errors(file_id, res.errors)
            await self._repo.mark_partial(file_id, error_count=res.invalid_count)
            status = "PARTIAL"

        log.info("cfs_ecy_upload.import", extra={"facility": facility, "status": status,
                                                 "file_id": file_id,
                                                 "imported": result["imported_count"],
                                                 "invalid": res.invalid_count,
                                                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"file_id": file_id, "status": status,
                "imported": result["imported_count"],
                "skipped": result.get("duplicate_count", 0) + res.duplicate_count,
                "invalid": res.invalid_count,
                "duplicate_file": result.get("duplicate", False),
                "summary": summary, "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- history
    async def list_uploads(self, filters: Dict[str, Any], *, limit: int, offset: int) -> Dict[str, Any]:
        rows = await self._repo.list_files(filters=filters, limit=limit, offset=offset)
        total = await self._repo.count_files(filters=filters)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    async def get_upload(self, file_id: int) -> Optional[Dict[str, Any]]:
        row = await self._repo.get_file(file_id)
        if row is None:
            return None
        row["errors"] = await self._repo.list_file_errors(file_id, limit=500, offset=0)
        return row
