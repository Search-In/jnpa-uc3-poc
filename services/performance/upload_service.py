"""Performance upload service — validate & import orchestration (Module 12 sub-module).

Thin over UploadRepository + the pure upload_parsers. Owns the validate/import
workflow, duplicate detection, and upload-history recording. Read+write, but
strictly additive: it only writes the new upload lifecycle tables and inserts into
the existing jnpa.perf_* tables via idempotent ON CONFLICT. The repository is
dependency-injected so tests can pass a fake.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from . import upload_parsers as P
from .upload_repository import UploadRepository

log = get_logger("services.performance.upload_service")

REPORT_TYPES = ("daily_status", "monthly_teu", "ldb_report")


class UploadService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[UploadRepository] = None) -> None:
        self._repo = repository or UploadRepository(dsn=dsn)

    def template(self, report_type: str) -> str:
        return P.template_csv(report_type)

    async def _parse(self, report_type: str, content: bytes, filename: str) -> P.ParseResult:
        header, rows = P.read_rows(content, filename)
        res = P.parse(report_type, header, rows)
        # duplicate-report detection (warning, non-blocking)
        if res.report_keys and not res.rejected:
            existing = await self._repo.existing_report_keys(report_type, list(res.report_keys))
            for k in sorted(existing):
                res.warn(None, None, "duplicate_report",
                         f"data for {k} already exists — existing rows will be skipped on import")
        return res

    @staticmethod
    def _summary(res: P.ParseResult) -> Dict[str, Any]:
        importable = sum(len(v) for v in res.records.values())
        return {"rows": res.row_count, "importable": importable,
                "errors": len(res.errors), "warnings": len(res.warnings),
                "rejected": res.rejected, "valid": (not res.rejected and len(res.errors) == 0)}

    # ------------------------------------------------------------ validate
    async def validate(self, report_type: str, content: bytes, filename: str,
                       uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res = await self._parse(report_type, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        status = "VALIDATED" if summary["valid"] else "REJECTED"
        upload_id = await self._repo.create_upload(
            report_type=report_type, filename=filename, size=len(content),
            uploaded_by=uploaded_by, status=status, row_count=res.row_count,
            error_count=len(res.errors), notes="validation only (no import)")
        await self._repo.add_errors(upload_id, res.errors)
        await self._repo.add_log(upload_id, "VALIDATE",
                                 "ERROR" if not summary["valid"] else "INFO",
                                 f"validated {res.row_count} rows: {summary['errors']} errors, "
                                 f"{summary['warnings']} warnings")
        log.info("upload.validate", extra={"report_type": report_type, "status": status,
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"upload_id": upload_id, "report_type": report_type, "status": status,
                "valid": summary["valid"], "summary": summary, "preview": res.preview,
                "errors": res.errors[:200], "warnings": res.warnings[:200]}

    # ------------------------------------------------------------ import
    async def import_file(self, report_type: str, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res = await self._parse(report_type, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        upload_id = await self._repo.create_upload(
            report_type=report_type, filename=filename, size=len(content),
            uploaded_by=uploaded_by, status="VALIDATED", row_count=res.row_count,
            error_count=len(res.errors), notes="import requested")
        await self._repo.add_errors(upload_id, res.errors)

        if not summary["valid"]:
            await self._repo.finalize_upload(upload_id, status="REJECTED", inserted=0, skipped=0,
                                             notes="rejected — validation errors, nothing imported")
            await self._repo.add_log(upload_id, "IMPORT", "ERROR",
                                     "import refused — file failed validation")
            return {"upload_id": upload_id, "status": "REJECTED", "inserted": 0, "skipped": 0,
                    "summary": summary, "errors": res.errors[:200]}
        try:
            inserted, skipped, per_table = await self._repo.import_records(res.records)
        except Exception as exc:  # noqa: BLE001 — any DB error rolls back the whole tx
            await self._repo.finalize_upload(upload_id, status="FAILED", inserted=0, skipped=0,
                                             notes=f"import failed and rolled back: {exc}")
            await self._repo.add_log(upload_id, "IMPORT", "ERROR", f"import failed, rolled back: {exc}")
            log.warning("upload.import_failed", extra={"upload_id": upload_id, "error": str(exc)})
            return {"upload_id": upload_id, "status": "FAILED", "inserted": 0, "skipped": 0,
                    "error": str(exc)}
        for tbl, n, ins in per_table:
            await self._repo.add_log(upload_id, "IMPORT", "INFO",
                                     f"{tbl}: {ins} inserted / {n} rows", tbl, ins)
        await self._repo.finalize_upload(upload_id, status="IMPORTED", inserted=inserted,
                                         skipped=skipped, notes="import complete")
        log.info("upload.import", extra={"upload_id": upload_id, "inserted": inserted,
                 "skipped": skipped, "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"upload_id": upload_id, "status": "IMPORTED", "inserted": inserted,
                "skipped": skipped, "summary": summary, "per_table": per_table,
                "warnings": res.warnings[:200]}

    # ------------------------------------------------------------ history
    async def list_uploads(self, filters, *, limit, offset) -> Dict[str, Any]:
        rows, total = await self._repo.list_uploads(filters, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    async def get_upload(self, upload_id: str):
        return await self._repo.get_upload(upload_id)
