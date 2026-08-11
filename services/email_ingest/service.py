"""Email processing orchestration.

This service OWNS no parsing and no master-table SQL. It extracts attachments,
asks :mod:`services.email_ingest.classifier` where they belong, and then hands
the bytes to the EXISTING upload services, which already do validation, row
dedup, upsert and ledger writes:

    MARINE    services.marine.upload_service.MarineUploadService
              .validate(content, filename, uploaded_by, document_type=...)     -> preview
              .import_file(content, filename, uploaded_by, document_type=...)   -> import
    GATE_DOC  services.gate_documents.service.GateDocumentService
              .validate(doc_type, content, filename, uploaded_by)               -> preview
              .import_file(doc_type, content, filename, uploaded_by)            -> import

Because those services are reused verbatim, duplicate protection is whatever the
domain already does (file sha256 + row hash upserts) — this layer adds no second
mechanism, only the email-level guard that a PROCESSED email is not re-imported.

Attachment bytes are never persisted: they are re-read from the mailbox by IMAP
uid for each preview/import.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

from . import classifier as C
from . import config as mail_config
from . import imap_client as IMAP
from .repository import EmailIngestRepository

log = get_logger("services.email_ingest.service")

STATUS_UNPROCESSED = "UNPROCESSED"
STATUS_PROCESSING = "PROCESSING"
STATUS_PROCESSED = "PROCESSED"
STATUS_FAILED = "FAILED"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"


def _count(payload: Dict[str, Any], *names: str) -> int:
    """First present integer among ``names`` (the two upload services differ)."""
    for n in names:
        v = payload.get(n)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    summary = payload.get("summary")
    if isinstance(summary, dict):
        for n in names:
            v = summary.get(n)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return 0


def _errors(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("errors")
    out: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for e in raw[:200]:
            if isinstance(e, dict):
                out.append(e)
            else:
                out.append({"error_code": "error", "error_detail": str(e)})
    return out


class EmailProcessingService:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._repo = EmailIngestRepository(dsn)

    # ------------------------------------------------------------------ config
    @staticmethod
    def mailbox_config():
        return mail_config.from_env()

    def health(self) -> Dict[str, Any]:
        """Credential-free posture for the page banner."""
        cfg = self.mailbox_config()
        out: Dict[str, Any] = {"mailbox": cfg.public(), "ledger": self._repo.enabled}
        if cfg.configured:
            ok, message = IMAP.check_connection(cfg)
            out["connected"], out["message"] = ok, message
        else:
            out["connected"] = False
            out["message"] = ("The mailbox is not configured. Set EMAIL_HOST, EMAIL_USER "
                              "and EMAIL_PASSWORD.")
        return out

    # -------------------------------------------------------------------- sync
    async def sync(self) -> Dict[str, Any]:
        """Poll the mailbox and upsert every subject-matching email into the ledger."""
        cfg = self.mailbox_config()
        summaries = IMAP.fetch_matching(cfg, with_content=False)   # raises MailboxError
        stored = 0
        for s in summaries:
            setattr(s, "mailbox", cfg.mailbox)
            try:
                if await self._repo.upsert_message(s) is not None:
                    stored += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not sink the poll
                log.warning("email_upsert_failed", subject=s.subject[:80], error=str(exc))
        return {"scanned": len(summaries), "stored": stored,
                "subject_prefix": cfg.subject_prefix}

    # -------------------------------------------------------------------- read
    async def list_messages(self, *, status: Optional[str] = None,
                            limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        return await self._repo.list_messages(status=status, limit=limit, offset=offset)

    async def get_message(self, email_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.get_message(email_id)

    # ----------------------------------------------------------------- process
    async def process(self, email_id: int, *, actor: str,
                      commit: bool = False, override: bool = False) -> Dict[str, Any]:
        """Classify + preview (``commit=False``) or import (``commit=True``).

        The preview path calls each domain's ``validate()`` (dry run — writes
        nothing) so the operator sees the target master table and record counts
        BEFORE confirming. The import path calls ``import_file()``.
        """
        row = await self._repo.get_message(email_id)
        if row is None:
            return {"ok": False, "status": STATUS_FAILED,
                    "message": "That email is no longer in the processing list."}

        # Duplicate guard: an already-imported email is not re-imported unless the
        # caller explicitly overrides. Preview stays available either way.
        if commit and row.get("processing_status") == STATUS_PROCESSED and not override:
            return {"ok": True, "status": STATUS_PROCESSED, "already_processed": True,
                    "message": "This email has already been imported.",
                    "target_master_table": row.get("target_master_table"),
                    "records_detected": row.get("records_detected", 0),
                    "records_imported": row.get("records_imported", 0),
                    "records_failed": row.get("records_failed", 0),
                    "attachments": []}

        cfg = self.mailbox_config()
        full = IMAP.fetch_by_uid(cfg, str(row.get("imap_uid") or ""), with_content=True)
        if full is None:
            await self._repo.mark_status(email_id, STATUS_FAILED, processed_by=actor,
                                         error_detail="Message not found in the mailbox.")
            return {"ok": False, "status": STATUS_FAILED,
                    "message": "The email could not be re-read from the mailbox. "
                               "It may have been moved or deleted."}

        if commit:
            await self._repo.mark_status(email_id, STATUS_PROCESSING, processed_by=actor)
        await self._repo.clear_errors(email_id)

        subject = full.subject or row.get("subject") or ""
        results: List[Dict[str, Any]] = []
        all_errors: List[Dict[str, Any]] = []
        detected, imported, failed = 0, 0, 0
        tables: List[str] = []
        types: List[str] = []
        needs_review = False

        if not full.attachments:
            # Body-only email: there is no body parser in UC3 today, so this is an
            # honest review rather than a silent success.
            await self._repo.record_result(
                email_id, status=STATUS_NEEDS_REVIEW, detected_type="EMAIL_BODY",
                target_master_table=None, detected=0, imported=0, failed=0,
                processed_by=actor,
                error_detail="The email has no attachments, and structured data typed "
                             "directly into the body is not yet supported.")
            return {"ok": True, "status": STATUS_NEEDS_REVIEW, "committed": False,
                    "message": "Unable to confidently map this email to an existing "
                               "master table.",
                    "detected_type": "EMAIL_BODY", "target_master_table": None,
                    "candidates": C.known_master_tables(),
                    "reason": "The email carries no attachment to classify.",
                    "records_detected": 0, "records_imported": 0, "records_failed": 0,
                    "attachments": []}

        for att in full.attachments:
            route = C.classify(att.filename, att.content or b"", subject)
            entry: Dict[str, Any] = {"filename": att.filename,
                                     "size_bytes": att.size_bytes,
                                     "content_type": att.content_type,
                                     **route.as_dict()}
            if not route.confident:
                needs_review = True
                entry["status"] = STATUS_NEEDS_REVIEW
                await self._repo.record_attachment_result(
                    email_id, att.sha256, att.filename,
                    detected_format=route.detected_format,
                    detected_document_type=route.document_type,
                    target_master_table=route.master_table,
                    process_status=STATUS_NEEDS_REVIEW,
                    error_detail=route.reason)
                all_errors.append({"record_ref": att.filename,
                                   "error_code": route.reason_code or "unclassified",
                                   "error_detail": route.reason})
                results.append(entry)
                continue

            try:
                outcome = await self._run_domain(route, att, actor, commit=commit,
                                                 override=override)
            except Exception as exc:  # noqa: BLE001 — one attachment must not sink the rest
                log.warning("email_attachment_failed", email_id=email_id,
                            filename=att.filename, error=str(exc))
                entry["status"] = STATUS_FAILED
                # Deliberately generic: the exception text can carry SQL / file
                # internals and must not reach the browser.
                entry["error"] = "This attachment could not be processed."
                await self._repo.record_attachment_result(
                    email_id, att.sha256, att.filename,
                    detected_format=route.detected_format,
                    detected_document_type=route.document_type,
                    target_master_table=route.master_table,
                    process_status=STATUS_FAILED,
                    error_detail="processing_error")
                all_errors.append({"record_ref": att.filename,
                                   "error_code": "processing_error",
                                   "error_detail": "This attachment could not be processed."})
                failed += 1
                results.append(entry)
                continue

            d = _count(outcome, "record_count", "records", "detected", "total", "parsed")
            i = _count(outcome, "imported", "imported_count", "upserted")
            f = _count(outcome, "invalid", "error_count", "rejected", "failed")
            detected += d
            imported += i if commit else 0
            failed += f
            errs = _errors(outcome)
            all_errors.extend({**e, "record_ref": e.get("record_ref") or att.filename}
                              for e in errs)
            entry.update({"status": STATUS_PROCESSED if commit else "PREVIEWED",
                          "records_detected": d, "records_imported": i if commit else 0,
                          "records_failed": f, "result": _safe_outcome(outcome)})
            if route.master_table:
                tables.append(route.master_table)
            if route.document_type:
                types.append(route.document_type)
            await self._repo.record_attachment_result(
                email_id, att.sha256, att.filename,
                detected_format=route.detected_format,
                detected_document_type=route.document_type,
                target_master_table=route.master_table,
                process_status=STATUS_PROCESSED if commit else STATUS_UNPROCESSED,
                records_detected=d, records_imported=i if commit else 0, records_failed=f)
            results.append(entry)

        await self._repo.add_errors(email_id, all_errors)

        target = tables[0] if len(set(tables)) == 1 and tables else (
            ", ".join(sorted(set(tables))) if tables else None)
        detected_type = types[0] if len(set(types)) == 1 and types else (
            ", ".join(sorted(set(types))) if types else None)

        if needs_review and not tables:
            status = STATUS_NEEDS_REVIEW
        elif failed and not imported and commit:
            status = STATUS_FAILED
        elif needs_review:
            status = STATUS_NEEDS_REVIEW
        else:
            status = STATUS_PROCESSED if commit else STATUS_UNPROCESSED

        if commit or status in (STATUS_NEEDS_REVIEW, STATUS_FAILED):
            await self._repo.record_result(
                email_id, status=status, detected_type=detected_type,
                target_master_table=target, detected=detected,
                imported=imported, failed=failed, processed_by=actor)

        return {
            "ok": True,
            "status": status,
            "committed": bool(commit),
            "message": ("Unable to confidently map this email to an existing master table."
                        if status == STATUS_NEEDS_REVIEW else
                        ("Import complete." if commit else "Preview ready — confirm to import.")),
            "detected_type": detected_type,
            "target_master_table": target,
            "candidates": C.known_master_tables() if status == STATUS_NEEDS_REVIEW else [],
            "records_detected": detected,
            "records_imported": imported,
            "records_failed": failed,
            "attachments": results,
            "errors": all_errors[:100],
        }

    async def _run_domain(self, route: "C.Route", att: Any, actor: str, *,
                          commit: bool, override: bool) -> Dict[str, Any]:
        """Delegate to the existing domain upload service. No parsing happens here."""
        if route.domain == C.DOMAIN_MARINE:
            from services.marine.upload_service import MarineUploadService
            svc = MarineUploadService(dsn=self._dsn)
            if commit:
                return await svc.import_file(att.content, att.filename, actor,
                                             document_type=route.document_type,
                                             override=override, data_origin="EMAIL")
            return await svc.validate(att.content, att.filename, actor,
                                      document_type=route.document_type)
        if route.domain == C.DOMAIN_GATE_DOC:
            from services.gate_documents.service import GateDocumentService
            svc = GateDocumentService(dsn=self._dsn)
            if commit:
                return await svc.import_file(route.document_type, att.content,
                                             att.filename, actor)
            return await svc.validate(route.document_type, att.content,
                                      att.filename, actor)
        raise ValueError(f"no importer for domain {route.domain!r}")


def _safe_outcome(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist the domain result before it crosses the API boundary."""
    keep = ("status", "import_status", "file_id", "imported", "skipped", "invalid",
            "duplicate_file", "record_count", "summary")
    return {k: outcome[k] for k in keep if k in outcome}


__all__ = ["EmailProcessingService", "STATUS_FAILED", "STATUS_NEEDS_REVIEW",
           "STATUS_PROCESSED", "STATUS_PROCESSING", "STATUS_UNPROCESSED"]
