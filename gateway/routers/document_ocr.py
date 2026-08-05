"""/api/ocr — Document OCR & structured field extraction (Feature 6).

Turns an uploaded document (EIR / gate slip / LR / invoice / e-way bill / permit)
into a stored, searchable record with extracted key-value fields. RDS-backed
(core.document_ocr). Additive — no existing endpoint/table is touched.

Extraction is a three-rung LIVE -> LOCAL -> MOCK chain (``_extract_async``):

  1. **LIVE** — POST the image to the dedicated EIR OCR service
     (``ingest/eir_ocr``, Tesseract, host 8210) and use its STRUCTURED fields.
     This service is validated against the four real WhatsApp gate slips and
     returns real values (e.g. EIRNo 4339869, LICNo MH43BX1488, ContainerNo
     MSMU1908508). Until 2026-08-04 it was built by compose but nothing ever
     called it: the dashboard mirrored `_mock_fields()` under a "field parsing
     TODO", so a working capability looked fabricated.
  2. **LOCAL** — in-process pytesseract + PIL. Yields real ``raw_text``; fields
     are parsed with the same extractors the service uses when importable.
  3. **MOCK** — deterministic per-doc_type stand-in so a demo never crashes on a
     missing engine. Tagged ``source="MOCK"`` and never presented as a real read.

Every rung is tagged in ``source`` so the UI can never mistake one for another.

    POST /api/ocr/document                 -> upload + OCR + persist (EXTRACTED)
    GET  /api/ocr/documents                 -> recent docs (no raw_text in list)
    GET  /api/ocr/documents/{id}            -> one full record (incl raw_text)
    POST /api/ocr/documents/{id}/verify     -> mark VERIFIED (+ optional field fixes)
    GET  /api/ocr/health                    -> {engine, configured, upstream}
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.document_ocr")

router = APIRouter(prefix="/api/ocr", tags=["ocr"])

# Recognised document types (anything else is accepted as UNKNOWN).
# EIR and GATE_SLIP route to the dedicated OCR service; the rest use the local
# text read. Both are additive — "UNKNOWN" still accepts anything.
_DOC_TYPES = {"LR", "INVOICE", "EWAYBILL", "PERMIT", "EIR", "GATE_SLIP", "UNKNOWN"}
_STATUS = {"UPLOADED", "EXTRACTED", "VERIFIED", "FAILED"}

#: doc_types the dedicated EIR OCR service understands.
_EIR_DOC_TYPES = {"EIR", "GATE_SLIP"}

#: Upstream call budget. The service runs several preprocessing variants with an
#: early exit; ~8s is comfortably above its p99 on the real slips while keeping a
#: hung sidecar from stalling an operator's upload.
_EIR_OCR_TIMEOUT_S = 8.0


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


def _flatten_fields(raw: Any) -> tuple[Dict[str, Any], Dict[str, float]]:
    """Normalise an eir_ocr field map to ``({name: value}, {name: confidence})``.

    Both ``eir_ocr.extract.fields_as_dict`` and the service's ``POST /infer``
    return the RICH shape ``{"EIRNo": {"value": ..., "conf": ..., "evidence": ...}}``.
    The stored ``core.document_ocr.fields`` column and every existing consumer
    expect flat ``{name: value}`` scalars, so flatten here — once — and keep the
    per-field confidences alongside rather than discarding them.

    Tolerates the flat shape too, so a future service version that returns plain
    scalars needs no change here. Blank sentinels are dropped: ``__BLANK__`` means
    "this slip genuinely has no such field" (e.g. eir2_dpworld_nsict has no
    container number), which is different from "not read".
    """
    values: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}
    for key, item in (raw or {}).items():
        if isinstance(item, dict):
            value = item.get("value")
            conf = item.get("conf")
            if isinstance(conf, (int, float)):
                confidences[key] = float(conf)
        else:
            value = item
        if value in (None, "", "__BLANK__"):
            continue
        values[key] = value
    return values, confidences


def _parse_fields_from_text(text: str, doc_type: str) -> Dict[str, Any]:
    """Parse structured fields out of OCR text using the eir_ocr extractors.

    Reuses ``ingest/eir_ocr``'s regex extractors when that package is importable
    in-process, so the LOCAL rung produces the SAME field names as the LIVE rung
    rather than mirroring mock values (the bug this replaces). Returns ``{}`` when
    the package is absent — an empty dict is honest; invented fields are not.
    """
    if not text:
        return {}
    try:
        from eir_ocr.extract import extract_eir_fields, fields_as_dict  # type: ignore

        # extract_eir_fields returns Dict[str, FieldValue] — a dict, but of
        # dataclasses, not JSON-serialisable scalars. fields_as_dict flattens it
        # to {name: value}, so it must ALWAYS be applied (an isinstance(dict)
        # short-circuit here would leak FieldValue objects into the response).
        parsed = extract_eir_fields(text, doc_type="EIR" if doc_type in _EIR_DOC_TYPES else doc_type)
        values, _conf = _flatten_fields(fields_as_dict(parsed))
        return values
    except Exception as exc:  # noqa: BLE001 — extractor is optional in-process
        log.debug("ocr_local_field_parse_unavailable", doc_type=doc_type, error=str(exc))
        return {}


# High-value identifiers a caller can validate a real read against. Deliberately
# narrow: an ISO 6346 container number, an Indian plate, and a numeric EIR no.
_VALIDATORS = {
    "ContainerNo": re.compile(r"^[A-Z]{4}\d{7}$"),
    "LICNo": re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{1,4}$"),
    "EIRNo": re.compile(r"^\d{4,10}$"),
}


def _validate_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Per-field format verdicts for the high-value identifiers.

    Reported alongside the fields (never used to DROP a value) so an operator can
    see at a glance which reads are trustworthy and which need the verify step.
    """
    out: Dict[str, Any] = {}
    for key, pattern in _VALIDATORS.items():
        raw = fields.get(key)
        if raw in (None, "", "__BLANK__"):
            continue
        normalised = re.sub(r"[^A-Z0-9]", "", str(raw).upper())
        out[key] = {
            "value": raw,
            "normalised": normalised,
            "valid": bool(pattern.match(normalised)),
        }
    return out


async def _extract_via_service(state: GatewayState, raw_bytes: bytes,
                               content_type: Optional[str], doc_type: str,
                               filename: str) -> Optional[Dict[str, Any]]:
    """Rung 1 — POST the image to the dedicated EIR OCR service.

    Returns ``None`` (never raises) when the service is unconfigured,
    unreachable, non-2xx, or produced no usable fields — the caller then falls
    through to the local read. That "no usable fields" case matters: a reachable
    service that returned an empty extraction is not a better answer than a local
    text read, so we do not let it short-circuit the chain.
    """
    base = (getattr(state.cfg, "eir_ocr_url", "") or "").strip()
    if not base or not raw_bytes:
        return None
    url = base.rstrip("/") + "/infer"
    try:
        resp = await state.http.post(
            url,
            files={"file": (filename or "upload.jpg", raw_bytes,
                            content_type or "application/octet-stream")},
            data={"doc_type": doc_type},
            timeout=_EIR_OCR_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — sidecar down is expected offline
        log.info("eir_ocr_unreachable", url=url, doc_type=doc_type, error=str(exc))
        return None
    if resp.status_code != 200:
        log.info("eir_ocr_non_200", url=url, status=resp.status_code)
        return None
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("eir_ocr_bad_json", url=url, error=str(exc))
        return None

    # The service returns the RICH shape {name: {value, conf, evidence}} (it
    # serialises eir_ocr's FieldValue dataclasses). Flatten to scalars for the
    # jsonb column and existing clients; keep the per-field confidences.
    fields, field_conf = _flatten_fields(data.get("fields"))
    if not fields:
        log.info("eir_ocr_no_fields", doc_type=doc_type,
                 engine_ready=data.get("engine_ready"))
        return None

    log.info("eir_ocr_live", doc_type=doc_type, fields=len(fields),
             confidence=data.get("confidence"), sha256=data.get("sha256"))
    return {
        "raw_text": data.get("raw_text") or "",
        "fields": fields,
        "confidence": data.get("confidence"),
        "source": "OCR_SERVICE",
        "validation": _validate_fields(fields),
        "field_confidence": field_conf,
        "engine": {
            "service": "eir-ocr",
            "ready": data.get("engine_ready"),
            "tesseract_version": data.get("tesseract_version"),
            "variants_run": data.get("variants_run"),
            "sha256": data.get("sha256"),
        },
    }


def _extract_local(raw_bytes: bytes, content_type: Optional[str],
                   doc_type: str) -> Optional[Dict[str, Any]]:
    """Rung 2 — in-process pytesseract read. ``None`` when unavailable/empty."""
    ctype = (content_type or "").lower()
    if not raw_bytes or not ctype.startswith("image/"):
        return None
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(raw_bytes))
        text = (pytesseract.image_to_string(img) or "").strip()
        if not text:
            log.info("ocr_empty_text_layer", doc_type=doc_type)
            return None
        fields = _parse_fields_from_text(text, doc_type)
        return {
            "raw_text": text,
            "fields": fields,
            "confidence": 0.9 if fields else 0.6,
            "source": "OCR",
            "validation": _validate_fields(fields),
        }
    except Exception as exc:  # noqa: BLE001 — degrade, never crash
        log.info("ocr_real_read_failed", doc_type=doc_type, error=str(exc))
        return None


def _extract_mock(raw_bytes: bytes, doc_type: str) -> Dict[str, Any]:
    """Rung 3 — deterministic stand-in, unmistakably tagged MOCK."""
    seed = hashlib.sha256(raw_bytes or (doc_type.encode())).hexdigest()
    fields = _mock_fields(doc_type, seed)
    raw_text = f"[MOCK OCR] {doc_type} document\n" + "\n".join(
        f"{k}: {v}" for k, v in fields.items()
    )
    return {
        "raw_text": raw_text,
        "fields": fields,
        "confidence": 0.75,
        "source": "MOCK",
        "validation": {},
    }


async def _extract_async(state: GatewayState, raw_bytes: bytes,
                         content_type: Optional[str], doc_type: str,
                         filename: str = "") -> Dict[str, Any]:
    """LIVE -> LOCAL -> MOCK. Always returns a usable result; never raises."""
    if doc_type in _EIR_DOC_TYPES:
        via_service = await _extract_via_service(
            state, raw_bytes, content_type, doc_type, filename)
        if via_service is not None:
            return via_service
    local = _extract_local(raw_bytes, content_type, doc_type)
    if local is not None:
        return local
    return _extract_mock(raw_bytes, doc_type)


def _extract(raw_bytes: bytes, content_type: Optional[str], doc_type: str) -> Dict[str, Any]:
    """Synchronous LOCAL -> MOCK extraction.

    Retained unchanged in behaviour for any in-process caller that has no
    GatewayState (and for the existing tests). The upload route uses
    ``_extract_async``, which adds the LIVE rung in front of these two.
    """
    local = _extract_local(raw_bytes, content_type, doc_type)
    if local is not None:
        return local
    return _extract_mock(raw_bytes, doc_type)


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
        # Return the GATEWAY PROXY path, not the internal ``s3://minio…`` URI
        # (fix G-2). ``s3://{bucket}/{object}`` is unreachable from a browser, so
        # a stored document could never actually be retrieved from the persisted
        # reference. ``/api/evidence/{object_name}`` is the same-origin route that
        # streams the object back (gateway/routers/evidence.py) — the convention
        # gateway/routers/violations.py:_store_evidence already follows.
        url = f"/api/evidence/{object_name}"
        log.info("ocr_document_stored", object_name=object_name, bucket=bucket,
                 bytes=len(raw_bytes))
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

    validation: Dict[str, Any] = {}
    engine_info: Dict[str, Any] = {}
    try:
        result = await _extract_async(state, raw_bytes, file.content_type, dtype,
                                      file.filename or "")
        status = "EXTRACTED"
        raw_text = result["raw_text"]
        fields = result["fields"]
        confidence = result["confidence"]
        source = result["source"]
        validation = result.get("validation") or {}
        engine_info = result.get("engine") or {}
    except Exception as exc:  # noqa: BLE001 — the chain should never raise, but be safe
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
    out = _iso(dict(row))
    # Additive response keys — the persisted columns are unchanged, so no
    # migration and no existing client breaks. `validation` lets the operator see
    # which high-value identifiers parsed cleanly; `engine` says WHICH rung read
    # the document, so a MOCK can never be mistaken for a real read.
    if validation:
        out["validation"] = validation
    if engine_info:
        out["engine"] = engine_info
    # Evidence object reference (fix G-2): the bucket-relative key alongside the
    # resolvable proxy URL already in `storage_url`, so a caller can retrieve the
    # stored document via GET /api/evidence/{object_path} without parsing the URL.
    out["object_path"] = object_name if storage_url else None
    out["object_name"] = file.filename
    return out


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
    """Report which extraction rung is actually available.

    Probes the dedicated EIR OCR service so the operator can tell — before the
    demo, not during it — whether an EIR upload will produce REAL fields or fall
    back. ``engine`` keeps its original values for backward compatibility; the
    new ``upstream`` / ``active_rung`` keys are additive.
    """
    available = _tesseract_available()
    base = (getattr(state.cfg, "eir_ocr_url", "") or "").strip()
    upstream: Dict[str, Any] = {"url": base or None, "reachable": False}
    if base:
        try:
            resp = await state.http.get(base.rstrip("/") + "/healthz", timeout=3.0)
            if resp.status_code == 200:
                body = resp.json()
                upstream.update({
                    "reachable": True,
                    "status": body.get("status"),
                    "engine_ready": body.get("engine_ready"),
                    "tesseract_version": body.get("tesseract_version"),
                })
        except Exception as exc:  # noqa: BLE001 — an absent sidecar is not an error
            upstream["error"] = str(exc)

    if upstream.get("engine_ready"):
        active = "OCR_SERVICE"
    elif available:
        active = "OCR"
    else:
        active = "MOCK"

    return {
        "engine": "tesseract" if available else "mock",
        "configured": bool(state.cfg.postgres_dsn),
        "upstream": upstream,
        "active_rung": active,
        "eir_doc_types": sorted(_EIR_DOC_TYPES),
    }
