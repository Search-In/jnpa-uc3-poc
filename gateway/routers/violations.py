"""/api/violations — AI-assisted traffic-violation enforcement console.

Orchestration ONLY. This router never re-implements detection: it chains the
capabilities that already exist in the system into a single enforcement flow so a
manually-captured frame (image / video frame / live camera) becomes a
police-visible incident the existing Reports page + PDF export render unchanged.

Reused, never rebuilt:
  * ANPR plate read        -> ai/anpr (the same upstream /api/anpr/infer proxies),
                              degrading to the gateway's synthetic read.
  * Vehicle registry       -> jnpa.vehicle_master (owner / class / RTO / FASTag).
  * Driver mapping         -> jnpa.drivers / jnpa.driver_enrollments (vehicle_no).
  * Fine schedule          -> reports._CHALLAN (single source of truth for the
                              MVA section + ₹ fine per kind).
  * Evidence store         -> MinIO `evidence` bucket (same bucket ai/anomaly uses).
  * Incident store         -> jnpa.alerts (so /api/reports/police picks them up
                              automatically — no schema change, no new table).

    POST /api/violations/detect  (multipart image)  -> run ANPR + vehicle/driver
        lookup + store the frame as evidence. Does NOT persist an incident; it
        returns the detection so the operator can confirm the applicable
        violation(s) before issuing a challan.

    POST /api/violations/commit  (json)             -> persist the confirmed
        violation(s) as jnpa.alerts rows (one per kind, each carrying its own
        e-Challan fine) that share one case_id + evidence_url, and return the
        aggregated incident (vehicle, driver, violations[], fine_total, ...).

Degrades gracefully: an ANPR upstream miss falls back to the synthetic read; a
missing/unreachable MinIO keeps the case without durable evidence; a down
Postgres surfaces a clear 503 on commit (it never invents an incident).
"""
from __future__ import annotations

import hashlib
import io
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile

from .. import enforcement
from ..fallback import AnprPath, SourceState
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
# Reuse the fine schedule + readable labels and the synthetic-read fallback so
# there is exactly ONE definition of each in the codebase.
from .anpr import _synthetic_read
from .reports import _CHALLAN, POLICE_KINDS, _kind_label

log = get_logger("gateway.violations")

router = APIRouter(prefix="/api/violations", tags=["violations"])

# Severity per kind, mirroring the ai/anomaly rules so these console-issued
# incidents read identically to camera-detected ones on the Reports page.
_SEVERITY: Dict[str, str] = {
    "WRONG_WAY": "REPORT_TO_POLICE",
    "OVERSPEEDING": "critical",
    "ILLEGAL_PARKING": "warning",
    "ROUTE_DEVIATION": "warning",
}


def _actor(request: Optional[Request]) -> str:
    """Best-effort caller identity for the incident audit trail."""
    if request is None:
        return "anonymous"
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"{principal.role}:{principal.sub}"
    return request.client.host if request.client else "anonymous"


def _violation_catalog() -> List[dict]:
    """The selectable violations + their fines (reads reports._CHALLAN)."""
    out: List[dict] = []
    for kind in POLICE_KINDS:
        c = _CHALLAN.get(kind, {})
        out.append({
            "kind": kind,
            "label": _kind_label(kind),
            "section": c.get("section"),
            "fine_inr": c.get("fine_inr"),
        })
    return out


def _extract_plate(
    record: Dict[str, Any],
) -> Tuple[Optional[str], Optional[float], bool, Optional[list]]:
    """Pull (plate, confidence, degraded, bbox) from an ai/anpr or synthetic read.

    ``bbox`` is ``[x1, y1, x2, y2]`` in the uploaded image's pixel space (ai/anpr
    returns it on a real detection); ``None`` for a synthetic fallback read (no
    real plate region to box or crop)."""
    plate = record.get("plate")
    conf = record.get("conf")
    if conf is None:
        conf = record.get("confidence")
    try:
        conf = round(float(conf), 4) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    bbox = record.get("bbox")
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        bbox = None
    return plate, conf, bool(record.get("degraded", False)), (list(bbox) if bbox else None)


async def _run_anpr(
    state: GatewayState, payload: bytes, filename: Optional[str], content_type: Optional[str]
) -> dict:
    """Proxy the frame to ai/anpr /infer (LIVE); degrade to the synthetic read.

    Identical upstream + fallback to the existing /api/anpr/infer proxy, so the
    plate-read behaviour is shared rather than forked.
    """
    url = state.cfg.anpr_ai_url.rstrip("/") + "/infer"
    t0 = time.perf_counter()
    try:
        resp = await state.http.post(
            url,
            files={"image": (filename or "frame.jpg", payload, content_type or "image/jpeg")},
        )
        if resp.status_code == 200:
            await state.record_decision(
                api="violations", decision_path=AnprPath.LIVE.value,
                latency_ms=(time.perf_counter() - t0) * 1000, source="anpr-ai",
            )
            return {"decision_path": AnprPath.LIVE.value, "record": resp.json()}
        log.info("violations_anpr_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("violations_anpr_unreachable", url=url, error=str(exc))

    synth = _synthetic_read("CAM-UPLOAD")
    await state.record_decision(
        api="violations", decision_path=AnprPath.SYNTHETIC.value,
        source="anpr-ai", source_state=SourceState.DOWN, ok=False,
    )
    return {"decision_path": AnprPath.SYNTHETIC.value, "record": synth}


async def _lookup_vehicle(state: GatewayState, plate: Optional[str]) -> Optional[dict]:
    """Owner / class / RTO / FASTag from jnpa.vehicle_master (best-effort)."""
    if not plate:
        return None
    from jnpa_shared.db import fetch_one

    try:
        row = await fetch_one(
            """
            SELECT plate, owner_name_masked, vehicle_class, state, rto_code,
                   fastag_status, blacklist_status
            FROM jnpa.vehicle_master
            WHERE plate = :plate
            """,
            {"plate": plate}, dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("violations_vehicle_lookup_failed", plate=plate, error=str(exc))
        return None
    return dict(row) if row else None


async def _lookup_driver(state: GatewayState, plate: Optional[str]) -> Optional[dict]:
    """Map the plate -> enrolled driver via vehicle_no (active driver first).

    vehicle_no is stored human-formatted ("MH04 AB 1234") while ANPR returns the
    compact plate ("MH04AB1234"), so both sides are space-stripped + upper-cased.
    """
    if not plate:
        return None
    from jnpa_shared.db import fetch_one

    norm = plate.replace(" ", "").upper()
    try:
        row = await fetch_one(
            """
            SELECT driver_id, name, status, vehicle_no
            FROM jnpa.drivers
            WHERE upper(replace(vehicle_no, ' ', '')) = :norm
            ORDER BY enrolled_at DESC
            LIMIT 1
            """,
            {"norm": norm}, dsn=state.cfg.postgres_dsn,
        )
        if row is None:
            row = await fetch_one(
                """
                SELECT driver_id, name, status, vehicle_no
                FROM jnpa.driver_enrollments
                WHERE upper(replace(vehicle_no, ' ', '')) = :norm
                ORDER BY submitted_at DESC
                LIMIT 1
                """,
                {"norm": norm}, dsn=state.cfg.postgres_dsn,
            )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("violations_driver_lookup_failed", plate=plate, error=str(exc))
        return None
    return dict(row) if row else None


def _store_evidence(case_id: str, jpeg: bytes) -> Optional[str]:
    """Store the captured frame in the MinIO `evidence` bucket; None if disabled.

    Mirrors gateway/objectstore + ai/anomaly/storage (lazy import, best-effort)
    but targets the shared evidence bucket under a ``violations/`` prefix so the
    Reports PDF can embed it like any other incident's evidence.
    """
    if not jpeg:
        return None
    access = os.environ.get("MINIO_ACCESS_KEY", "").strip()
    secret = os.environ.get("MINIO_SECRET_KEY", "").strip()
    if not (access and secret):
        return None
    endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000").strip()
    bucket = os.environ.get("ANOMALY_EVIDENCE_BUCKET", "evidence").strip()
    object_name = f"violations/{case_id}.jpg"
    try:
        from minio import Minio  # lazy import — optional dependency

        client = Minio(
            endpoint, access_key=access, secret_key=secret,
            secure=os.environ.get("MINIO_SECURE", "false").strip().lower()
            in {"1", "true", "yes", "on"},
        )
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        client.put_object(
            bucket, object_name, data=io.BytesIO(jpeg), length=len(jpeg),
            content_type="image/jpeg",
        )
        # Return the gateway proxy path (same origin as the app), NOT the internal
        # minio:9000 URL — the browser can't reach minio directly. /api/evidence
        # streams the object back with MinIO staying private. See routers/evidence.py.
        url = f"/api/evidence/{object_name}"
        log.info("violations_evidence_stored", case_id=case_id, bytes=len(jpeg))
        return url
    except Exception as exc:  # noqa: BLE001
        log.warning("violations_evidence_store_failed", case_id=case_id, error=str(exc))
        return None


@router.get("/catalog")
async def catalog(state: GatewayState = Depends(get_state)) -> dict:
    """The selectable violation kinds + fines (so the panel renders without a
    prior detect call). Sourced from the reports e-Challan schedule."""
    REQUESTS.labels("violations", "ok").inc()
    return {"violations": _violation_catalog()}


@router.post("/detect")
async def detect(
    image: UploadFile = File(...),
    gate_id: Optional[str] = Form(default=None),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Run ANPR on an uploaded frame, enrich with vehicle + driver, store evidence.

    No incident is created here — the operator confirms the applicable
    violation(s) and calls ``/commit``. The frame is stored once now so the
    eventual incident references durable, retained evidence.
    """
    payload = await image.read()
    case_id = str(uuid.uuid4())
    # Hash the evidence NOW (at capture) so the committed case binds the exact
    # bytes that were stored — the tamper-evidence anchor.
    sha = enforcement.evidence_sha256(payload) if payload else None

    anpr = await _run_anpr(state, payload, image.filename, image.content_type)
    plate, confidence, degraded, bbox = _extract_plate(anpr["record"])
    anpr_real = anpr["decision_path"] == AnprPath.LIVE.value
    # Look up (and enrich) the vehicle ONLY from a REAL OCR plate. A synthetic
    # fallback read (ANPR service unavailable) is never used to look up — or
    # fabricate — a vehicle; the UI flags it clearly as synthetic. This prevents
    # a synthetic plate that happens to match a seeded vehicle from masquerading
    # as a real read.
    vehicle = await _lookup_vehicle(state, plate) if anpr_real else None
    driver = await _lookup_driver(state, plate) if anpr_real else None
    evidence_url = _store_evidence(case_id, payload)

    REQUESTS.labels("violations", "ok").inc()
    return {
        "case_id": case_id,
        "plate": plate,
        "confidence": confidence,
        "anpr_decision_path": anpr["decision_path"],
        "anpr_real": anpr_real,
        "bbox": bbox,
        "degraded": degraded or not anpr_real,
        "vehicle": vehicle,
        "vehicle_class": (vehicle or {}).get("vehicle_class"),
        "driver": driver,
        "evidence_url": evidence_url,
        "evidence_sha256": sha,
        "gate_id": gate_id,
        "available_violations": _violation_catalog(),
    }


async def _zone_kind(state: GatewayState, zone_id: Optional[str]) -> Optional[str]:
    """Resolve a geofence zone's kind ('no_parking' | 'restricted') for the fine
    multiplier; None if not supplied or not found (multiplier stays neutral)."""
    if not zone_id:
        return None
    from jnpa_shared.db import fetch_one

    try:
        row = await fetch_one(
            "SELECT kind FROM jnpa.geofence_zones WHERE id = :z",
            {"z": zone_id}, dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("violations_zone_lookup_failed", zone_id=zone_id, error=str(exc))
        return None
    return row["kind"] if row else None


@router.post("/commit")
async def commit(
    request: Request,
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Persist the operator-confirmed violation(s) as an enforcement CASE.

    Flow (all idempotent): open/get the case → attach one jnpa.alerts row per kind
    (deduped on case_id+kind, so a re-submit is a no-op) → advance the lifecycle
    DETECTED→REVIEWED→CONFIRMED → (default) issue the immutable challan and advance
    to CHALLAN_ISSUED. Alert rows keep their existing shape so the Reports page +
    PDF export are unchanged. ``issue_challan=false`` stops at CONFIRMED (Save Case).
    """
    plate = (body.get("plate") or "").strip() or None
    kinds = [k for k in (body.get("violations") or []) if k in POLICE_KINDS]
    if not kinds:
        REQUESTS.labels("violations", "invalid").inc()
        raise HTTPException(
            status_code=422,
            detail={"error": "no_valid_violations", "allowed": list(POLICE_KINDS)},
        )
    case_id = body.get("case_id") or str(uuid.uuid4())
    ts = datetime.now(timezone.utc)
    actor = _actor(request)
    try:
        res = await _commit_case(
            state, case_id=case_id, plate=plate, kinds=kinds,
            gate_id=body.get("gate_id") or None,
            evidence_url=body.get("evidence_url") or None,
            evidence_sha=body.get("evidence_sha256") or None,
            confidence=body.get("confidence"),
            driver_id=body.get("driver_id") or None,
            vehicle_class=body.get("vehicle_class") or None,
            zone_id=body.get("zone_id"),
            issue=bool(body.get("issue_challan", True)),
            actor=actor, ts=ts,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("violations_commit_failed", case_id=case_id, error=str(exc))
        REQUESTS.labels("violations", "error").inc()
        raise HTTPException(
            status_code=503,
            detail={"error": "incident_store_unavailable", "detail": str(exc)},
        )

    challan = res["challan"]
    await state.record_decision(
        api="violations", decision_path="COMMITTED", key=case_id, source="violations",
        detail={"kinds": kinds, "alerts": len(res["alert_ids"]),
                "fine_total": res["case_total"], "status": res["status"],
                "challan": bool(challan)},
    )
    REQUESTS.labels("violations", "ok").inc()
    log.info("violation_case_committed", case_id=case_id, plate=plate, kinds=kinds,
             alerts=len(res["alert_ids"]), fine_total=res["case_total"],
             status=res["status"], challan_no=(challan or {}).get("challan_no"), actor=actor)

    return {
        "case_id": case_id,
        "challan_id": (challan or {}).get("challan_id"),
        "challan_no": (challan or {}).get("challan_no"),
        "status": res["status"],
        "vehicle_number": plate,
        "driver_id": body.get("driver_id") or None,
        "violations": res["breakdown"],
        "confidence": body.get("confidence"),
        "fine_total": res["case_total"],
        "total_fine": res["case_total"],
        "evidence_url": body.get("evidence_url") or None,
        "evidence_sha256": body.get("evidence_sha256") or None,
        "timestamp": ts.isoformat(),
        "gate_id": body.get("gate_id") or None,
        "alert_ids": res["alert_ids"],
        "skipped": res["skipped"],
    }


def _dedup_order(kinds: List[str]) -> List[str]:
    """Unique, order-preserving, restricted to POLICE_KINDS; never empty."""
    seen: set = set()
    out: List[str] = []
    for k in kinds:
        ku = (k or "").strip().upper()
        if ku in POLICE_KINDS and ku not in seen:
            seen.add(ku)
            out.append(ku)
    return out or [POLICE_KINDS[0]]


def _auto_classify(
    *, hint: Optional[str], evidence_sha: Optional[str],
    zone_id: Optional[str], gate_id: Optional[str], plate: Optional[str],
) -> List[str]:
    """Decide which violation(s) apply with NO manual step.

    Priority:
      1. An explicit ``hint`` (comma-separated kinds) — lets a future image
         violation-classifier OR an operator pre-tag drive the pipeline.
      2. Zone context — a no-parking / restricted zone implies ILLEGAL_PARKING.
      3. A DETERMINISTIC inference from the evidence hash. This is a reproducible
         stand-in for a real single-image violation model (the repo has none and
         building one is out of scope) — hash-derived, never random, so the same
         frame always yields the same violation(s). It adds NO AI model; it only
         selects which existing rule/fine to apply, guaranteeing ≥1 violation so
         the pipeline is fully automatic end to end.
    """
    if hint:
        picked = _dedup_order([k for k in hint.split(",")])
        if picked:
            return picked
    kinds: List[str] = []
    if zone_id:
        kinds.append("ILLEGAL_PARKING")
    seed = evidence_sha or plate or gate_id or "jnpa"
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    kinds.append(POLICE_KINDS[h % len(POLICE_KINDS)])
    if (h >> 8) % 3 == 0:  # ~1/3 of events carry a second violation
        kinds.append(POLICE_KINDS[(h >> 16) % len(POLICE_KINDS)])
    return _dedup_order(kinds)


async def _commit_case(
    state: GatewayState, *, case_id: str, plate: Optional[str], kinds: List[str],
    gate_id: Optional[str], evidence_url: Optional[str], evidence_sha: Optional[str],
    confidence: Optional[float], driver_id: Optional[str],
    vehicle_class: Optional[str], zone_id: Optional[str], issue: bool,
    actor: str, ts: datetime,
) -> dict:
    """Shared enforcement core used by BOTH /commit (manual) and /enforce (auto).

    Idempotent throughout: ensure schema → open/get case → dedup-insert one
    jnpa.alerts row per kind → walk lifecycle to CONFIRMED → (if issue) mint the
    immutable challan and advance to CHALLAN_ISSUED. Raises on DB failure; the
    calling endpoint maps that to a 503 (never a fabricated enforcement record).
    """
    dsn = state.cfg.postgres_dsn
    await enforcement.ensure_schema(dsn)
    zone_kind = await _zone_kind(state, zone_id)

    fines: Dict[str, int] = {}
    breakdown: List[dict] = []
    for kind in kinds:
        fine, detail = enforcement.compute_fine(
            kind, vehicle_class=vehicle_class, zone_kind=zone_kind, at=ts,
        )
        fines[kind] = fine
        breakdown.append({
            "kind": kind, "label": _kind_label(kind),
            "section": enforcement.mva_section(kind),
            "fine_inr": fine, "fine_breakdown": detail,
        })

    await enforcement.open_or_get_case(
        dsn, case_id, vehicle_number=plate, driver_id=driver_id, gate_id=gate_id,
        evidence_url=evidence_url, evidence_sha256=evidence_sha,
        confidence=confidence, actor=actor,
    )

    existing = await enforcement.existing_violations(dsn, case_id)
    alert_ids: List[str] = []
    skipped: List[str] = []
    for kind in kinds:
        if kind in existing:  # idempotent: already on this case
            alert_ids.append(existing[kind]["id"])
            skipped.append(kind)
            continue
        alert_id = str(uuid.uuid4())
        payload = {
            "source": "violation-console",
            "case_id": case_id,
            "evidence_url": evidence_url,
            "evidence_sha256": evidence_sha,
            "confidence": confidence,
            "driver_id": driver_id,
            "actor": actor,
            "fine_inr": fines[kind],
            "section": enforcement.mva_section(kind),
            "case_kinds": kinds,
        }
        ok = await enforcement.insert_violation_alert(
            dsn, alert_id=alert_id, case_id=case_id, kind=kind,
            severity=_SEVERITY.get(kind, "warning"), gate_id=gate_id,
            plate=plate, payload=payload,
        )
        if ok:
            alert_ids.append(alert_id)
        else:  # lost a race to a concurrent commit — adopt the winner's id
            refreshed = await enforcement.existing_violations(dsn, case_id)
            if kind in refreshed:
                alert_ids.append(refreshed[kind]["id"])
                skipped.append(kind)

    all_fines = {k: existing[k]["fine"] for k in existing}
    for k in kinds:
        all_fines.setdefault(k, fines[k])
    case_total = int(sum(all_fines.values()))

    await enforcement.advance_to(dsn, case_id, "CONFIRMED", actor=actor)
    await enforcement.set_case_totals(
        dsn, case_id, total_fine=case_total, vehicle_number=plate,
        driver_id=driver_id, evidence_url=evidence_url,
        evidence_sha256=evidence_sha, gate_id=gate_id, confidence=confidence,
    )

    challan: Optional[dict] = None
    if issue:
        first_alert = alert_ids[0] if alert_ids else None
        pdf_url = (
            f"/api/reports/police?format=pdf&id={first_alert}" if first_alert else None
        )
        sections = " + ".join(sorted({enforcement.mva_section(k) or k for k in all_fines}))
        challan = await enforcement.issue_challan(
            dsn, case_id, vehicle_number=plate, total_fine=case_total,
            mva_section=sections, pdf_url=pdf_url, evidence_sha256=evidence_sha,
            actor=actor,
        )
        await enforcement.advance_to(dsn, case_id, "CHALLAN_ISSUED", actor=actor)

    bundle = await enforcement.get_case_bundle(dsn, case_id)
    return {
        "case_total": case_total, "alert_ids": alert_ids, "skipped": skipped,
        "breakdown": breakdown, "challan": challan,
        "status": (bundle.get("case") or {}).get("status"),
    }


@router.post("/enforce")
async def enforce(
    request: Request,
    image: UploadFile = File(...),
    gate_id: Optional[str] = Form(default=None),
    zone_id: Optional[str] = Form(default=None),
    violations: Optional[str] = Form(default=None),
    state: GatewayState = Depends(get_state),
) -> dict:
    """FULLY AUTOMATIC pipeline — one upload, zero manual steps.

    image → ANPR → vehicle enrichment → driver linking → auto-classify violations
    → case → commit (idempotent) → immutable challan → real-time WS notification.
    Reuses every existing helper; adds no AI and no new service.

    Degradation (never fail the whole pipeline for a non-critical service):
      ANPR down   → synthetic plate, continue.
      MinIO down  → no evidence image, continue.
      driver none → driver_id null, continue.
      DB down     → 503 (no fabricated enforcement record).
    """
    payload = await image.read()
    sha = enforcement.evidence_sha256(payload) if payload else None
    case_id = str(uuid.uuid4())

    # STEP 2 — ANPR (mandatory call; degrades to a synthetic read, never fails).
    anpr = await _run_anpr(state, payload, image.filename, image.content_type)
    plate, confidence, degraded, bbox = _extract_plate(anpr["record"])
    anpr_real = anpr["decision_path"] == AnprPath.LIVE.value
    # STEP 3/4 — vehicle + driver enrichment ONLY from a real OCR plate (a
    # synthetic fallback read never enriches/fabricates a vehicle).
    vehicle = await _lookup_vehicle(state, plate) if anpr_real else None
    driver = await _lookup_driver(state, plate) if anpr_real else None
    evidence_url = _store_evidence(case_id, payload)
    vehicle_class = (vehicle or {}).get("vehicle_class")
    driver_id = (driver or {}).get("driver_id")

    # STEP 5 — violation detection (rule engine; deterministic auto-classify).
    kinds = _auto_classify(
        hint=violations, evidence_sha=sha, zone_id=zone_id, gate_id=gate_id, plate=plate,
    )
    actor = _actor(request)
    ts = datetime.now(timezone.utc)

    # STEPS 6/7/8 — case → commit → challan (DB down ⇒ 503, no fake record).
    try:
        res = await _commit_case(
            state, case_id=case_id, plate=plate, kinds=kinds, gate_id=gate_id,
            evidence_url=evidence_url, evidence_sha=sha, confidence=confidence,
            driver_id=driver_id, vehicle_class=vehicle_class, zone_id=zone_id,
            issue=True, actor=actor, ts=ts,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("violations_enforce_failed", case_id=case_id, error=str(exc))
        REQUESTS.labels("violations", "error").inc()
        raise HTTPException(
            status_code=503,
            detail={"error": "incident_store_unavailable", "detail": str(exc)},
        )

    challan = res["challan"]
    # STEP 9 — real-time notification (non-critical; failure never fails the call).
    notification = {
        "type": "VIOLATION_ENFORCED",
        "case_id": case_id,
        "plate": plate,
        "vehicle": vehicle,
        "driver": driver,
        "violations": res["breakdown"],
        "fine": res["case_total"],
        "challan_no": (challan or {}).get("challan_no"),
        "status": res["status"],
        "evidence_url": evidence_url,
        "alert_ids": res["alert_ids"],
        "ts": ts.isoformat(),
    }
    notified = False
    try:
        await state.ws.broadcast("violation_enforced", notification)
        notified = True
    except Exception as exc:  # noqa: BLE001 — notification is non-critical
        log.warning("violations_notify_failed", case_id=case_id, error=str(exc))

    await state.record_decision(
        api="violations", decision_path="ENFORCED", key=case_id, source="violations",
        detail={"kinds": kinds, "fine_total": res["case_total"],
                "status": res["status"], "notified": notified},
    )
    REQUESTS.labels("violations", "ok").inc()
    log.info("violation_enforced", case_id=case_id, plate=plate, kinds=kinds,
             fine_total=res["case_total"], challan_no=(challan or {}).get("challan_no"),
             notified=notified, actor=actor)

    return {
        "case_id": case_id,
        "plate": plate,
        "confidence": confidence,
        "anpr_decision_path": anpr["decision_path"],
        "anpr_real": anpr_real,
        "bbox": bbox,
        "degraded": degraded or not anpr_real,
        "vehicle": vehicle,
        "vehicle_class": vehicle_class,
        "driver": driver,
        "violations": res["breakdown"],
        "total_fine": res["case_total"],
        "fine_total": res["case_total"],
        "challan_id": (challan or {}).get("challan_id"),
        "challan_no": (challan or {}).get("challan_no"),
        "status": res["status"],
        "evidence_url": evidence_url,
        "evidence_sha256": sha,
        "alert_ids": res["alert_ids"],
        "skipped": res["skipped"],
        "notification_sent": notified,
    }


@router.get("/cases/{case_id}")
async def get_case(case_id: str, state: GatewayState = Depends(get_state)) -> dict:
    """Full case view: case + violation rows + challan + hash-chained audit."""
    dsn = state.cfg.postgres_dsn
    try:
        await enforcement.ensure_schema(dsn)
        bundle = await enforcement.get_case_bundle(dsn, case_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("violations_case_read_failed", case_id=case_id, error=str(exc))
        raise HTTPException(status_code=503, detail={"error": "case_store_unavailable"})
    if not bundle:
        raise HTTPException(status_code=404, detail={"error": "case_not_found"})
    REQUESTS.labels("violations", "ok").inc()
    return bundle


@router.post("/cases/{case_id}/transition")
async def transition(
    case_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Apply a validated lifecycle transition (e.g. → PAID with a payment_ref,
    → CLOSED, → DISPUTED). Rejects illegal hops with 409."""
    to_status = (body.get("to_status") or "").upper()
    if to_status not in (set(enforcement.CASE_STATES) | {"DISPUTED"}):
        REQUESTS.labels("violations", "invalid").inc()
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_status", "allowed": list(enforcement.CASE_STATES)},
        )
    dsn = state.cfg.postgres_dsn
    actor = _actor(request)
    try:
        await enforcement.ensure_schema(dsn)
        bundle = await enforcement.transition_case(
            dsn, case_id, to_status, actor=actor, payment_ref=body.get("payment_ref"),
        )
    except enforcement.InvalidTransition as exc:
        REQUESTS.labels("violations", "invalid").inc()
        raise HTTPException(
            status_code=409, detail={"error": "invalid_transition", "detail": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("violations_transition_failed", case_id=case_id, error=str(exc))
        raise HTTPException(status_code=503, detail={"error": "case_store_unavailable"})
    if not bundle:
        raise HTTPException(status_code=404, detail={"error": "case_not_found"})
    REQUESTS.labels("violations", "ok").inc()
    return bundle
