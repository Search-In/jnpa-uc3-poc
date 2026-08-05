"""Berthing UPLOAD service — validate & import orchestration (module 7).

Thin over the pure :mod:`upload_parsers` + the EXISTING :class:`BerthingRepository`.
Owns the validate → preview → confirm-import workflow and upload-history reads, in the
same mould as :class:`services.cfs_ecy.upload_service.CfsEcyUploadService`. Writes ONLY
the berthing_* tables (idempotent upsert on the vessel-call key + file-hash dedup).

The uploaded file is CSV / XLS / XLSX of the NORMALISED berthing model. (The raw
per-terminal PDFs are ingested by scripts/import_berthing_reports.py, which shares this
repository.)
"""
from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from . import upload_parsers as P
from .repository import BerthingRepository

log = get_logger("services.berthing.upload_service")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fmt(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return "PDF"
    if name.endswith((".xlsx", ".xlsm")):
        return "XLSX"
    if name.endswith(".xls"):
        return "XLS"
    return "CSV"


class BerthingUploadService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[BerthingRepository] = None) -> None:
        self._repo = repository or BerthingRepository(dsn)

    # ---------------------------------------------------------------- template
    def template(self) -> str:
        return P.template_csv()

    # ---------------------------------------------------------------- parse core
    def _parse(self, terminal: Optional[str], content: bytes, filename: str) -> "P.ParseResult":
        # PDF is the real Berthing source format — route it through the per-terminal PDF
        # parsers (auto-detecting the terminal); CSV/XLS/XLSX use the tabular reader. Both
        # yield the SAME normalised ParseResult, so validate/preview/import are unchanged.
        if P.is_pdf(filename):
            return P.parse_pdf(content, filename, terminal=terminal)
        header, rows = P.read_rows_from_bytes(content, filename)
        return P.parse(header, rows, terminal=terminal, source_file=filename)

    @staticmethod
    def _ledger_terminal(terminal: Optional[str], res: "P.ParseResult") -> Optional[str]:
        """Terminal recorded on the import-ledger row: the selector if valid, else the
        PDF-detected terminal, else the terminal of the parsed rows (single-terminal PDF)."""
        return (P.terminal_ok(terminal) or getattr(res, "detected_terminal", None)
                or (res.records[0]["terminal"] if res.records else None))

    @staticmethod
    def _summary(res: "P.ParseResult") -> Dict[str, Any]:
        valid = len(res.records)
        return {"rows": res.row_count, "valid": valid, "invalid": res.invalid_count,
                "duplicates": res.duplicate_count, "importable": valid,
                "errors": len(res.errors), "warnings": len(res.warnings),
                "rejected": res.rejected, "valid_bool": (not res.rejected and valid > 0)}

    # ---------------------------------------------------------------- validate (dry-run)
    async def validate(self, terminal: Optional[str], content: bytes, filename: str,
                       uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res = self._parse(terminal, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        status = "VALIDATED" if summary["valid_bool"] else "REJECTED"
        log.info("berthing_upload.validate", extra={"terminal": terminal, "status": status,
                                                    "valid": summary["valid"],
                                                    "invalid": summary["invalid"],
                                                    "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"terminal": (getattr(res, "detected_terminal", None) or terminal),
                "status": status, "valid": summary["valid_bool"],
                "summary": summary, "preview": res.preview,
                "errors": res.errors[:200], "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- import (confirm)
    async def import_file(self, terminal: Optional[str], content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        sha = _sha256(content)
        physical_format = _fmt(filename)
        try:
            res = self._parse(terminal, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)

        if res.rejected or not res.records:
            detail = ("rejected — " + (res.errors[0]["error_detail"] if res.errors
                                       else "no importable rows"))
            file_id = await self._repo.record_rejected_upload(
                terminal=self._ledger_terminal(terminal, res), physical_format=physical_format,
                filename=filename, file_hash=sha, uploaded_by=uploaded_by,
                detail=detail, errors=res.errors)
            return {"file_id": file_id, "status": "REJECTED", "imported": 0, "updated": 0,
                    "skipped": 0, "invalid": res.invalid_count, "duplicate_file": False,
                    "summary": summary, "errors": res.errors[:200]}

        result = await self._repo.persist(
            res.records, terminal=self._ledger_terminal(terminal, res), filename=filename,
            file_hash=sha, physical_format=physical_format, file_size=len(content),
            uploaded_by=uploaded_by, source="UPLOAD")

        file_id = result.get("file_id")
        status = result["status"]                       # SUCCESS | SKIPPED_DUPLICATE | FAILED
        if status == "SUCCESS" and file_id:
            if res.invalid_count:
                await self._repo.add_row_errors(file_id, res.errors)
                await self._repo.mark_partial(file_id, failed_rows=res.invalid_count,
                                              duplicate_rows=res.duplicate_count)
                status = "PARTIAL"
            elif res.duplicate_count:
                await self._repo.set_duplicates(file_id, duplicate_rows=res.duplicate_count)

        log.info("berthing_upload.import", extra={"terminal": terminal, "status": status,
                                                  "file_id": file_id,
                                                  "inserted": result.get("inserted"),
                                                  "updated": result.get("updated"),
                                                  "invalid": res.invalid_count,
                                                  "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"file_id": file_id, "status": status,
                "imported": result.get("inserted", 0), "updated": result.get("updated", 0),
                "skipped": res.duplicate_count, "invalid": res.invalid_count,
                "duplicate_file": result.get("duplicate_file", False),
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
