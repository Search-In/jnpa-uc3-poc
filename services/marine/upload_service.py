"""Marine UPLOAD service — validate & import orchestration (UC-I vessel-call spine).

Thin over the pure marine PARSER FRAMEWORK (:mod:`services.marine.parsers`) + the
EXISTING :class:`VesselCallRepository`. Owns the validate → preview → confirm-import
workflow and upload-history reads, in the same mould as
:class:`services.berthing.BerthingUploadService`.

Multi-format (Phase 2): CSV, direct PCS XML (CALINF/BERMAN/VESPRO) and log-wrapped XML
(VESARR/VESDEP) are all accepted — ``parse_marine`` detects the envelope and routes each
message to its parser, producing ``_target``-tagged records that the repository persists
into core.vessel / core.vessel_call / core.vessel_call_event. An unknown or malformed
document becomes a typed row error (REJECTED), never a crash.

Response contract is backward compatible: every existing key on validate/import is
preserved; ``document_type`` (and, on import, ``failed``) are ADDITIVE.
"""
from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

from . import upload_parsers as P
from .parsers import DocumentTypeError, ParseResult, detect_format, parse_marine
from .repository import VesselCallRepository

log = get_logger("services.marine.upload_service")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _physical_format(filename: str, content: bytes) -> str:
    """Ledger physical_format = the ACTUAL uploaded file container.

    The parser's detect_format() returns a ROUTING format ('CSV'|'XML'|'LOG'|'XLSX'
    |'PDF'|'SHP'); the shapefile ('SHP') is delivered as a ZIP bundle (or, rarely, a
    bare .shp). The ledger stores what was uploaded — 'ZIP' for the zip, 'SHP' for a
    bare .shp — while parser detection/routing stays 'SHP' internally, unchanged. All
    other formats pass through as-is, so XML/XLSX/PDF/CSV uploads are unaffected."""
    fmt = detect_format(filename, content)
    if fmt == "SHP":
        return "ZIP" if content[:4] == b"PK\x03\x04" else "SHP"
    return fmt


def _document_type(res: ParseResult) -> Optional[str]:
    """The PCS document type of an upload: the single message type present, 'MIXED'
    when several, or None when there are no records (e.g. a rejected file)."""
    kinds = {r.get("_message") for r in res.records if r.get("_message")}
    if not kinds:
        return None
    return next(iter(kinds)) if len(kinds) == 1 else "MIXED"


def _build_preview(res: ParseResult) -> List[Dict[str, Any]]:
    """A compact, format-agnostic preview from the normalized records. Reuses the CSV
    parser's own preview when present; otherwise summarizes the PCS records."""
    if res.preview:
        return res.preview[:20]
    out: List[Dict[str, Any]] = []
    for r in res.records[:20]:
        out.append({
            "Type": r.get("_message"),
            "Target": r.get("_target"),
            "VCN": r.get("vcn") or "—",
            "IMO": r.get("imo_no") or "—",
            "Voyage": r.get("voyage_no") or r.get("via_no") or "—",
            "Event": r.get("event_type") or "—",
        })
    return out


class MarineUploadService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[VesselCallRepository] = None) -> None:
        self._repo = repository or VesselCallRepository(dsn)

    # ---------------------------------------------------------------- template
    def template(self) -> str:
        return P.template_csv()

    # ---------------------------------------------------------------- parse core
    def _parse(self, content: bytes, filename: str,
               document_type: Optional[str] = None) -> ParseResult:
        """Detect the envelope and parse to normalized records. CSV / XML / LOG all
        route through the pure parser framework; never raises for a bad document — the
        framework returns typed errors instead.

        ``document_type`` (optional) routes explicitly through the parser registry; when
        omitted, envelope detection routes exactly as before. It raises DocumentTypeError
        for a client-supplied value that is unknown or contradicts the detected envelope —
        a REQUEST fault, distinct from a bad document, and surfaced as HTTP 400 upstream."""
        return parse_marine(content, filename, document_type)

    @staticmethod
    def _summary(res: ParseResult) -> Dict[str, Any]:
        valid = len(res.records)
        return {"rows": res.row_count, "valid": valid, "invalid": res.invalid_count,
                "duplicates": res.duplicate_count, "importable": valid,
                "errors": len(res.errors), "warnings": len(res.warnings),
                "rejected": res.rejected, "valid_bool": (not res.rejected and valid > 0)}

    # ---------------------------------------------------------------- validate (dry-run)
    async def validate(self, content: bytes, filename: str,
                       uploaded_by: str,
                       document_type: Optional[str] = None) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res = self._parse(content, filename, document_type)
        except DocumentTypeError:
            # A client-supplied document_type fault, NOT a bad file: re-raise so the
            # router answers 400. DocumentTypeError subclasses ValueError, so it MUST be
            # caught ahead of the read_error handler below or it would be masked as a
            # REJECTED parse result.
            raise
        except ValueError as exc:
            res = ParseResult(); res.rejected = True
            res.err(None, None, "read_error", f"could not read file: {exc}")
        summary = self._summary(res)
        status = "VALIDATED" if summary["valid_bool"] else "REJECTED"
        doc_type = _document_type(res)
        log.info("marine_upload.validate", extra={"status": status, "document_type": doc_type,
                                                  "valid": summary["valid"],
                                                  "invalid": summary["invalid"],
                                                  "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"status": status, "valid": summary["valid_bool"], "summary": summary,
                "document_type": doc_type, "preview": _build_preview(res),
                "errors": res.errors[:200], "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- import (confirm)
    async def import_file(self, content: bytes, filename: str,
                          uploaded_by: str,
                          document_type: Optional[str] = None) -> Dict[str, Any]:
        t0 = perf_counter()
        sha = _sha256(content)
        physical_format = _physical_format(filename, content)
        try:
            res = self._parse(content, filename, document_type)
        except DocumentTypeError:
            # See validate(): a request fault, re-raised for the router's 400. Raised
            # BEFORE any ledger write, so a bad document_type never records an upload.
            raise
        except ValueError as exc:
            res = ParseResult(); res.rejected = True
            res.err(None, None, "read_error", f"could not read file: {exc}")
        summary = self._summary(res)
        doc_type = _document_type(res)

        # Structural rejection (unreadable / unknown document / zero usable records) →
        # FAILED ledger row so it still shows in upload history.
        if res.rejected or not res.records:
            detail = ("rejected — " + (res.errors[0]["error_detail"] if res.errors
                                       else "no importable records"))
            file_id = await self._repo.record_rejected_upload(
                physical_format=physical_format, filename=filename, file_hash=sha,
                uploaded_by=uploaded_by, detail=detail, errors=res.errors,
                document_type=doc_type)
            return {"file_id": file_id, "status": "REJECTED", "imported": 0, "updated": 0,
                    "skipped": 0, "invalid": res.invalid_count, "failed": 0,
                    "duplicate_file": False, "document_type": doc_type,
                    "summary": summary, "errors": res.errors[:200]}

        # The repository owns the ledger + errors atomically; hand it the parse-level
        # errors/counters so status and marine_import_errors are written in ONE txn.
        result = await self._repo.persist(
            res.records, filename=filename, file_hash=sha, physical_format=physical_format,
            document_type=doc_type, parse_errors=res.errors, parse_invalid=res.invalid_count,
            parse_duplicate=res.duplicate_count, file_size=len(content),
            uploaded_by=uploaded_by, source="UPLOAD")

        status = result["status"]  # SUCCESS | PARTIAL | FAILED | SKIPPED_DUPLICATE
        log.info("marine_upload.import", extra={"status": status, "document_type": doc_type,
                                                "file_id": result.get("file_id"),
                                                "inserted": result.get("inserted"),
                                                "updated": result.get("updated"),
                                                "failed": result.get("failed"),
                                                "ms": round((perf_counter() - t0) * 1000, 1)})
        # Backward-compatible envelope: existing keys preserved; failed/document_type added.
        return {"file_id": result.get("file_id"), "status": status,
                "imported": result.get("inserted", 0), "updated": result.get("updated", 0),
                "skipped": result.get("duplicate", 0), "invalid": result.get("invalid", 0),
                "failed": result.get("failed", 0),
                "duplicate_file": result.get("duplicate_file", False),
                "document_type": doc_type, "summary": summary, "warnings": res.warnings[:200]}

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
