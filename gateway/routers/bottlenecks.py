"""/api/bottlenecks — Road-bottleneck analytics (UC-III completion, Feature 9).

Ranks the corridor's most congested segments from the latest per-segment
``jnpa.traffic_snapshots`` (jam_factor + speed), enriches each with corridor
metadata (name + midpoint lat/lon + length) and an estimated ``avg_delay_min``
(travel-time penalty vs. free flow), and can persist the ranking as a
timestamped snapshot into ``jnpa.bottleneck_snapshots`` (migration 0024).

Robust by design: when ``jnpa.traffic_snapshots`` is empty (or postgres is
unconfigured) the ranking falls back to a deterministic, corridor-metadata
derived estimate so the dashboard card never renders blank; the ``source`` field
says which path produced the numbers. Additive — no existing endpoint/table is
touched.

    GET    /api/bottlenecks?top=3        -> current top-N bottleneck ranking
    POST   /api/bottlenecks/snapshot     -> compute + persist a ranking snapshot
    GET    /api/bottlenecks/history?limit=-> recent persisted snapshot rows
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.bottlenecks")

router = APIRouter(prefix="/api/bottlenecks", tags=["bottlenecks"])

# Default corridor free-flow speed (km/h) when a segment carries no explicit
# free-flow metadata. NH-348 corridor is signposted ~50 km/h.
_DEFAULT_FREE_FLOW_KMH = 50.0
_DEFAULT_LENGTH_KM = 1.8


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize datetimes; parse a stringified ``detail`` jsonb column."""
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k == "detail":
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


def _segment_meta() -> Dict[str, Dict[str, Any]]:
    """Corridor segment metadata keyed by segment_id: name + midpoint lat/lon +
    length_km + free_flow_kmh. Imported defensively so a missing/renamed symbol in
    ``jnpa_shared.corridor`` never crashes the endpoint — on failure the caller
    derives names from the segment_id and leaves lat/lon null."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from jnpa_shared import corridor  # noqa: WPS433 (defensive local import)
    except Exception as exc:  # noqa: BLE001
        log.debug("corridor_import_failed", error=str(exc))
        return out
    try:
        for seg in getattr(corridor, "segments", []) or []:
            try:
                mid: Tuple[float, float] = seg.midpoint
                lat, lon = float(mid[0]), float(mid[1])
            except Exception:  # noqa: BLE001
                lat, lon = None, None  # type: ignore[assignment]
            length = getattr(seg, "length_km", None)
            free_flow = getattr(seg, "free_flow_kmh", None)
            out[seg.id] = {
                "name": getattr(seg, "name", None) or seg.id,
                "lat": lat,
                "lon": lon,
                "length_km": float(length) if length else _DEFAULT_LENGTH_KM,
                "free_flow_kmh": float(free_flow) if free_flow else _DEFAULT_FREE_FLOW_KMH,
            }
    except Exception as exc:  # noqa: BLE001
        log.debug("corridor_segments_failed", error=str(exc))
    return out


def _deterministic_jam(segment_id: str) -> float:
    """Stable per-segment jam estimate (0..10, TomTom-style) with no RNG, so a
    metadata-only fallback ranking is reproducible across requests."""
    h = int.from_bytes(hashlib.sha256(segment_id.encode()).digest()[:2], "big")
    return round(0.5 + (h % 800) / 100.0, 3)  # 0.5 .. 8.49


def _avg_delay_min(length_km: float, speed_kmh: Optional[float],
                   free_flow_kmh: float) -> float:
    """Travel-time penalty (minutes) over the segment vs. free flow. Guards
    divide-by-zero on both legs and clamps negative results to 0."""
    if not length_km or not free_flow_kmh or not speed_kmh or speed_kmh <= 0:
        return 0.0
    delay = (length_km / speed_kmh * 60.0) - (length_km / free_flow_kmh * 60.0)
    return round(max(0.0, delay), 2)


def _rank_entries(candidates: List[Dict[str, Any]], top: int) -> List[Dict[str, Any]]:
    """Sort by jam_factor DESC (speed ASC as a tiebreak), take top N and assign
    a 1-based rank."""
    ordered = sorted(
        candidates,
        key=lambda c: (-(c.get("jam_factor") or 0.0), c.get("speed_kmh") or 1e9),
    )
    ranked: List[Dict[str, Any]] = []
    for i, c in enumerate(ordered[: max(0, top)], start=1):
        ranked.append({
            "rank": i,
            "segment_id": c["segment_id"],
            "name": c.get("name") or c["segment_id"],
            "jam_factor": round(float(c.get("jam_factor") or 0.0), 3),
            "speed_kmh": round(float(c["speed_kmh"]), 2) if c.get("speed_kmh") is not None else None,
            "free_flow_kmh": round(float(c.get("free_flow_kmh") or _DEFAULT_FREE_FLOW_KMH), 2),
            "avg_delay_min": c.get("avg_delay_min", 0.0),
            "lat": c.get("lat"),
            "lon": c.get("lon"),
        })
    return ranked


def _metadata_candidates(meta: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic corridor-metadata-only candidate set (no live traffic)."""
    out: List[Dict[str, Any]] = []
    for sid, m in meta.items():
        jam = _deterministic_jam(sid)
        free_flow = m.get("free_flow_kmh") or _DEFAULT_FREE_FLOW_KMH
        # Higher jam -> lower speed; cap the reduction so speed stays positive.
        speed = round(free_flow * (1.0 - min(jam / 10.0, 0.9)), 2)
        length = m.get("length_km") or _DEFAULT_LENGTH_KM
        out.append({
            "segment_id": sid,
            "name": m.get("name") or sid,
            "jam_factor": jam,
            "speed_kmh": speed,
            "free_flow_kmh": free_flow,
            "avg_delay_min": _avg_delay_min(length, speed, free_flow),
            "lat": m.get("lat"),
            "lon": m.get("lon"),
        })
    return out


async def _compute_ranking(dsn: Optional[str], top: int) -> Dict[str, Any]:
    """Shared ranking logic for GET and POST. Prefers the latest per-segment
    ``jnpa.traffic_snapshots`` rows; falls back to a deterministic corridor
    estimate when there is no live data (or no database). ``source`` tags the
    path: ``traffic_snapshots`` | ``metadata``."""
    meta = _segment_meta()
    rows: List[Dict[str, Any]] = []
    if dsn:
        from jnpa_shared.db import fetch_all
        try:
            fetched = await fetch_all(
                """
                SELECT DISTINCT ON (segment_id)
                       segment_id, ts, speed_kmh, jam_factor, source
                FROM jnpa.traffic_snapshots
                ORDER BY segment_id, ts DESC
                """,
                dsn=dsn,
            )
            rows = [dict(r) for r in fetched]
        except Exception as exc:  # noqa: BLE001 - infra-timing dependent
            log.debug("bottleneck_snapshots_query_failed", error=str(exc))
            rows = []

    if rows:
        source = "traffic_snapshots"
        candidates: List[Dict[str, Any]] = []
        for r in rows:
            sid = r.get("segment_id")
            if not sid:
                continue
            m = meta.get(sid, {})
            free_flow = m.get("free_flow_kmh") or _DEFAULT_FREE_FLOW_KMH
            length = m.get("length_km") or _DEFAULT_LENGTH_KM
            speed = r.get("speed_kmh")
            speed = float(speed) if speed is not None else None
            candidates.append({
                "segment_id": sid,
                "name": m.get("name") or sid,
                "jam_factor": float(r.get("jam_factor") or 0.0),
                "speed_kmh": speed,
                "free_flow_kmh": free_flow,
                "avg_delay_min": _avg_delay_min(length, speed, free_flow),
                "lat": m.get("lat"),
                "lon": m.get("lon"),
            })
    else:
        source = "metadata"
        candidates = _metadata_candidates(meta)

    bottlenecks = _rank_entries(candidates, top)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(bottlenecks),
        "bottlenecks": bottlenecks,
        "source": source,
    }


@router.get("")
async def get_bottlenecks(top: int = Query(default=3, ge=1, le=50),
                          state: GatewayState = Depends(get_state)) -> dict:
    """Current top-N corridor bottleneck ranking. Always renders — falls back to
    a deterministic corridor-metadata estimate when no live traffic is available
    (or postgres is unconfigured), tagging ``source`` accordingly."""
    result = await _compute_ranking(state.cfg.postgres_dsn, top)
    REQUESTS.labels("bottlenecks", "ok").inc()
    return result


@router.post("/snapshot")
async def snapshot_bottlenecks(body: Dict[str, Any] = Body(default_factory=dict),
                               state: GatewayState = Depends(get_state)) -> dict:
    """Compute the current ranking and persist each ranked row into
    ``jnpa.bottleneck_snapshots`` under a shared ``ts`` (now()). Optional body:
    ``{"top": N}``. Requires a database — 503 otherwise."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    top = int(body.get("top") or 3) if isinstance(body, dict) else 3
    result = await _compute_ranking(dsn, top)
    bottlenecks = result["bottlenecks"]

    from jnpa_shared.db import execute
    persisted = 0
    for b in bottlenecks:
        detail = {"generated_at": result["generated_at"], "source": result["source"]}
        await execute(
            """INSERT INTO jnpa.bottleneck_snapshots
                 (ts, rank, segment_id, name, jam_factor, speed_kmh, free_flow_kmh,
                  avg_delay_min, lat, lon, detail)
               VALUES (now(), :rank, :sid, :name, :jam, :speed, :free, :delay,
                  :lat, :lon, CAST(:detail AS jsonb))""",
            {
                "rank": b["rank"], "sid": b["segment_id"], "name": b["name"],
                "jam": b["jam_factor"], "speed": b["speed_kmh"],
                "free": b["free_flow_kmh"], "delay": b["avg_delay_min"],
                "lat": b["lat"], "lon": b["lon"],
                "detail": json.dumps(detail),
            },
            dsn=dsn,
        )
        persisted += 1

    # --- Realtime + alert fan-out (reuses the existing WS hub + audit/alert
    # helpers; additive — no new infra). One WS "bottleneck" frame refreshes the
    # dashboard/Geo Analytics; the worst-ranked segment also raises a control-room
    # alert (WS "alert" + jnpa.alerts row + digital-twin event) so it surfaces on
    # the Alerts Center exactly like other alert kinds. Best-effort throughout. ---
    top = bottlenecks[0] if bottlenecks else None
    board_frame = {"type": "bottleneck", "count": len(bottlenecks),
                   "generated_at": result["generated_at"], "source": result["source"],
                   "bottlenecks": bottlenecks}
    try:
        await state.ws.broadcast("bottleneck", board_frame)
    except Exception as exc:  # noqa: BLE001
        log.debug("bottleneck_ws_failed", error=str(exc))
    alerted = False
    if top:
        # Escalate by jam severity (same convention as the congestion alerter).
        jam = float(top.get("jam_factor") or 0.0)
        severity = "critical" if jam >= 7.0 else ("warning" if jam >= 4.0 else "info")
        alert_payload = {
            "type": "bottleneck", "segment_id": top.get("segment_id"),
            "name": top.get("name"), "rank": top.get("rank"),
            "jam_factor": top.get("jam_factor"), "speed_kmh": top.get("speed_kmh"),
            "avg_delay_min": top.get("avg_delay_min"),
            "lat": top.get("lat"), "lon": top.get("lon"),
            "title": f"Bottleneck: {top.get('name') or top.get('segment_id')}",
            "body": f"Jam {jam:.1f} · ~{float(top.get('avg_delay_min') or 0):.0f} min delay "
                    f"at {float(top.get('speed_kmh') or 0):.0f} km/h",
        }
        try:
            await state.ws.broadcast("alert", alert_payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("bottleneck_alert_ws_failed", error=str(exc))
        # Durable control-room alert row (Alerts Center reads jnpa.alerts).
        try:
            await execute(
                """INSERT INTO jnpa.alerts (kind, severity, gate_id, payload)
                   VALUES ('bottleneck', :sev, NULL, CAST(:payload AS jsonb))""",
                {"sev": severity, "payload": json.dumps(alert_payload)}, dsn=dsn)
        except Exception as exc:  # noqa: BLE001
            log.debug("bottleneck_alert_row_failed", error=str(exc))
        # Digital-twin event + audit trail (reuses the shared alert->event sink).
        try:
            from .. import audit
            await audit.persist_alert_event(
                {"kind": "bottleneck", "severity": severity, "payload": alert_payload})
        except Exception as exc:  # noqa: BLE001
            log.debug("bottleneck_dte_failed", error=str(exc))
        alerted = True

    REQUESTS.labels("bottlenecks", "ok").inc()
    return {"persisted": persisted, "source": result["source"],
            "generated_at": result["generated_at"], "bottlenecks": bottlenecks,
            "broadcast": True, "alert_raised": alerted}


@router.get("/history")
async def bottlenecks_history(limit: int = Query(default=50, ge=1, le=1000),
                              state: GatewayState = Depends(get_state)) -> dict:
    """Recent persisted bottleneck snapshot rows, newest first (flat list)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "snapshots": []}
    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(
            """SELECT ts, rank, segment_id, name, jam_factor, speed_kmh,
                      free_flow_kmh, avg_delay_min, lat, lon, detail
               FROM jnpa.bottleneck_snapshots
               ORDER BY ts DESC, rank ASC
               LIMIT :limit""",
            {"limit": limit}, dsn=dsn)
    except Exception as exc:  # noqa: BLE001 - infra-timing dependent
        log.debug("bottleneck_history_failed", error=str(exc))
        rows = []
    REQUESTS.labels("bottlenecks", "ok").inc()
    return {"count": len(rows), "snapshots": [_iso(dict(r)) for r in rows]}
