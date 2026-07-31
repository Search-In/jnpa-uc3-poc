"""Gate Document service — validate/import orchestration + reads (UC-III).

Thin over the pure :mod:`upload_parsers` and :class:`GateDocumentRepository`,
mirroring :class:`services.cfs_ecy.upload_service.CfsEcyUploadService`: it owns
the validate → preview → confirm-import workflow, the upload history reads, and
the document-side reads (per container, per truck, TAT).
"""
from __future__ import annotations

import hashlib
from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from . import upload_parsers as P
from .repository import GateDocumentRepository

log = get_logger("services.gate_documents.service")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fmt(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return "XLSX"
    if name.endswith(".xls"):
        return "XLS"
    return "CSV"


class GateDocumentService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[GateDocumentRepository] = None) -> None:
        self._repo = repository or GateDocumentRepository(dsn)

    # ---------------------------------------------------------------- template
    def template(self, doc_type: str) -> str:
        return P.template_csv(doc_type)

    # ---------------------------------------------------------------- parse core
    def _parse(self, doc_type: str, content: bytes, filename: str) -> "P.ParseResult":
        header, rows = P.read_rows_from_bytes(content, filename)
        return P.parse(header, rows, doc_type=doc_type, source_file=filename)

    @staticmethod
    def _summary(res: "P.ParseResult") -> Dict[str, Any]:
        valid = len(res.records)
        return {"rows": res.row_count, "valid": valid, "invalid": res.invalid_count,
                "duplicates": res.duplicate_count, "importable": valid,
                "errors": len(res.errors), "warnings": len(res.warnings),
                "rejected": res.rejected,
                "valid_bool": (not res.rejected and valid > 0)}

    # ---------------------------------------------------------------- validate
    async def validate(self, doc_type: str, content: bytes, filename: str,
                       uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            res = self._parse(doc_type, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)
        status = "VALIDATED" if summary["valid_bool"] else "REJECTED"
        log.info("gate_doc_upload.validate", extra={"doc_type": doc_type, "status": status,
                                                    "valid": summary["valid"],
                                                    "invalid": summary["invalid"],
                                                    "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"doc_type": doc_type, "status": status, "valid": summary["valid_bool"],
                "summary": summary, "preview": res.preview,
                "errors": res.errors[:200], "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- import
    async def import_file(self, doc_type: str, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        t0 = perf_counter()
        sha = _sha256(content)
        size = len(content)
        physical_format = _fmt(filename)
        try:
            res = self._parse(doc_type, content, filename)
        except ValueError as exc:
            res = P.ParseResult(); res.rejected = True
            res.err(None, None, str(exc), f"could not read file: {exc}")
        summary = self._summary(res)

        if res.rejected or not res.records:
            detail = ("rejected — " + (res.errors[0]["error_detail"] if res.errors
                                       else "no importable rows"))
            file_id = await self._repo.record_rejected_upload(
                doc_type=doc_type, physical_format=physical_format, source_file=filename,
                source_sha256=sha, file_size=size, uploaded_by=uploaded_by,
                detail=detail, errors=res.errors)
            return {"file_id": file_id, "status": "REJECTED", "imported": 0, "skipped": 0,
                    "invalid": res.invalid_count, "duplicate_file": False,
                    "summary": summary, "errors": res.errors[:200]}

        result = await self._repo.persist(
            res.records, doc_type=doc_type, source_file=filename, source_sha256=sha,
            physical_format=physical_format, file_size=size, uploaded_by=uploaded_by,
            duplicate_count=res.duplicate_count, source="UPLOAD")

        file_id = result.get("file_id")
        status = result["import_status"]
        # Persist DB-level per-row failures too. Without this the ledger showed
        # error_count>0 with NO error rows, so a failed import gave the operator
        # a count and no reason.
        if file_id and result.get("row_errors"):
            await self._repo.add_row_errors(file_id, result["row_errors"])
        if status == "SUCCESS" and res.invalid_count and file_id:
            await self._repo.add_row_errors(file_id, res.errors)
            await self._repo.mark_partial(file_id, error_count=res.invalid_count)
            status = "PARTIAL"

        log.info("gate_doc_upload.import", extra={"doc_type": doc_type, "status": status,
                                                  "file_id": file_id,
                                                  "imported": result["imported_count"],
                                                  "invalid": res.invalid_count,
                                                  "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"file_id": file_id, "status": status,
                "imported": result["imported_count"],
                "skipped": result.get("duplicate_count", 0),
                "invalid": res.invalid_count,
                "duplicate_file": result.get("duplicate", False),
                "summary": summary, "warnings": res.warnings[:200]}

    # ---------------------------------------------------------------- reads
    async def list_docs(self, doc_type: str, *, filters, limit: int, offset: int) -> Dict[str, Any]:
        rows = await self._repo.list_docs(doc_type, filters=filters, limit=limit, offset=offset)
        total = await self._repo.count_docs(doc_type, filters=filters)
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def docs_for_container(self, container_no: str) -> Dict[str, Any]:
        docs = await self._repo.docs_for_container(container_no)
        docs["container_no"] = container_no
        docs["total"] = sum(len(v) for k, v in docs.items() if isinstance(v, list))
        return docs

    async def docs_for_truck(self, truck_no: str) -> Dict[str, Any]:
        docs = await self._repo.docs_for_truck(truck_no)
        docs["truck_no"] = truck_no
        docs["total"] = sum(len(v) for k, v in docs.items() if isinstance(v, list))
        # Terminals touched + measured TATs — the client's hero-truck framing.
        terminals = {d.get("terminal") for d in docs["eir"] + docs["pin"] + docs["form13"]
                     if d.get("terminal")}
        docs["terminals"] = sorted(terminals)
        docs["tat_samples"] = [
            {"eir_no": d.get("eir_no"), "terminal": d.get("terminal"),
             "container_number": d.get("container_number"),
             "truck_in_time": d.get("truck_in_time"), "truck_out_time": d.get("truck_out_time"),
             "tat_minutes": d.get("tat_minutes")}
            for d in docs["eir"] if d.get("tat_minutes") is not None]
        return docs

    async def tat_summary(self, *, terminal: Optional[str] = None) -> Dict[str, Any]:
        return await self._repo.tat_summary(terminal=terminal)

    async def summary(self) -> Dict[str, Any]:
        return await self._repo.summary()

    # ---------------------------------------------------------------- history
    async def list_uploads(self, filters, *, limit: int, offset: int) -> Dict[str, Any]:
        rows = await self._repo.list_files(filters=filters, limit=limit, offset=offset)
        total = await self._repo.count_files(filters=filters)
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def get_upload(self, file_id: int) -> Optional[Dict[str, Any]]:
        row = await self._repo.get_file(file_id)
        if row is None:
            return None
        row["errors"] = await self._repo.list_file_errors(file_id, limit=500, offset=0)
        return row
