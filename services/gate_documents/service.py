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

        # `imported` must report what THIS request persisted (fix G-1). On a
        # re-upload of an already-ingested file the repository writes nothing and
        # returns SKIPPED_DUPLICATE, but `imported_count` still carried the count
        # from the ORIGINAL import — so the response claimed rows had landed when
        # none had. DB behaviour is unchanged (it was already idempotent by row
        # hash); only the reported number is corrected. The original import's
        # count stays visible as `previously_imported` and in the ledger.
        duplicate_file = bool(result.get("duplicate", False))
        imported = 0 if status == "SKIPPED_DUPLICATE" else result["imported_count"]

        log.info("gate_doc_upload.import", extra={"doc_type": doc_type, "status": status,
                                                  "file_id": file_id,
                                                  "imported": imported,
                                                  "invalid": res.invalid_count,
                                                  "ms": round((perf_counter() - t0) * 1000, 1)})
        out = {"file_id": file_id, "status": status,
               "imported": imported,
               "skipped": result.get("duplicate_count", 0),
               "invalid": res.invalid_count,
               "duplicate_file": duplicate_file,
               "summary": summary, "warnings": res.warnings[:200]}
        if status == "SKIPPED_DUPLICATE":
            out["previously_imported"] = result["imported_count"]
        return out

    # ---------------------------------------------------------------- reads
    async def list_docs(self, doc_type: str, *, filters, limit: int, offset: int) -> Dict[str, Any]:
        rows = await self._repo.list_docs(doc_type, filters=filters, limit=limit, offset=offset)
        total = await self._repo.count_docs(doc_type, filters=filters)
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def list_source_documents(self, *, category: Optional[str] = None,
                                    container: Optional[str] = None,
                                    vehicle: Optional[str] = None,
                                    driver_licence: Optional[str] = None,
                                    terminal: Optional[str] = None,
                                    from_ts: Optional[Any] = None,
                                    to_ts: Optional[Any] = None,
                                    limit: int, offset: int) -> Dict[str, Any]:
        """Parsed source gate documents (core.gate_document) — see the repository.

        Alongside the page, report the shape of the FULL filtered set (not just
        the current page): which terminals it touches and the date span it
        covers. That is what a truck-visit view states above the timeline, and
        computing it here keeps the client from having to fetch everything.
        """
        rows, total = await self._repo.list_source_documents(
            category=category, container=container, vehicle=vehicle,
            driver_licence=driver_licence, terminal=terminal,
            from_ts=from_ts, to_ts=to_ts, limit=limit, offset=offset)
        terminals = sorted({r["terminal"] for r in rows if r.get("terminal")})
        stamps = sorted(r["doc_ts"] for r in rows if r.get("doc_ts"))
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows), "terminals": terminals,
                "terminal_count": len(terminals),
                "first_doc_ts": stamps[0] if stamps else None,
                "last_doc_ts": stamps[-1] if stamps else None}
      
    async def hourly_profile(self, doc_type: str, *, filters,
                             group_by: str = "hour") -> Dict[str, Any]:
        """Hourly (or daily) document counts for a window — the aggregate view of
        the same filter set :meth:`list_docs` pages through (audit finding G1)."""
        rows = await self._repo.hourly_profile(doc_type, filters=filters,
                                               group_by=group_by)
        buckets = [{"bucket": r["bucket"], "documents": int(r["documents"] or 0),
                    "unique_trucks": int(r.get("unique_trucks") or 0)} for r in rows]
        total = sum(b["documents"] for b in buckets)
        peak = max(buckets, key=lambda b: b["documents"]) if buckets else None
        return {"group_by": group_by, "count": len(buckets), "total_documents": total,
                "peak_bucket": peak["bucket"] if peak else None,
                "peak_documents": peak["documents"] if peak else 0,
                "mean_per_bucket": round(total / len(buckets), 2) if buckets else 0.0,
                "buckets": buckets}

    async def docs_for_container(self, container_no: str, *,
                                 source: Optional[str] = None) -> Dict[str, Any]:
        docs = await self._repo.docs_for_container(container_no, source=source)
        docs["container_no"] = container_no
        docs["total"] = sum(len(v) for k, v in docs.items() if isinstance(v, list))
        return docs

    async def docs_for_truck(self, truck_no: str, *,
                             source: Optional[str] = None) -> Dict[str, Any]:
        docs = await self._repo.docs_for_truck(truck_no, source=source)
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
