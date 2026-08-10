"""On-disk store for original berthing-report PDFs.

Verbatim tables live in Postgres (``core.berthing_report_*``). The **source PDF
bytes** are kept beside them under ``BERTHING_PDF_DIR`` (default
``~/.jnpa-uc1/berthing-pdfs``), keyed by ``pdf_hash`` (sha256 hex). That lets
``GET /api/berthing/documents/{id}/pdf`` re-open the exact source file.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def pdf_dir() -> Path:
    raw = (os.environ.get("BERTHING_PDF_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".jnpa-uc1" / "berthing-pdfs"


def pdf_path(pdf_hash: str) -> Path:
    return pdf_dir() / f"{pdf_hash}.pdf"


def store_pdf(pdf_hash: str, content: bytes) -> Path:
    """Write bytes idempotently. Returns the path on disk."""
    if not pdf_hash or not content:
        raise ValueError("pdf_hash and content are required")
    root = pdf_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = pdf_path(pdf_hash)
    if not path.is_file() or path.stat().st_size != len(content):
        tmp = path.with_suffix(".pdf.tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    return path


def load_pdf(pdf_hash: str) -> Optional[bytes]:
    path = pdf_path(pdf_hash)
    if not path.is_file():
        return None
    return path.read_bytes()


__all__ = ["pdf_dir", "pdf_path", "store_pdf", "load_pdf"]
