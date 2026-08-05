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


def _fmt(content: bytes, filename: str) -> str:
    """Physical format for the audit ledger. Never raises — an unreadable file still
    has to be recorded in the upload history."""
    try:
        return P.sniff_format(content, filename)
    except ValueError:
        return "UNKNOWN"


class UploadService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[UploadRepository] = None) -> None:
        self._repo = repository or UploadRepository(dsn=dsn)

    def template(self, report_type: str) -> str:
        return P.template_csv(report_type)

    @staticmethod
    def _parse_only(report_type: str, content: bytes, filename: str) -> P.ParseResult:
        """Pure parse (no DB). Route by CONTENT: an official JNPA PDF goes to the
        validated PDF extraction engine, the normalised template to the CSV/XLSX
        reader. Both yield the same ParseResult, so everything downstream is unchanged.

        Any parser failure — corrupt PDF, scanned PDF, wrong report, malformed CSV —
        becomes a REJECTED ParseResult, never an exception the API turns into a 500.
        """
        try:
            if P.sniff_format(content, filename) == "PDF":
                return P.parse_pdf(report_type, content, filename)
            header, rows = P.read_rows(content, filename)
            return P.parse(report_type, header, rows)
        except ValueError as exc:
            code = str(exc).split(":", 1)[0].strip() or "unreadable_file"
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, code, f"could not read file: {exc}")
            return res
        except Exception as exc:  # noqa: BLE001 — an unexpected parser fault is still
            # the client's file being unreadable, not a server outage: reject cleanly
            # and record the reason instead of returning 500.
            log.warning("upload.parse_failed", extra={"report_type": report_type,
                        "filename": filename, "error": f"{type(exc).__name__}: {exc}"})
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, "parse_failed",
                    f"the file could not be parsed as a {report_type} report "
                    f"({type(exc).__name__}: {exc})")
            return res

    async def _parse(self, report_type: str, content: bytes, filename: str) -> P.ParseResult:
        res = self._parse_only(report_type, content, filename)
        # duplicate-report detection (warning, non-blocking — re-uploading a corrected
        # report is supported and refreshes the existing rows in place)
        if res.report_keys and not res.rejected:
            existing = await self._repo.existing_report_keys(report_type, list(res.report_keys))
            for k in sorted(existing):
                res.warn(None, None, "duplicate_report",
                         f"data for {k} already exists — importing will REPLACE those "
                         f"values with the ones in this file")
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
            error_count=len(res.errors), notes="validation only (no import)",
            file_format=_fmt(content, filename))
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
            error_count=len(res.errors), notes="import requested",
            file_format=_fmt(content, filename))
        await self._repo.add_errors(upload_id, res.errors)

        if not summary["valid"]:
            await self._repo.finalize_upload(upload_id, status="REJECTED", inserted=0, skipped=0,
                                             notes="rejected — validation errors, nothing imported")
            await self._repo.add_log(upload_id, "IMPORT", "ERROR",
                                     "import refused — file failed validation")
            return {"upload_id": upload_id, "status": "REJECTED", "inserted": 0, "skipped": 0,
                    "summary": summary, "errors": res.errors[:200]}
        try:
            # source_file + upload_id are stamped on every imported row for traceability
            inserted, updated, per_table = await self._repo.import_records(
                res.records, upload_id=upload_id, source_file=filename)
        except Exception as exc:  # noqa: BLE001 — any DB error rolls back the whole tx
            await self._repo.finalize_upload(upload_id, status="FAILED", inserted=0, skipped=0,
                                             notes=f"import failed and rolled back: {exc}")
            await self._repo.add_log(upload_id, "IMPORT", "ERROR", f"import failed, rolled back: {exc}")
            log.warning("upload.import_failed", extra={"upload_id": upload_id, "error": str(exc)})
            return {"upload_id": upload_id, "status": "FAILED", "inserted": 0, "skipped": 0,
                    "updated": 0, "error": str(exc)}
        for tbl, n, ins, upd in per_table:
            await self._repo.add_log(upload_id, "IMPORT", "INFO",
                                     f"{tbl}: {ins} inserted / {upd} updated / {n} rows",
                                     tbl, ins + upd)
        await self._repo.finalize_upload(upload_id, status="IMPORTED", inserted=inserted,
                                         skipped=0, updated=updated,
                                         notes=(f"import complete — {inserted} new, "
                                                f"{updated} refreshed from this file"))
        log.info("upload.import", extra={"upload_id": upload_id, "inserted": inserted,
                 "updated": updated, "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"upload_id": upload_id, "status": "IMPORTED", "inserted": inserted,
                "updated": updated, "skipped": 0, "summary": summary, "per_table": per_table,
                "warnings": res.warnings[:200]}

    # ------------------------------------------------------------ history
    async def list_uploads(self, filters, *, limit, offset) -> Dict[str, Any]:
        rows, total = await self._repo.list_uploads(filters, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    async def get_upload(self, upload_id: str):
        return await self._repo.get_upload(upload_id)
