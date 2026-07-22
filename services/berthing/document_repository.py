"""Berthing full-extract persistence — raw-SQL repository for the verbatim PDF store.

Writes ONLY the additive migration-0037 tables (jnpa.berthing_report_documents /
jnpa.berthing_report_tables). It NEVER touches the normalised 0036 tables
(berthing_reports / berthing_events / berthing_import_files / berthing_import_errors)
or any other table. Raw ``text()`` over the shared async engine, mirroring
:mod:`services.berthing.repository`. JSONB columns are bound as ``json.dumps`` strings
cast to jsonb.

Idempotent: a document is de-duped on ``pdf_hash`` (sha256 of the file bytes) — the same
PDF re-uploaded returns the existing document and writes nothing new.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.berthing.document_repository")


class BerthingDocumentRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def find_by_hash(self, pdf_hash: str) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, file_name, terminal, report_date, page_count, table_count, "
                "row_count, uploaded_by, created_at "
                "FROM jnpa.berthing_report_documents WHERE pdf_hash = :h"),
                {"h": pdf_hash})).mappings().first()
        return dict(row) if row else None

    async def persist(self, result: Mapping[str, Any], *, pdf_hash: str,
                      uploaded_by: Optional[str]) -> dict:
        """Persist one extracted document + all its tables in ONE transaction.
        Re-uploading identical bytes is a safe no-op (SKIPPED_DUPLICATE)."""
        existing = await self.find_by_hash(pdf_hash)
        if existing is not None:
            return {"document_id": existing["id"], "status": "SKIPPED_DUPLICATE",
                    "terminal": existing["terminal"], "table_count": existing["table_count"],
                    "row_count": existing["row_count"], "duplicate": True}

        tables: Sequence[Mapping[str, Any]] = result.get("tables", [])
        rd = result.get("report_date")
        report_date = _dt.date.fromisoformat(rd) if isinstance(rd, str) else rd
        doc = {
            "file_name": result.get("file_name"), "terminal": result.get("terminal"),
            "report_date": report_date, "pdf_hash": pdf_hash,
            "page_count": result.get("page_count"),
            "table_count": len([t for t in tables if t["table_name"] != "UNCAPTURED_TEXT"]),
            "row_count": sum(t["row_count"] for t in tables), "uploaded_by": uploaded_by,
        }
        async with get_engine(self._dsn).begin() as conn:
            did = (await conn.execute(text(_DOC_INSERT), doc)).mappings().first()["id"]
            rows = [{
                "document_id": did, "terminal": result.get("terminal"),
                "table_name": t["table_name"], "panel_index": i,
                "page_number": t.get("page_number", 1),
                "original_columns": json.dumps(t.get("original_columns", [])),
                "rows": json.dumps(t.get("rows", [])),
                "row_count": t.get("row_count", 0),
                "extraction_note": t.get("extraction_note"),
            } for i, t in enumerate(tables)]
            if rows:
                await conn.execute(text(_TABLE_INSERT), rows)
        return {"document_id": did, "status": "IMPORTED", "terminal": doc["terminal"],
                "table_count": doc["table_count"], "row_count": doc["row_count"],
                "duplicate": False}

    async def list_documents(self, *, terminal: Optional[str], limit: int, offset: int) -> dict:
        where, params = ("", {})
        if terminal:
            where = " WHERE terminal = :terminal"; params["terminal"] = terminal
        params.update(limit=limit, offset=offset)
        async with get_engine(self._dsn).connect() as conn:
            items = (await conn.execute(text(
                "SELECT id, file_name, terminal, report_date, page_count, table_count, "
                "row_count, uploaded_by, created_at "
                f"FROM jnpa.berthing_report_documents{where} "
                "ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)).mappings().all()
            total = int((await conn.execute(text(
                f"SELECT count(*) FROM jnpa.berthing_report_documents{where}"),
                {k: v for k, v in params.items() if k == "terminal"})).scalar() or 0)
        return {"items": [dict(r) for r in items], "total": total, "limit": limit, "offset": offset}

    async def get_document(self, document_id: int) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, file_name, terminal, report_date, page_count, table_count, "
                "row_count, uploaded_by, created_at "
                "FROM jnpa.berthing_report_documents WHERE id = :id"),
                {"id": document_id})).mappings().first()
        return dict(row) if row else None

    async def get_tables(self, document_id: int) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT id, table_name, panel_index, page_number, original_columns, rows, "
                "row_count, extraction_note "
                "FROM jnpa.berthing_report_tables WHERE document_id = :id "
                "ORDER BY panel_index"), {"id": document_id})).mappings().all()
        return [dict(r) for r in rows]


_DOC_INSERT = """
INSERT INTO jnpa.berthing_report_documents
    (file_name, terminal, report_date, pdf_hash, page_count, table_count, row_count, uploaded_by)
VALUES
    (:file_name, :terminal, :report_date, :pdf_hash, :page_count, :table_count,
     :row_count, :uploaded_by)
RETURNING id
"""

_TABLE_INSERT = """
INSERT INTO jnpa.berthing_report_tables
    (document_id, terminal, table_name, panel_index, page_number, original_columns, rows,
     row_count, extraction_note)
VALUES
    (:document_id, :terminal, :table_name, :panel_index, :page_number,
     CAST(:original_columns AS jsonb), CAST(:rows AS jsonb), :row_count, :extraction_note)
"""
