"""Email ledger persistence — raw SQL over the shared async engine.

Same shape as :mod:`services.gate_documents.repository`: ``get_engine(dsn)``,
one transaction per unit of work, bound parameters everywhere, and idempotent
upserts (``ON CONFLICT DO NOTHING`` / ``DO UPDATE``) so re-polling the mailbox
never duplicates a row.

Tables are created by infra/postgres/v3/0139_email_ingest.sql. No credential is
read or written here — the mailbox password never reaches this layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.email_ingest.repository")

_LIST_COLS = (
    "id, message_id, imap_uid, mailbox, subject, sender, recipients, cc, "
    "received_at, body_preview, attachment_count, processing_status, "
    "detected_type, target_master_table, records_detected, records_imported, "
    "records_failed, error_detail, processed_at, processed_by, created_at"
)


class EmailIngestRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    @property
    def enabled(self) -> bool:
        return bool(self._dsn)

    # ------------------------------------------------------------------ upsert
    async def upsert_message(self, summary: Any) -> Optional[int]:
        """Insert (or refresh the headers of) one polled message. Returns its id.

        Idempotent on ``message_id``: an email already in the ledger keeps its
        processing status and counters — only the mutable header/preview fields
        and the IMAP uid are refreshed, so a re-poll can never reset a PROCESSED
        email back to UNPROCESSED.
        """
        if not self.enabled:
            return None
        params = {
            "message_id": summary.message_id,
            "imap_uid": summary.imap_uid,
            "mailbox": getattr(summary, "mailbox", "INBOX") or "INBOX",
            "subject": summary.subject,
            "sender": summary.sender,
            "recipients": summary.recipients,
            "cc": summary.cc,
            "received_at": summary.received_at,
            "body_preview": summary.body_preview,
            "body_text": summary.body_text,
            "attachment_count": len(summary.attachments or []),
        }
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(text("""
                INSERT INTO core.email_message
                    (message_id, imap_uid, mailbox, subject, sender, recipients, cc,
                     received_at, body_preview, body_text, attachment_count)
                VALUES
                    (:message_id, :imap_uid, :mailbox, :subject, :sender, :recipients, :cc,
                     :received_at, :body_preview, :body_text, :attachment_count)
                ON CONFLICT (message_id) DO UPDATE SET
                    imap_uid         = EXCLUDED.imap_uid,
                    subject          = EXCLUDED.subject,
                    sender           = EXCLUDED.sender,
                    recipients       = EXCLUDED.recipients,
                    cc               = EXCLUDED.cc,
                    received_at      = EXCLUDED.received_at,
                    body_preview     = EXCLUDED.body_preview,
                    body_text        = EXCLUDED.body_text,
                    attachment_count = EXCLUDED.attachment_count,
                    updated_at       = now()
                RETURNING id"""), params)).mappings().first()
            email_id = int(row["id"]) if row else None
            if email_id is not None:
                await self._sync_attachments(conn, email_id, summary.attachments or [])
            return email_id

    async def _sync_attachments(self, conn, email_id: int, attachments: List[Any]) -> None:
        for att in attachments:
            await conn.execute(text("""
                INSERT INTO core.email_attachment
                    (email_id, filename, content_type, size_bytes, sha256)
                VALUES (:email_id, :filename, :content_type, :size_bytes, :sha256)
                ON CONFLICT (email_id, sha256) WHERE sha256 IS NOT NULL DO UPDATE SET
                    filename     = EXCLUDED.filename,
                    content_type = EXCLUDED.content_type,
                    size_bytes   = EXCLUDED.size_bytes,
                    updated_at   = now()"""), {
                "email_id": email_id, "filename": att.filename,
                "content_type": att.content_type, "size_bytes": att.size_bytes,
                "sha256": att.sha256 or None,
            })

    # -------------------------------------------------------------------- read
    async def list_messages(self, *, status: Optional[str] = None,
                            limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        if not self.enabled:
            return {"items": [], "total": 0}
        where, params = "", {}
        if status:
            where = " WHERE processing_status = :status"
            params["status"] = status
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(
                f"SELECT count(*) FROM core.email_message{where}"), params)).scalar() or 0
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                f"SELECT {_LIST_COLS} FROM core.email_message{where} "
                "ORDER BY received_at DESC NULLS LAST, id DESC "
                "LIMIT :limit OFFSET :offset"), params)).mappings().all()
        return {"items": [dict(r) for r in rows], "total": int(total)}

    async def get_message(self, email_id: int) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_LIST_COLS}, body_text FROM core.email_message WHERE id = :id"),
                {"id": email_id})).mappings().first()
            if not row:
                return None
            atts = (await conn.execute(text(
                "SELECT id, filename, content_type, size_bytes, sha256, detected_format, "
                "detected_document_type, target_master_table, process_status, "
                "records_detected, records_imported, records_failed, error_detail "
                "FROM core.email_attachment WHERE email_id = :id ORDER BY id"),
                {"id": email_id})).mappings().all()
            errs = (await conn.execute(text(
                "SELECT id, attachment_id, record_ref, error_code, error_detail, created_at "
                "FROM core.email_processing_error WHERE email_id = :id ORDER BY id LIMIT 200"),
                {"id": email_id})).mappings().all()
        out = dict(row)
        out["attachments"] = [dict(a) for a in atts]
        out["errors"] = [dict(e) for e in errs]
        return out

    async def status_of(self, email_id: int) -> Optional[str]:
        if not self.enabled:
            return None
        async with get_engine(self._dsn).connect() as conn:
            return (await conn.execute(text(
                "SELECT processing_status FROM core.email_message WHERE id = :id"),
                {"id": email_id})).scalar()

    # ------------------------------------------------------------------- write
    async def mark_status(self, email_id: int, status: str, *,
                          processed_by: Optional[str] = None,
                          error_detail: Optional[str] = None) -> None:
        if not self.enabled:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text("""
                UPDATE core.email_message
                   SET processing_status = :status,
                       processed_by      = COALESCE(:processed_by, processed_by),
                       error_detail      = :error_detail,
                       updated_at        = now()
                 WHERE id = :id"""),
                {"id": email_id, "status": status,
                 "processed_by": processed_by, "error_detail": error_detail})

    async def record_result(self, email_id: int, *, status: str, detected_type: Optional[str],
                            target_master_table: Optional[str], detected: int, imported: int,
                            failed: int, processed_by: Optional[str],
                            error_detail: Optional[str] = None) -> None:
        if not self.enabled:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text("""
                UPDATE core.email_message
                   SET processing_status   = :status,
                       detected_type       = :detected_type,
                       target_master_table = :target,
                       records_detected    = :detected,
                       records_imported    = :imported,
                       records_failed      = :failed,
                       error_detail        = :error_detail,
                       processed_at        = now(),
                       processed_by        = :processed_by,
                       updated_at          = now()
                 WHERE id = :id"""),
                {"id": email_id, "status": status, "detected_type": detected_type,
                 "target": target_master_table, "detected": detected, "imported": imported,
                 "failed": failed, "processed_by": processed_by, "error_detail": error_detail})

    async def record_attachment_result(self, email_id: int, sha256: Optional[str],
                                       filename: str, **fields: Any) -> None:
        """Update one attachment's outcome, keyed by sha256 (filename fallback)."""
        if not self.enabled:
            return
        allowed = ("detected_format", "detected_document_type", "target_master_table",
                   "process_status", "records_detected", "records_imported",
                   "records_failed", "error_detail")
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = :{k}" for k in sets)
        key_clause = "sha256 = :sha" if sha256 else "filename = :filename"
        params = {**sets, "email_id": email_id, "sha": sha256, "filename": filename}
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                f"UPDATE core.email_attachment SET {assignments}, updated_at = now() "
                f"WHERE email_id = :email_id AND {key_clause}"), params)

    async def clear_errors(self, email_id: int) -> None:
        if not self.enabled:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "DELETE FROM core.email_processing_error WHERE email_id = :id"),
                {"id": email_id})

    async def add_errors(self, email_id: int, errors: List[Dict[str, Any]]) -> None:
        if not self.enabled or not errors:
            return
        async with get_engine(self._dsn).begin() as conn:
            for err in errors[:200]:
                await conn.execute(text("""
                    INSERT INTO core.email_processing_error
                        (email_id, record_ref, error_code, error_detail)
                    VALUES (:email_id, :record_ref, :error_code, :error_detail)"""),
                    {"email_id": email_id,
                     "record_ref": str(err.get("record_ref") or err.get("ref") or "")[:200] or None,
                     "error_code": str(err.get("error_code") or err.get("code") or "error")[:100],
                     "error_detail": str(err.get("error_detail") or err.get("message")
                                         or err.get("detail") or "")[:2000] or None})


__all__ = ["EmailIngestRepository"]
