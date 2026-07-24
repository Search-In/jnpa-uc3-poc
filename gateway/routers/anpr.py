"""/api/anpr — proxy to ai/anpr with the camera-feed fallback chain.

Per-camera degradation level (spec):

    LIVE       -> ingest/anpr healthy AND < 2 s lag (recent frame on the bus)
    CACHED     -> last 60 s of frames replayed from the Redis Stream
                  (frames.{camera_id}, written by ingest/anpr)
    SYNTHETIC  -> synthetic plate generator (a deterministic plate "read"
                  overlaid on a stock frame) when no live or cached frame exists

The degradation level per camera is surfaced on ``/api/kpi/cameras`` (see the
kpi router) and the chosen rung is recorded as a decision.

``POST /api/anpr/infer`` proxies a multipart image straight to ai/anpr's
``/infer`` (LIVE inference); on upstream failure it degrades to a synthetic read
so the dashboard never goes blank during a demo.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from jnpa_shared import frame_bus
from jnpa_shared.schemas import VehicleClass

from ..fallback import AnprPath, SourceState
from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.anpr")

router = APIRouter(prefix="/api/anpr", tags=["anpr"])

# Demo corridor cameras (mirrors infra/postgres/init.sql seed).
KNOWN_CAMERAS = [
    "CAM-COR-01", "CAM-COR-02", "CAM-COR-03",
    "CAM-COR-04", "CAM-COR-05", "CAM-COR-06",
    "CAM-NSICT-ENT", "CAM-JNPCT-ENT", "CAM-NSIGT-ENT", "CAM-BMCT-ENT",
]

# Deterministic synthetic plates for the SYNTHETIC rung (valid MH series).
_SYNTH_PLATES = [
    "MH04AB1234", "MH43CD5678", "MH12EF9012", "MH01GH3456", "MH02IJ7890",
]


def _latest_frame_age_s(camera_id: str) -> Optional[float]:
    """Age (seconds) of the most recent frame on the bus, or None if absent.

    Best-effort: any frame-bus / Redis error is treated as "no frame" so the
    chain degrades gracefully rather than erroring.
    """
    try:
        consumer = frame_bus.FrameBusConsumer(camera_ids=[camera_id])
        latest = consumer.latest(camera_id)
        consumer.close()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("frame_bus_unavailable", camera_id=camera_id, error=str(exc))
        return None
    if not latest:
        return None
    _entry_id, msg = latest
    ts = msg.ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - ts).total_seconds()


def _synthetic_read(camera_id: str) -> dict:
    """A deterministic synthetic ANPR read (no RNG so demos are reproducible)."""
    h = int.from_bytes(hashlib.sha256(camera_id.encode()).digest()[:4], "big")
    plate = _SYNTH_PLATES[h % len(_SYNTH_PLATES)]
    return {
        "camera_id": camera_id,
        "plate": plate,
        "conf": 0.55,
        "vehicle_class": VehicleClass.HGV.value,
        "degraded": True,
        "engine": "synthetic",
        "weather": "synthetic-overlay",
    }


# Forced camera rung -> the SourceState its Health Card should show.
_ANPR_FORCED_STATE = {
    AnprPath.LIVE.value: SourceState.LIVE,
    AnprPath.CACHED.value: SourceState.DEGRADED,
    AnprPath.SYNTHETIC.value: SourceState.DOWN,
}


def camera_state(state: GatewayState, camera_id: str) -> dict:
    """Resolve the camera-feed rung for one camera (LIVE / CACHED / SYNTHETIC)."""
    cfg = state.cfg
    age = _latest_frame_age_s(camera_id)
    # Presenter fault injection: a forced rung short-circuits the real cascade.
    forced = state.faults.forced("camera")
    if forced is not None:
        path = AnprPath(forced)
        src_state = _ANPR_FORCED_STATE[forced]
    elif age is not None and age < cfg.anpr_lag_threshold_s:
        path, src_state = AnprPath.LIVE, SourceState.LIVE
    elif age is not None and age <= cfg.cache_ttl_anpr_s:
        path, src_state = AnprPath.CACHED, SourceState.DEGRADED
    else:
        path, src_state = AnprPath.SYNTHETIC, SourceState.DOWN
    return {
        "camera_id": camera_id,
        "decision_path": path.value,
        "frame_age_s": round(age, 2) if age is not None else None,
        "forced": forced is not None,
        "_state": src_state,
    }


@router.get("/cameras")
async def cameras(state: GatewayState = Depends(get_state)) -> dict:
    """Per-camera degradation level for the dashboard (LIVE/CACHED/SYNTHETIC)."""
    rows = []
    for cam in KNOWN_CAMERAS:
        cs = camera_state(state, cam)
        state.observe_source(
            f"anpr:{cam}", state=cs["_state"], decision_path=cs["decision_path"],
            ok=cs["decision_path"] == AnprPath.LIVE.value,
        )
        rows.append({k: v for k, v in cs.items() if not k.startswith("_")})
    return {"cameras": rows}


def _mean(values: list) -> Optional[float]:
    nums = [v for v in values if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), 4) if nums else None


def _normalize_anpr_eval(data: dict) -> dict:
    """Map ai/anpr's held-out OCR benchmark into the evaluator-facing shape
    (model_name / accuracy / precision / recall / ocr_confidence /
    dataset_breakdown / data_mode) WITHOUT fabricating any value.

    Every returned number is derived from a real measured field; the semantics of
    each (OCR eval is not a binary classifier, so precision/recall are defined
    explicitly) are documented in ``metric_definitions``. All upstream keys are
    preserved so the existing DemoConsole probe keeps working.
    """
    from jnpa_shared.config import get_settings

    slices = data.get("slices") or []
    detection = data.get("detection") or {}
    combined = data.get("combined_weighted_accuracy_pct")
    engine = data.get("engine")
    degraded = bool(data.get("degraded"))

    breakdown = []
    det_recalls = []
    clean_exact = None
    for s in slices:
        name = s.get("name")
        det = detection.get(name, {}) if isinstance(detection, dict) else {}
        dr = det.get("detection_recall@0.3iou")
        if dr is not None:
            det_recalls.append(dr)
        if name in ("clean", "clear"):
            clean_exact = s.get("exact_match")
        breakdown.append({
            "condition": name,
            "n": s.get("n"),
            "exact_match": s.get("exact_match"),
            "char_accuracy": s.get("char_accuracy"),
            "detection_recall": dr,
        })

    accuracy = combined / 100.0 if isinstance(combined, (int, float)) else None
    # `precision` = clean-condition exact-match (best-case read precision);
    # `recall` = mean plate detection recall; both are real measured fields.
    precision = clean_exact if clean_exact is not None else accuracy
    recall = _mean(det_recalls)
    ocr_confidence = _mean([s.get("char_accuracy") for s in slices])

    # data_mode reflects the whole-twin data posture; metrics themselves are also
    # tagged honest via `degraded`/`engine` (fallback OCR reports its true lower
    # numbers, never the 95% target).
    data_mode = get_settings().data_mode

    out = dict(data)  # preserve every upstream key (backward compatible)
    out.update({
        "model_name": (
            "YOLOv8n + PaddleOCR PP-OCRv4"
            if engine == "paddle+yolo"
            else "deterministic-fallback-OCR"
        ),
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "precision": round(precision, 4) if isinstance(precision, (int, float)) else None,
        "recall": recall,
        "ocr_confidence": ocr_confidence,
        "dataset_breakdown": breakdown,
        "data_mode": data_mode,
        "metrics_synthetic": degraded or engine != "paddle+yolo",
        "metric_definitions": {
            "accuracy": "combined weighted plate exact-match across all conditions",
            "precision": "clean-condition plate exact-match rate (read precision)",
            "recall": "mean plate detection recall @0.3 IoU",
            "ocr_confidence": "mean character-level accuracy across conditions",
        },
    })
    return out


@router.get("/eval")
async def eval_metrics(state: GatewayState = Depends(get_state)) -> dict:
    """Proxy ai/anpr's held-out OCR benchmark (``GET /eval``) and normalize it into
    the evaluator-facing shape the dashboard model-performance card renders
    (model_name / accuracy / precision / recall / ocr_confidence /
    dataset_breakdown / data_mode). All upstream keys are preserved so the
    DemoConsole realism probe (web/src/data/live.ts:ocrEval) keeps working.

    503 when the AI service is unreachable — the dashboard degrades that to a
    static target note.
    """
    url = state.cfg.anpr_ai_url.rstrip("/") + "/eval"
    t0 = time.perf_counter()
    try:
        # The held-out OCR benchmark runs the model over the eval set, so it is
        # inherently slow (seconds-to-minutes on a CPU-only host). Allow a
        # generous budget (override with GATEWAY_ANPR_EVAL_TIMEOUT_S) rather than
        # 503-ing a working-but-slow eval.
        import os as _os
        _eval_timeout = float(_os.environ.get("GATEWAY_ANPR_EVAL_TIMEOUT_S", "60"))
        resp = await state.http.get(url, timeout=_eval_timeout)
        UPSTREAM_LATENCY.labels("anpr", "anpr-ai").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            REQUESTS.labels("anpr", "ok").inc()
            return _normalize_anpr_eval(resp.json())
        log.info("anpr_eval_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("anpr_eval_unreachable", url=url, error=str(exc))
    raise HTTPException(status_code=503, detail={"error": "anpr_eval_unavailable"})


@router.post("/infer")
async def infer(
    image: UploadFile = File(...), state: GatewayState = Depends(get_state)
) -> dict:
    """Proxy a multipart image to ai/anpr /infer (LIVE), degrade to SYNTHETIC."""
    cfg = state.cfg
    url = cfg.anpr_ai_url.rstrip("/") + "/infer"
    payload = await image.read()
    t0 = time.perf_counter()
    try:
        resp = await state.http.post(
            url, files={"image": (image.filename or "frame.jpg", payload, image.content_type or "image/jpeg")},
        )
        UPSTREAM_LATENCY.labels("anpr", "anpr-ai").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            data = resp.json()
            await state.record_decision(
                api="anpr", decision_path=AnprPath.LIVE.value,
                latency_ms=(time.perf_counter() - t0) * 1000, source="anpr-ai",
            )
            REQUESTS.labels("anpr", "ok").inc()
            return {"decision_path": AnprPath.LIVE.value, "record": data}
        log.info("anpr_infer_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("anpr_infer_unreachable", url=url, error=str(exc))

    # Degrade: synthetic read so the demo never goes blank.
    synth = _synthetic_read("CAM-UPLOAD")
    await state.record_decision(
        api="anpr", decision_path=AnprPath.SYNTHETIC.value,
        source="anpr-ai", source_state=SourceState.DOWN, ok=False,
    )
    REQUESTS.labels("anpr", "ok").inc()
    return {"decision_path": AnprPath.SYNTHETIC.value, "record": synth}


@router.get("/read/{camera_id}")
async def read_camera(camera_id: str, state: GatewayState = Depends(get_state)) -> dict:
    """Resolve the current ANPR read for a camera through the fallback chain.

    LIVE/CACHED return the most recent persisted read; SYNTHETIC returns a
    deterministic generated read. The decision is recorded for the demo.
    """
    cs = camera_state(state, camera_id)
    path = cs["decision_path"]

    record: Optional[dict] = None
    if path in (AnprPath.LIVE.value, AnprPath.CACHED.value):
        record = await _latest_db_read(state, camera_id)
    if record is None:
        # No persisted read available -> synthesize (even if a frame existed).
        path = AnprPath.SYNTHETIC.value
        cs["_state"] = SourceState.DOWN
        record = _synthetic_read(camera_id)

    await state.record_decision(
        api="anpr", key=camera_id, decision_path=path, source=f"anpr:{camera_id}",
        source_state=cs["_state"], ok=path == AnprPath.LIVE.value,
        detail={"frame_age_s": cs["frame_age_s"]},
    )
    REQUESTS.labels("anpr", "ok").inc()
    return {"camera_id": camera_id, "decision_path": path, "record": record,
            "frame_age_s": cs["frame_age_s"]}


async def _latest_db_read(state: GatewayState, camera_id: str) -> Optional[dict]:
    from jnpa_shared.db import fetch_one
    try:
        row = await fetch_one(
            """
            SELECT ts, camera_id, plate, conf, vehicle_class, image_url, weather, degraded
            FROM core.anpr_read
            WHERE camera_id = :cam
            ORDER BY ts DESC
            LIMIT 1
            """,
            {"cam": camera_id}, dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("anpr_db_read_failed", camera_id=camera_id, error=str(exc))
        return None
    if not row:
        return None
    out = dict(row)
    if isinstance(out.get("ts"), datetime):
        out["ts"] = out["ts"].isoformat()
    return out
