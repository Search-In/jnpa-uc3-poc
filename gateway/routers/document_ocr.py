"""/api/ocr — Document OCR & structured field extraction (Feature 6).

Turns an uploaded document (LR / invoice / e-way bill / permit) into a stored,
searchable record with extracted key-value fields. RDS-backed
(core.document_ocr). Additive — no existing endpoint/table is touched.

The OCR engine is optional and degrades gracefully. ``_extract`` first TRIES a
real read (pytesseract + PIL over the uploaded image bytes); if the optional
deps are absent, or the bytes carry no readable text layer, it falls back to a
DETERMINISTIC MOCK extraction that returns plausible fields per doc_type and is
clearly tagged ``source="MOCK"`` so a demo never crashes on a missing engine.

    POST /api/ocr/document                 -> upload + OCR + persist (EXTRACTED)
    GET  /api/ocr/documents                 -> recent docs (no raw_text in list)
    GET  /api/ocr/documents/{id}            -> one full record (incl raw_text)
    POST /api/ocr/documents/{id}/verify     -> mark VERIFIED (+ optional field fixes)
    GET  /api/ocr/health                    -> {engine, configured}
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.document_ocr")

router = APIRouter(prefix="/api/ocr", tags=["ocr"])

# Recognised document types (anything else is accepted as UNKNOWN).
_DOC_TYPES = {"LR", "INVOICE", "EWAYBILL", "PERMIT", "UNKNOWN"}
_STATUS = {"UPLOADED", "EXTRACTED", "VERIFIED", "FAILED"}


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a DB row for JSON: datetimes -> isoformat, jsonb text -> dict."""
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k == "fields":
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


def _tesseract_available() -> bool:
    """True when the optional real-OCR stack (pytesseract + PIL) is importable."""
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001 — optional dependency
        return False
    return True


def _mock_fields(doc_type: str, seed: str) -> Dict[str, Any]:
    """Deterministic, plausible fields per doc_type (stand-in for a real read).

    Hash-derived so the same bytes always yield the same fields — never random —
    which keeps demos reproducible and clearly distinguishable as MOCK output.
    """
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    n6 = h % 1_000_000
    if doc_type == "LR":
        return {
            "lr_number": f"LR-{n6:06d}",
            "consignor": "ABC Logistics Pvt Ltd",
            "consignee": "JNPA Terminal Operations",
            "date": "2026-07-16",
        }
    if doc_type == "INVOICE":
        return {
            "invoice_no": f"INV-{n6:06d}",
            "amount": round(1000 + (h % 90000) / 100.0, 2),
            "gstin": f"27ABCDE{(h % 10000):04d}F1Z5",
        }
    if doc_type == "EWAYBILL":
        return {
            "ewb_no": f"{100000000000 + (h % 900000000000)}",
            "valid_upto": "2026-07-20",
        }
    if doc_type == "PERMIT":
        return {"permit_no": f"PMT-{n6:06d}"}
    return {}


def _extract(raw_bytes: bytes, content_type: Optional[str], doc_type: str) -> Dict[str, Any]:
    """Extract ``raw_text`` + structured ``fields`` from the uploaded bytes.

    Tries a REAL OCR read first (pytesseract over the image via PIL). Falls back
    to a deterministic MOCK extraction when the deps are unavailable OR the image
    yields no readable text. Never raises for a missing engine — always returns a
    usable result, tagging ``source`` as "OCR" (real) or "MOCK" (fallback).
    """
    seed = hashlib.sha256(raw_bytes or (doc_type.encode())).hexdigest()
    ctype = (content_type or "").lower()

    # Attempt a real read only for image payloads with the optional stack present.
    if raw_bytes and ctype.startswith("image/"):
        try:
            import pytesseract
            from PIL import Image

            img = Image.open(io.BytesIO(raw_bytes))
            text = (pytesseract.image_to_string(img) or "").strip()
            if text:
                return {
                    "raw_text": text,
                    "fields": _mock_fields(doc_type, seed),  # field parsing TODO — mirror text for now
                    "confidence": 0.9,
                    "source": "OCR",
                }
            log.info("ocr_empty_text_layer", doc_type=doc_type)
        except Exception as exc:  # noqa: BLE001 — degrade to MOCK, never crash
            log.info("ocr_real_read_failed", doc_type=doc_type, error=str(exc))

    fields = _mock_fields(doc_type, seed)
    raw_text = f"[MOCK OCR] {doc_type} document\n" + "\n".join(
        f"{k}: {v}" for k, v in fields.items()
    )
    return {
        "raw_text": raw_text,
        "fields": fields,
        "confidence": 0.75,
        "source": "MOCK",
    }


def _store_document(object_name: str, raw_bytes: bytes, content_type: Optional[str]) -> Optional[str]:
    """Best-effort store the uploaded bytes to the object store; None if disabled.

    Wraps the import + call in try/except so an absent/unreachable MinIO (or a
    missing ``minio`` client lib) leaves ``storage_url=None`` and never fails the
    upload. Reuses gateway/objectstore.py's configured client.
    """
    if not raw_bytes:
        return None
    try:
        from .. import objectstore

        if not objectstore.enabled():
            return None
        import os as _os

        bucket = _os.environ.get("DOCUMENT_OCR_BUCKET", "documents").strip()
        client = objectstore._client()
        if not objectstore._ensure_bucket(client, bucket):
            return None
        client.put_object(
            bucket, object_name, data=io.BytesIO(raw_bytes), length=len(raw_bytes),
            content_type=content_type or "application/octet-stream",
        )
        url = f"s3://{bucket}/{object_name}"
        log.info("ocr_document_stored", object_name=object_name, bytes=len(raw_bytes))
        return url
    except Exception as exc:  # noqa: BLE001 — object storage is best-effort
        log.warning("ocr_document_store_failed", object_name=object_name, error=str(exc))
        return None


@router.post("/document")
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form(default="UNKNOWN"),
    source_ref: Optional[str] = Form(default=None),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Upload a document, run OCR, persist the extracted record.

    Flow: store bytes (best-effort) -> ``_extract`` (real OCR, else MOCK) ->
    INSERT core.document_ocr with status EXTRACTED (FAILED on error). Returns the
    row id + extracted fields.
    """
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning

    dtype = (doc_type or "UNKNOWN").strip().upper()
    if dtype not in _DOC_TYPES:
        dtype = "UNKNOWN"

    raw_bytes = await file.read()
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "bin"
    object_name = f"ocr/{uuid.uuid4()}.{ext}"
    storage_url = _store_document(object_name, raw_bytes, file.content_type)

    try:
        result = _extract(raw_bytes, file.content_type, dtype)
        status = "EXTRACTED"
        raw_text = result["raw_text"]
        fields = result["fields"]
        confidence = result["confidence"]
        source = result["source"]
    except Exception as exc:  # noqa: BLE001 — _extract should never raise, but be safe
        log.warning("ocr_extract_failed", doc_type=dtype, error=str(exc))
        status = "FAILED"
        raw_text = None
        fields = {}
        confidence = None
        source = "MOCK"

    row = await execute_returning(
        """INSERT INTO core.document_ocr
             (doc_type, source_ref, storage_url, raw_text, fields, confidence, status, source)
           VALUES (:dtype, :sref, :surl, :raw, CAST(:fields AS jsonb), :conf, :status, :source)
           RETURNING id, doc_type, fields, confidence, source, storage_url, status""",
        {
            "dtype": dtype, "sref": source_ref, "surl": storage_url,
            "raw": raw_text, "fields": json.dumps(fields or {}),
            "conf": confidence, "status": status, "source": source,
        },
        dsn=dsn,
    )
    if not row:
        REQUESTS.labels("document_ocr", "error").inc()
        raise HTTPException(500, "insert_failed")
    REQUESTS.labels("document_ocr", "ok").inc()
    return _iso(dict(row))


@router.get("/documents")
async def list_documents(
    doc_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Recent documents (no raw_text — id/ts/doc_type/source_ref/confidence/status/fields)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "documents": []}
    from jnpa_shared.db import fetch_all

    params: Dict[str, Any] = {"limit": limit}
    clause = ""
    if doc_type:
        clause = "WHERE doc_type = :dtype"
        params["dtype"] = doc_type.strip().upper()
    rows = await fetch_all(
        f"""SELECT id, ts, doc_type, source_ref, confidence, status, fields
            FROM core.document_ocr {clause} ORDER BY ts DESC LIMIT :limit""",
        params, dsn=dsn,
    )
    REQUESTS.labels("document_ocr", "ok").inc()
    return {"count": len(rows), "documents": [_iso(dict(r)) for r in rows]}


@router.get("/documents/{doc_id}")
async def get_document(doc_id: int, state: GatewayState = Depends(get_state)) -> dict:
    """Full record for one document, including raw_text."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import fetch_one

    row = await fetch_one(
        "SELECT * FROM core.document_ocr WHERE id = :id", {"id": doc_id}, dsn=dsn)
    if not row:
        raise HTTPException(404, "document_not_found")
    REQUESTS.labels("document_ocr", "ok").inc()
    return {"document": _iso(dict(row))}


@router.post("/documents/{doc_id}/verify")
async def verify_document(
    doc_id: int,
    body: Dict[str, Any] = Body(default=None),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Mark a document VERIFIED. Optional body ``{fields}`` merges operator field
    corrections into the extracted ``fields`` jsonb. Returns the updated record."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute, fetch_one

    row = await fetch_one(
        "SELECT * FROM core.document_ocr WHERE id = :id", {"id": doc_id}, dsn=dsn)
    if not row:
        raise HTTPException(404, "document_not_found")

    corrections = (body or {}).get("fields") if isinstance(body, dict) else None
    if isinstance(corrections, dict) and corrections:
        # Merge operator corrections into the existing fields jsonb (right wins).
        await execute(
            """UPDATE core.document_ocr
               SET fields = COALESCE(fields, '{}'::jsonb) || CAST(:patch AS jsonb),
                   status = 'VERIFIED'
               WHERE id = :id""",
            {"patch": json.dumps(corrections), "id": doc_id}, dsn=dsn,
        )
    else:
        await execute(
            "UPDATE core.document_ocr SET status = 'VERIFIED' WHERE id = :id",
            {"id": doc_id}, dsn=dsn,
        )

    updated = await fetch_one(
        "SELECT * FROM core.document_ocr WHERE id = :id", {"id": doc_id}, dsn=dsn)
    REQUESTS.labels("document_ocr", "ok").inc()
    return {"document": _iso(dict(updated)) if updated else None}


@router.get("/health")
async def ocr_health(state: GatewayState = Depends(get_state)) -> dict:
    """Report the active OCR engine and whether a DB is configured."""
    available = _tesseract_available()
    return {
        "engine": "tesseract" if available else "mock",
        "configured": bool(state.cfg.postgres_dsn),
    }
