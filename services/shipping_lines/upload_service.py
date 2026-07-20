"""Shipping-line UPLOAD service — validate & import orchestration (module 4).

Thin over the pure :mod:`upload_parsers` + the EXISTING
:class:`ShippingLinesRepository`. Owns the validate → preview → confirm-import
workflow and upload-history reads. Strictly additive: it writes ONLY the reused
ledger tables (sl_import_files / sl_import_errors / sl_events) and inserts domain
rows through the SAME ``repository.persist`` (sha256 file dedup + row_sha256
ON CONFLICT DO NOTHING — idempotent, duplicate-safe, never overwrites).

Two EDO paths coexist:
  * flat template (CSV/XLS/XLSX)  -> _map_edo_row -> sl_delivery_orders
  * legacy CODECO XML in an .xlsx -> the existing parsers.parse_edo (unchanged)
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from . import upload_parsers as P
from .parsers import ParsedList, parse_edo
from .repository import ShippingLinesRepository

log = get_logger("services.shipping_lines.upload_service")

EVENT_UPLOAD = {"IAL": "shipping_line.ial_uploaded", "EAL": "shipping_line.eal_uploaded",
                "EDO": "shipping_line.edo_uploaded"}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fmt(filename: str, xml: bool) -> str:
    if xml:
        return "CODECO_XML"
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return "XLSX"
    if name.endswith(".xls"):
        return "XLS"
    return "CSV"


class ShippingLinesUploadService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[ShippingLinesRepository] = None) -> None:
        self._repo = repository or ShippingLinesRepository(dsn)

    # ---------------------------------------------------------------- template
    def template(self, list_type: str) -> str:
        return P.template_csv(list_type)

    # ---------------------------------------------------------------- parse core
    def _parse(self, list_type: str, content: bytes, filename: str):
        """Return (ParseResult-like dict, is_xml). Raises ValueError on unreadable file."""
        header, rows = P.read_rows_from_bytes(content, filename)
        if list_type == "EDO" and P.is_codeco_xml_upload(header):
            return self._parse_xml_edo(content, filename), True
        return P.parse(list_type, header, rows), False

    def _parse_xml_edo(self, content: bytes, filename: str) -> "P.ParseResult":
        """Route a legacy CODECO-XML-in-xlsx upload through the existing parser."""
        res = P.ParseResult()
        res.target = "delivery"
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
            tf.write(content)
            path = tf.name
        try:
            parsed = parse_edo(path, terminal=P.derive_terminal(filename))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        res.records = list(parsed.delivery_orders)
        res.row_count = parsed.record_count
        res.preview = [{"Container": o.get("container_no"), "GatePass": o.get("gate_pass_no"),
                        "Vehicle": o.get("vehicle_no"), "Agent": o.get("shipping_agent_code"),
                        "ISO": o.get("iso_code"), "Status": o.get("equipment_status")}
                       for o in res.records[:20]]
        return res

    @staticmethod
    def _summary(res: "P.ParseResult") -> Dict[str, Any]:
        valid = len(res.records)
        return {"rows": res.row_count, "valid": valid, "invalid": res.invalid_count,
                "duplicates": res.duplicate_count, "importable": valid,
                "errors": len(res.errors), "warnings": len(res.warnings),
                "rejected": res.rejected,
                "valid_bool": (not res.rejected and valid > 0)}

    # ---------------------------------------------------------------- validate (dry-run)
    async def validate(self, list_type: str, content: bytes, filename: str,
                       uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res, _xml = self._parse(list_type, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        status = "VALIDATED" if summary["valid_bool"] else "REJECTED"
        log.info("sl_upload.validate", list_type=list_type, status=status,
                 valid=summary["valid"], invalid=summary["invalid"],
                 ms=round((perf_counter() - t0) * 1000, 1))
        return {"list_type": list_type, "status": status, "valid": summary["valid_bool"],
                "summary": summary, "preview": res.preview,
                "errors": res.errors[:200], "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- import (confirm)
    async def import_file(self, list_type: str, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        sha = _sha256(content)
        size = len(content)
        try:
            res, is_xml = self._parse(list_type, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True; is_xml = False
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        physical_format = _fmt(filename, is_xml)
        terminal = P.derive_terminal(filename)

        # Structural rejection (bad template / unreadable / zero valid) → FAILED ledger row.
        if res.rejected or not res.records:
            detail = ("rejected — " + (res.errors[0]["error_detail"] if res.errors
                                       else "no importable rows"))
            file_id = await self._repo.record_rejected_upload(
                list_type=list_type, terminal=terminal, physical_format=physical_format,
                source_file=filename, source_sha256=sha, file_size=size,
                uploaded_by=uploaded_by, detail=detail, errors=res.errors)
            return {"file_id": file_id, "status": "REJECTED", "imported": 0, "skipped": 0,
                    "invalid": res.invalid_count, "summary": summary,
                    "errors": res.errors[:200]}

        # Build a ParsedList of ONLY the valid rows and hand it to the shared persist().
        line_code = next((r.get("shipping_line_code") for r in res.records
                          if r.get("shipping_line_code")), None) if res.target == "advance" else None
        parsed = ParsedList(
            header={"list_type": list_type, "terminal": terminal, "vessel_visit": None,
                    "voyage": None, "line_code": line_code, "direction": None},
            containers=(res.records if res.target == "advance" else []),
            delivery_orders=(res.records if res.target == "delivery" else []),
            record_count=len(res.records))

        result = await self._repo.persist(
            parsed, source_file=filename, source_sha256=sha, physical_format=physical_format,
            file_size=size, uploaded_by=uploaded_by, source="UPLOAD")

        file_id = result.get("file_id")
        status = result["import_status"]           # SUCCESS | SKIPPED_DUPLICATE | FAILED
        # Attach the skipped/invalid source rows as errors + flag PARTIAL (only for a
        # fresh SUCCESS — a duplicate file already carries its original outcome).
        if status == "SUCCESS" and res.invalid_count and file_id:
            await self._repo.add_row_errors(file_id, res.errors)
            await self._repo.mark_partial(file_id, error_count=res.invalid_count)
            status = "PARTIAL"
        if status in ("SUCCESS", "PARTIAL") and file_id:
            try:
                await self._repo.record_event(
                    EVENT_UPLOAD.get(list_type, "shipping_line.uploaded"),
                    module=list_type, reference=filename,
                    payload={"file_id": file_id, "uploaded_by": uploaded_by,
                             "imported": result["imported_count"], "invalid": res.invalid_count})
            except Exception as exc:  # noqa: BLE001 — event write must not fail an import
                log.warning("sl_upload.event_failed", error=str(exc))

        log.info("sl_upload.import", list_type=list_type, status=status, file_id=file_id,
                 imported=result["imported_count"], invalid=res.invalid_count,
                 ms=round((perf_counter() - t0) * 1000, 1))
        return {"file_id": file_id, "status": status,
                "imported": result["imported_count"],
                "skipped": summary["duplicates"] + (result["record_count"] - result["imported_count"]
                                                    if not result.get("duplicate") else 0),
                "invalid": res.invalid_count, "duplicate_file": result.get("duplicate", False),
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
        row["events"] = await self._repo.list_events(reference=row.get("source_file"), limit=50, offset=0)
        return row
