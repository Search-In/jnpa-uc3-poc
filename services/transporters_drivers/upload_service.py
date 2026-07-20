"""Transporters & Drivers UPLOAD service — validate & import orchestration.

Thin over the pure :mod:`upload_parsers` + the raw-SQL
:class:`TransportersDriversRepository`. Owns the validate -> preview -> confirm-import
workflow and upload-history reads. Strictly additive: it writes ONLY the new ledger
tables (td_import_files / td_import_errors) and UPSERTS master rows through
``repository.persist`` (idempotent on source_company_id / licence_no_norm). Mirrors
:mod:`services.cfs_ecy.upload_service`; the dimension is ``entity`` (TRANSPORTER /
DRIVER) instead of ``facility`` (CFS / ECY).
"""
from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from . import upload_parsers as P
from .repository import TransportersDriversRepository

log = get_logger("services.transporters_drivers.upload_service")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fmt(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return "XLSX"
    if name.endswith(".xls"):
        return "XLS"
    return "CSV"


class TransportersDriversUploadService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[TransportersDriversRepository] = None) -> None:
        self._repo = repository or TransportersDriversRepository(dsn)

    # ---------------------------------------------------------------- template
    def template(self, entity: str) -> str:
        return P.template_csv(entity)

    # ---------------------------------------------------------------- parse core
    def _parse(self, entity: str, content: bytes, filename: str) -> "P.ParseResult":
        header, rows = P.read_rows_from_bytes(content, filename)
        return P.parse(header, rows, entity=entity, source_file=filename)

    @staticmethod
    def _summary(res: "P.ParseResult") -> Dict[str, Any]:
        valid = len(res.records)
        return {"rows": res.row_count, "valid": valid, "invalid": res.invalid_count,
                "duplicates": res.duplicate_count, "importable": valid,
                "errors": len(res.errors), "warnings": len(res.warnings),
                "rejected": res.rejected,
                "valid_bool": (not res.rejected and valid > 0)}

    # ---------------------------------------------------------------- validate (dry-run)
    async def validate(self, entity: str, content: bytes, filename: str,
                       uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res = self._parse(entity, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        status = "VALIDATED" if summary["valid_bool"] else "REJECTED"
        log.info("td_upload.validate", extra={"entity": entity, "status": status,
                                              "valid": summary["valid"],
                                              "invalid": summary["invalid"],
                                              "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"entity": entity, "status": status, "valid": summary["valid_bool"],
                "summary": summary, "preview": res.preview,
                "errors": res.errors[:200], "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- import (confirm)
    async def import_file(self, entity: str, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        sha = _sha256(content)
        size = len(content)
        physical_format = _fmt(filename)
        try:
            res = self._parse(entity, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)

        # Structural rejection (bad template / unreadable / zero valid) -> FAILED ledger row.
        if res.rejected or not res.records:
            detail = ("rejected — " + (res.errors[0]["error_detail"] if res.errors
                                       else "no importable rows"))
            file_id = await self._repo.record_rejected_upload(
                entity_type=entity, physical_format=physical_format, source_file=filename,
                source_sha256=sha, file_size=size, uploaded_by=uploaded_by, detail=detail,
                errors=res.errors)
            return {"file_id": file_id, "status": "REJECTED", "imported": 0, "skipped": 0,
                    "invalid": res.invalid_count, "duplicate_file": False,
                    "summary": summary, "errors": res.errors[:200]}

        result = await self._repo.persist(
            res.records, entity_type=entity, source_file=filename, source_sha256=sha,
            physical_format=physical_format, file_size=size, uploaded_by=uploaded_by,
            source="UPLOAD")

        file_id = result.get("file_id")
        status = result["import_status"]           # SUCCESS | PARTIAL | SKIPPED_DUPLICATE | FAILED
        # Attach the invalid source rows (parser) + any DB-level row failures (persist)
        # as errors, and flag PARTIAL — only for a fresh, non-duplicate import.
        db_errors = result.get("row_errors", [])
        all_errors = list(res.errors) + list(db_errors)
        if status in ("SUCCESS", "PARTIAL") and file_id and (res.invalid_count or db_errors):
            await self._repo.add_row_errors(file_id, all_errors)
            await self._repo.mark_partial(file_id, error_count=res.invalid_count + len(db_errors))
            status = "PARTIAL"

        log.info("td_upload.import", extra={"entity": entity, "status": status,
                                            "file_id": file_id,
                                            "imported": result["imported_count"],
                                            "created": result.get("created"),
                                            "updated": result.get("updated"),
                                            "invalid": res.invalid_count,
                                            "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"file_id": file_id, "status": status,
                "imported": result["imported_count"],
                "created": result.get("created", 0), "updated": result.get("updated", 0),
                "skipped": result.get("duplicate_count", 0) + res.duplicate_count,
                "invalid": res.invalid_count + len(db_errors),
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
