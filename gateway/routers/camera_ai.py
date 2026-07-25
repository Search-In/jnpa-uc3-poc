"""/api/camera-ai — Camera-AI counting + trailer/container identification.

UC-III completion, Features 3/4/5. Backed by the additive migration 0024 tables
(core.camera_ai_count / trailer_reads / container_reads):

  * Feature 3 — periodic vehicle/queue COUNTING snapshots with a server-derived
    congestion level, plus a live ``camera_ai`` WebSocket broadcast and a
    per-gate summary rollup.
  * Feature 4 — TRAILER identification reads (trailer number + towing tractor
    plate association).
  * Feature 5 — CONTAINER identification reads with full ISO-6346 check-digit
    validation (owner + category + serial + check digit).

Object-detection *events* already land in core.digital_twin_event via
/api/ai/event; these tables are the counting/aggregation + OCR read snapshots.
Additive — no existing endpoint/table is touched. RDS-backed; degrades cleanly
when postgres is unavailable (reads empty, writes 503).

    POST /api/camera-ai/counts                        -> ingest a counting snapshot
    GET  /api/camera-ai/counts?camera_id=&gate_id=&limit= -> recent snapshots
    GET  /api/camera-ai/summary                       -> latest-per-gate + totals + congestion
    POST /api/camera-ai/trailer                       -> ingest a trailer read
    GET  /api/camera-ai/trailer?limit=                -> recent trailer reads
    POST /api/camera-ai/container                     -> ingest + ISO-6346-validate a container read
    GET  /api/camera-ai/container?limit=              -> recent container reads
    GET  /api/camera-ai/dashboard                     -> rollup (reads, valid/invalid, congestion, avg conf)
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.camera_ai")

router = APIRouter(prefix="/api/camera-ai", tags=["camera-ai"])

# Congestion thresholds derived from the queue length.
_QUEUE_HIGH = 20
_QUEUE_MEDIUM = 8
_CONGESTION = {"LOW", "MEDIUM", "HIGH"}

# ISO-6346 owner-code / category-identifier + container-number regex.
_CONTAINER_RE = re.compile(r"^([A-Z]{3})([UJZ])(\d{6})(\d)$")


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k in ("class_counts", "detail"):
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


def _derive_congestion(queue_count: int) -> str:
    if queue_count >= _QUEUE_HIGH:
        return "HIGH"
    if queue_count >= _QUEUE_MEDIUM:
        return "MEDIUM"
    return "LOW"


# --- ISO-6346 -----------------------------------------------------------------
# Letter values: A=10, B=12, ... skipping every multiple of 11 (11, 22, 33 are
# not used). Positional weights are 2^0..2^9; check digit = sum mod 11 (10 -> 0).
def _build_iso6346_values() -> Dict[str, int]:
    values: Dict[str, int] = {}
    n = 10
    for i in range(26):
        while n % 11 == 0:
            n += 1
        values[chr(ord("A") + i)] = n
        n += 1
    return values


_ISO6346_VALUES = _build_iso6346_values()


def _iso6346_check_digit(prefix: str) -> Optional[int]:
    """Compute the ISO-6346 check digit for the 10-char owner+serial prefix
    (4 letters + 6 digits). Returns the expected check digit (0-9) or None if
    the prefix is malformed."""
    if len(prefix) != 10:
        return None
    total = 0
    for i, ch in enumerate(prefix):
        if ch.isalpha():
            val = _ISO6346_VALUES.get(ch)
            if val is None:
                return None
        elif ch.isdigit():
            val = int(ch)
        else:
            return None
        total += val * (2 ** i)
    check = total % 11
    return 0 if check == 10 else check


def _validate_container(number: str) -> Dict[str, Any]:
    """Validate a container number against ISO-6346. Returns
    {normalized, check_digit_ok, valid, expected_check_digit}."""
    norm = re.sub(r"[^A-Z0-9]", "", (number or "").upper())
    m = _CONTAINER_RE.match(norm)
    if not m:
        return {"normalized": norm, "check_digit_ok": False, "valid": False,
                "expected_check_digit": None}
    prefix = norm[:10]
    stated = int(norm[10])
    expected = _iso6346_check_digit(prefix)
    ok = expected is not None and expected == stated
    return {"normalized": norm, "check_digit_ok": bool(ok), "valid": bool(ok),
            "expected_check_digit": expected}


# --- Feature 3: COUNTING ------------------------------------------------------
@router.post("/counts")
async def ingest_counts(body: Dict[str, Any] = Body(...),
                        state: GatewayState = Depends(get_state)) -> dict:
    """Ingest a counting snapshot. Body: {camera_id, gate_id, vehicle_count,
    queue_count, class_counts, confidence, congestion_level?, source?}. The
    congestion level is DERIVED from queue_count when not supplied."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning

    queue_count = int(body.get("queue_count") or 0)
    vehicle_count = int(body.get("vehicle_count") or 0)
    congestion = str(body.get("congestion_level") or "").upper()
    if congestion not in _CONGESTION:
        congestion = _derive_congestion(queue_count)

    row = await execute_returning(
        """INSERT INTO core.camera_ai_count
             (camera_id, gate_id, vehicle_count, queue_count, class_counts,
              congestion_level, confidence, source, detail)
           VALUES (:cam, :gate, :vc, :qc, CAST(:cc AS jsonb),
              :cong, :conf, :src, CAST(:detail AS jsonb))
           RETURNING *""",
        {
            "cam": body.get("camera_id"), "gate": body.get("gate_id"),
            "vc": vehicle_count, "qc": queue_count,
            "cc": json.dumps(body.get("class_counts") or {}),
            "cong": congestion,
            "conf": float(body.get("confidence") or 0.0),
            "src": str(body.get("source") or "CAMERA_AI").upper(),
            "detail": json.dumps(body.get("detail") or {}),
        },
        dsn=dsn,
    )
    if not row:
        raise HTTPException(500, "insert_failed")
    row = _iso(dict(row))
    try:
        await state.ws.broadcast("camera_ai", row)
    except Exception as exc:  # noqa: BLE001
        log.warning("camera_ai_ws_failed", error=str(exc))
    REQUESTS.labels("camera_ai", "ok").inc()
    return {"ingested": True, "row": row}


@router.get("/counts")
async def list_counts(camera_id: Optional[str] = Query(default=None),
                      gate_id: Optional[str] = Query(default=None),
                      limit: int = Query(default=100, ge=1, le=1000),
                      state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "counts": []}
    from jnpa_shared.db import fetch_all

    where: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if camera_id:
        where.append("camera_id = :cam")
        params["cam"] = camera_id
    if gate_id:
        where.append("gate_id = :gate")
        params["gate"] = gate_id
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await fetch_all(
        f"SELECT * FROM core.camera_ai_count {clause} ORDER BY ts DESC LIMIT :limit",
        params, dsn=dsn)
    REQUESTS.labels("camera_ai", "ok").inc()
    return {"count": len(rows), "counts": [_iso(dict(r)) for r in rows]}


@router.get("/summary")
async def counts_summary(state: GatewayState = Depends(get_state)) -> dict:
    """Latest snapshot per gate + totals (over the latest row per gate) + a
    congestion-level distribution."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "gates": [], "totals": {"vehicle_count": 0, "queue_count": 0},
                "congestion": {}}
    from jnpa_shared.db import fetch_all

    # Latest row per gate via DISTINCT ON.
    rows = await fetch_all(
        """SELECT DISTINCT ON (gate_id) *
           FROM core.camera_ai_count
           ORDER BY gate_id, ts DESC""",
        {}, dsn=dsn)
    gates = [_iso(dict(r)) for r in rows]
    totals_v = sum(int(g.get("vehicle_count") or 0) for g in gates)
    totals_q = sum(int(g.get("queue_count") or 0) for g in gates)
    congestion: Dict[str, int] = {}
    for g in gates:
        lvl = g.get("congestion_level") or "LOW"
        congestion[lvl] = congestion.get(lvl, 0) + 1
    REQUESTS.labels("camera_ai", "ok").inc()
    return {
        "count": len(gates),
        "gates": gates,
        "totals": {"vehicle_count": totals_v, "queue_count": totals_q},
        "congestion": congestion,
    }


# --- Feature 4: TRAILER IDENTIFICATION ----------------------------------------
@router.post("/trailer")
async def ingest_trailer(body: Dict[str, Any] = Body(...),
                         state: GatewayState = Depends(get_state)) -> dict:
    """Ingest a trailer read. Body: {camera_id, gate_id, trailer_number, plate,
    vehicle_id, confidence, image_url, source?}. ``plate`` is the towing-tractor
    association."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning

    row = await execute_returning(
        """INSERT INTO core.trailer_read
             (camera_id, gate_id, trailer_number, plate, vehicle_id,
              confidence, image_url, source, detail)
           VALUES (:cam, :gate, :tn, :plate, :vid, :conf, :img, :src,
              CAST(:detail AS jsonb))
           RETURNING *""",
        {
            "cam": body.get("camera_id"), "gate": body.get("gate_id"),
            "tn": body.get("trailer_number"), "plate": body.get("plate"),
            "vid": body.get("vehicle_id"),
            "conf": float(body.get("confidence") or 0.0),
            "img": body.get("image_url"),
            "src": str(body.get("source") or "CAMERA_AI").upper(),
            "detail": json.dumps(body.get("detail") or {}),
        },
        dsn=dsn,
    )
    if not row:
        raise HTTPException(500, "insert_failed")
    REQUESTS.labels("camera_ai", "ok").inc()
    return {"ingested": True, "row": _iso(dict(row))}


@router.get("/trailer")
async def list_trailer(limit: int = Query(default=100, ge=1, le=1000),
                       state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "reads": []}
    from jnpa_shared.db import fetch_all
    rows = await fetch_all(
        "SELECT * FROM core.trailer_read ORDER BY ts DESC LIMIT :limit",
        {"limit": limit}, dsn=dsn)
    REQUESTS.labels("camera_ai", "ok").inc()
    return {"count": len(rows), "reads": [_iso(dict(r)) for r in rows]}


# --- Feature 5: CONTAINER IDENTIFICATION (ISO-6346) ---------------------------
@router.post("/container")
async def ingest_container(body: Dict[str, Any] = Body(...),
                           state: GatewayState = Depends(get_state)) -> dict:
    """Ingest a container read + validate the number against ISO-6346. Body:
    {camera_id, gate_id, container_number, iso_type?, plate, vehicle_id,
    confidence, image_url, source?}."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning

    check = _validate_container(body.get("container_number") or "")
    iso_type = str(body.get("iso_type") or body.get("size_type") or "")

    row = await execute_returning(
        """INSERT INTO core.container_read
             (camera_id, gate_id, container_number, iso_type, check_digit_ok,
              valid, plate, vehicle_id, confidence, image_url, source, detail)
           VALUES (:cam, :gate, :num, :iso, :cdok, :valid, :plate, :vid,
              :conf, :img, :src, CAST(:detail AS jsonb))
           RETURNING *""",
        {
            "cam": body.get("camera_id"), "gate": body.get("gate_id"),
            "num": check["normalized"], "iso": iso_type,
            "cdok": check["check_digit_ok"], "valid": check["valid"],
            "plate": body.get("plate"), "vid": body.get("vehicle_id"),
            "conf": float(body.get("confidence") or 0.0),
            "img": body.get("image_url"),
            "src": str(body.get("source") or "OCR").upper(),
            "detail": json.dumps({**(body.get("detail") or {}),
                                  "expected_check_digit": check["expected_check_digit"]}),
        },
        dsn=dsn,
    )
    if not row:
        raise HTTPException(500, "insert_failed")
    REQUESTS.labels("camera_ai", "ok").inc()
    return {"row": _iso(dict(row)),
            "valid": check["valid"], "check_digit_ok": check["check_digit_ok"]}


@router.get("/container")
async def list_container(limit: int = Query(default=100, ge=1, le=1000),
                         state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "reads": []}
    from jnpa_shared.db import fetch_all
    rows = await fetch_all(
        "SELECT * FROM core.container_read ORDER BY ts DESC LIMIT :limit",
        {"limit": limit}, dsn=dsn)
    REQUESTS.labels("camera_ai", "ok").inc()
    return {"count": len(rows), "reads": [_iso(dict(r)) for r in rows]}


# --- Dashboard rollup ---------------------------------------------------------
@router.get("/dashboard")
async def camera_ai_dashboard(state: GatewayState = Depends(get_state)) -> dict:
    """Rollup: trailer/container read counts (valid vs invalid), latest
    congestion level per gate, and average detection confidence."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"trailer_reads": 0, "container_reads": {"total": 0, "valid": 0, "invalid": 0},
                "congestion_by_gate": {}, "avg_confidence": {}}
    from jnpa_shared.db import fetch_all, fetch_one

    trailer_n = await fetch_one("SELECT count(*) AS n FROM core.trailer_read", {}, dsn=dsn)
    container = await fetch_one(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE valid) AS valid,
                  count(*) FILTER (WHERE NOT valid) AS invalid
           FROM core.container_read""",
        {}, dsn=dsn)
    congestion_rows = await fetch_all(
        """SELECT DISTINCT ON (gate_id) gate_id, congestion_level
           FROM core.camera_ai_count
           WHERE gate_id IS NOT NULL
           ORDER BY gate_id, ts DESC""",
        {}, dsn=dsn)
    avg_counts = await fetch_one(
        "SELECT avg(confidence) AS c FROM core.camera_ai_count", {}, dsn=dsn)
    avg_trailer = await fetch_one(
        "SELECT avg(confidence) AS c FROM core.trailer_read", {}, dsn=dsn)
    avg_container = await fetch_one(
        "SELECT avg(confidence) AS c FROM core.container_read", {}, dsn=dsn)

    def _f(row: Optional[Mapping]) -> float:
        return round(float(row["c"]), 4) if row and row["c"] is not None else 0.0

    REQUESTS.labels("camera_ai", "ok").inc()
    return {
        "trailer_reads": int(trailer_n["n"]) if trailer_n else 0,
        "container_reads": {
            "total": int(container["total"]) if container else 0,
            "valid": int(container["valid"]) if container else 0,
            "invalid": int(container["invalid"]) if container else 0,
        },
        "congestion_by_gate": {r["gate_id"]: r["congestion_level"] for r in congestion_rows},
        "avg_confidence": {
            "counts": _f(avg_counts),
            "trailer": _f(avg_trailer),
            "container": _f(avg_container),
        },
    }
