"""COARRI/COPRAR bytes-ingest seam — the consumer the jnpa_sync router calls.

``import_file(content, filename, uploaded_by)`` mirrors the rail consumers'
contract: parse → persist → outcome envelope, never raising on bad payloads
(structural failures become REJECTED ledger rows so they show in history and
stay replayable).
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from .parsers import (EdiVesselParseError, detect_doc_type,
                      direction_from_filename, parse_document)
from .repository import EdiVesselRepository

log = get_logger("services.edi_vessel.service")


class EdiVesselService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[EdiVesselRepository] = None) -> None:
        self._repo = repository or EdiVesselRepository(dsn)

    async def import_file(self, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        sha = hashlib.sha256(content).hexdigest()
        size = len(content)
        xml_text = content.decode("utf-8", errors="replace")
        try:
            doc_type, header, rows = parse_document(xml_text)
        except EdiVesselParseError as exc:
            feed = detect_doc_type(xml_text) or "COARRI"
            file_id = await self._repo.record_rejected(
                feed=feed, source_file=filename, source_sha256=sha,
                file_size=size, uploaded_by=uploaded_by,
                detail=f"UNSUPPORTED_FORMAT: {exc}")
            log.warning("edi_vessel.rejected", file=filename, error=str(exc))
            return {"file_id": file_id, "import_status": "REJECTED",
                    "record_count": 0, "imported_count": 0, "error_count": 1,
                    "duplicate_count": 0, "duplicate": False}
        direction = direction_from_filename(filename)
        doc_fields = {
            "doc_type": doc_type,
            "direction": direction,
            "document_number": header.get("document_number"),
            "common_ref": header.get("common_ref"),
            "sender_id": header.get("sender_id"),
            "vcn": header.get("vcn"),
            "terminal_code": header.get("terminal_code"),
            "agent_code": header.get("agent_code"),
            # Header-level line code (COPARN); item-level codes override it.
            "line_code": header.get("line_code"),
            "rotation_no": header.get("rotation_no"),
            "rotation_date": header.get("rotation_date"),
            "voyage": header.get("voyage"),
            "source_file": filename,
        }
        records = [{**doc_fields, **row} for row in rows]
        outcome = await self._repo.persist(
            doc_type, records, source_file=filename, source_sha256=sha,
            file_size=size, uploaded_by=uploaded_by)
        declared = header.get("declared_count")
        if (declared is not None and outcome.get("import_status") == "SUCCESS"
                and declared != len(records)):
            # Header count vs parsed rows drift — worth a log line, not a failure.
            log.info("edi_vessel.count_drift", file=filename,
                     declared=declared, parsed=len(records))
        return outcome


__all__ = ["EdiVesselService"]
