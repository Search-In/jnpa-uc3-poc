"""/api/gates + /api/zones — corridor geometry and geo-fence management.

    GET  /api/gates            -> the 4 JNPA gates (id, name, lat, lon) + live
                                  throughput vs. target so the dashboard can
                                  colour each gate marker.
    GET  /api/corridor         -> the NH-348 corridor polyline + segments (static
                                  geometry from jnpa_shared.corridor) so the map
                                  can draw the 40 km corridor without bundling it.
    GET  /api/zones            -> geo-fence polygons (no-parking / restricted).
    PUT  /api/zones            -> replace the geo-fence set (terra-draw editor
                                  writeback). The anomaly service reads these
                                  live, so the dashboard's edits take effect
                                  without a redeploy.

Zones are stored in ``jnpa.geofence_zones`` (see infra/postgres/init.sql). The
route degrades gracefully if the table is missing on an older volume (returns
the static corridor.NO_PARK_ZONES seed so the editor still has something to
show / edit).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException

from jnpa_shared import corridor

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.geo")

router = APIRouter(tags=["geo"])

# Per-gate target throughput (vehicles/hour) for the marker colour. PoC values
# mirroring JNPA terminal design capacities; the dashboard colours a gate amber
# when the last-hour throughput drops below ``amber`` of target and red below
# ``red`` of target (and also red when it *exceeds* target — congestion).
GATE_TARGETS: Dict[str, int] = {
    "G-NSICT": 220,
    "G-JNPCT": 180,
    "G-NSIGT": 160,
    "G-BMCT": 200,
}


# ----------------------------------------------------------------------- gates
@router.get("/api/gates")
async def gates(state: GatewayState = Depends(get_state)) -> dict:
    """The 4 gates with coords + last-hour throughput vs. target."""
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            "SELECT id, name, lat, lon FROM jnpa.gates ORDER BY id",
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("gates_db_failed", error=str(exc))
        rows = []

    # Last-hour reads per gate (proxy for throughput) from the throughput view.
    throughput: Dict[str, int] = {}
    try:
        tp = await fetch_all(
            """
            SELECT COALESCE(c.gate_id, 'CORRIDOR') AS gate_id, count(*) AS reads
            FROM jnpa.anpr_reads a
            LEFT JOIN jnpa.cameras c ON c.id = a.camera_id
            WHERE a.ts > now() - interval '60 minutes'
            GROUP BY 1
            """,
            dsn=state.cfg.postgres_dsn,
        )
        throughput = {r["gate_id"]: int(r["reads"]) for r in tp}
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("gate_throughput_failed", error=str(exc))

    out: List[dict] = []
    for r in rows:
        gid = r["id"]
        target = GATE_TARGETS.get(gid, 200)
        reads = throughput.get(gid, 0)
        out.append({
            "id": gid,
            "name": r["name"],
            "lat": r["lat"],
            "lon": r["lon"],
            "target_vph": target,
            "throughput_60min": reads,
            "utilisation": round(reads / target, 3) if target else None,
        })
    REQUESTS.labels("gates", "ok").inc()
    return {"gates": out, "count": len(out)}


# -------------------------------------------------------------------- corridor
@router.get("/api/corridor")
async def corridor_geometry() -> dict:
    """Static NH-348 corridor polyline + segments (GeoJSON [lon,lat] order)."""
    line = [[lon, lat] for (lat, lon) in corridor.WAYPOINTS]
    segs = [
        {
            "id": s.id,
            "start": [s.start[1], s.start[0]],
            "end": [s.end[1], s.end[0]],
            "length_km": s.length_km,
        }
        for s in corridor.segments
    ]
    REQUESTS.labels("corridor", "ok").inc()
    return {
        "name": "NH-348 JNPA to Karal Phata",
        "polyline": line,
        "segments": segs,
        "length_km": corridor.total_length_km(),
        "segment_count": len(segs),
    }


# ----------------------------------------------------------------------- zones
def _seed_zones() -> List[dict]:
    """Static fallback zones from corridor.NO_PARK_ZONES (GeoJSON [lon,lat])."""
    out = []
    for z in corridor.NO_PARK_ZONES:
        ring = [[lon, lat] for (lat, lon) in z.polygon]
        # close the ring for a valid GeoJSON polygon
        if ring and ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        out.append({
            "id": z.id,
            "name": z.name,
            "kind": "no_parking",
            "polygon": ring,
            "escalation": {"warn_min": 5, "notice_min": 15, "challan_min": 30},
            "enabled": True,
        })
    return out


@router.get("/api/zones")
async def list_zones(state: GatewayState = Depends(get_state)) -> dict:
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT id, name, kind, polygon, escalation, enabled, updated_at
            FROM jnpa.geofence_zones
            ORDER BY id
            """,
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:
        log.debug("zones_db_unavailable", error=str(exc))
        REQUESTS.labels("zones", "ok").inc()
        return {"source": "seed", "zones": _seed_zones()}

    if not rows:
        REQUESTS.labels("zones", "ok").inc()
        return {"source": "seed", "zones": _seed_zones()}

    out = []
    for r in rows:
        d: Dict[str, Any] = dict(r)
        if isinstance(d.get("updated_at"), datetime):
            d["updated_at"] = d["updated_at"].isoformat()
        out.append(d)
    REQUESTS.labels("zones", "ok").inc()
    return {"source": "db", "zones": out, "count": len(out)}


@router.put("/api/zones")
async def put_zones(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Replace the geo-fence set. Body: ``{"zones": [Zone, ...]}``.

    Each zone: ``{id, name, kind, polygon:[[lon,lat],...], escalation:{...},
    enabled}``. We upsert every supplied zone and delete any DB zone not in the
    payload, so the editor is the source of truth (idempotent PUT semantics).
    """
    from jnpa_shared.db import execute

    zones = body.get("zones")
    if not isinstance(zones, list):
        raise HTTPException(status_code=422, detail={"error": "zones_must_be_a_list"})

    import json

    supplied_ids: List[str] = []
    try:
        for z in zones:
            zid = z.get("id")
            polygon = z.get("polygon")
            name = z.get("name")
            if not zid or not isinstance(polygon, list) or len(polygon) < 3:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "zone_needs_id_name_and_ring", "zone": zid},
                )
            kind = z.get("kind", "no_parking")
            if kind not in ("no_parking", "restricted"):
                kind = "no_parking"
            escalation = z.get("escalation") or {
                "warn_min": 5, "notice_min": 15, "challan_min": 30
            }
            enabled = bool(z.get("enabled", True))
            supplied_ids.append(zid)
            await execute(
                """
                INSERT INTO jnpa.geofence_zones
                    (id, name, kind, polygon, escalation, enabled, updated_at)
                VALUES (:id, :name, :kind, CAST(:polygon AS jsonb),
                        CAST(:escalation AS jsonb), :enabled, now())
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    kind = EXCLUDED.kind,
                    polygon = EXCLUDED.polygon,
                    escalation = EXCLUDED.escalation,
                    enabled = EXCLUDED.enabled,
                    updated_at = now()
                """,
                {
                    "id": zid,
                    "name": name or zid,
                    "kind": kind,
                    "polygon": json.dumps(polygon),
                    "escalation": json.dumps(escalation),
                    "enabled": enabled,
                },
                dsn=state.cfg.postgres_dsn,
            )
        # Delete zones that the editor removed.
        if supplied_ids:
            placeholders = ", ".join(f":id{i}" for i in range(len(supplied_ids)))
            params = {f"id{i}": zid for i, zid in enumerate(supplied_ids)}
            await execute(
                f"DELETE FROM jnpa.geofence_zones WHERE id NOT IN ({placeholders})",
                params,
                dsn=state.cfg.postgres_dsn,
            )
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("zones_put_failed", error=str(exc))
        raise HTTPException(status_code=503,
                            detail={"error": "zones_writeback_failed", "reason": str(exc)})

    REQUESTS.labels("zones", "ok").inc()
    return {"saved": True, "count": len(supplied_ids), "ids": supplied_ids}


# ---------------------------------------------------------------------------
# Geo-fence EVENTS (enter / exit / dwell violation) — durable event log.
# Producers (the anomaly service, or any GPS consumer) POST an enter/exit event
# here; it is persisted to jnpa.geofence_events for audit/analytics. The alerts
# pump also lands geofence-family violations here automatically (gateway/audit).
# ---------------------------------------------------------------------------
@router.post("/api/geo/events")
async def create_geofence_event(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Record a geo-fence event. Body: ``{vehicle_id, zone_id, entry_time?,
    exit_time?, violation_type?, action_taken?}`` (ISO-8601 timestamps)."""
    from .. import audit

    def _ts(v: Any) -> Any:
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None

    zone_id = body.get("zone_id")
    if not zone_id and not body.get("vehicle_id"):
        raise HTTPException(status_code=422,
                            detail={"error": "vehicle_id_or_zone_id_required"})
    await audit.record_geofence_event(
        vehicle_id=body.get("vehicle_id"),
        zone_id=zone_id,
        entry_time=_ts(body.get("entry_time")),
        exit_time=_ts(body.get("exit_time")),
        violation_type=body.get("violation_type") or "ENTER",
        action_taken=body.get("action_taken"),
        dsn=state.cfg.postgres_dsn,
    )
    REQUESTS.labels("geofence_events", "ok").inc()
    return {"recorded": True}


@router.get("/api/geo/events")
async def list_geofence_events(
    limit: int = 100,
    event_type: str | None = None,
    state: GatewayState = Depends(get_state),
) -> dict:
    """Recent geo-fence events (audit/analytics read path), RDS-backed."""
    from jnpa_shared.db import fetch_all

    where = ""
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if event_type:
        where = "WHERE event_type = :et"
        params["et"] = event_type
    try:
        rows = await fetch_all(
            f"""
            SELECT id, vehicle_id, driver_id, zone_id,
                   -- Defensive: never surface a blank event_type even on a legacy
                   -- (pre-migration-0010) volume — derive one from the row.
                   COALESCE(NULLIF(event_type, ''), violation_type,
                            CASE WHEN exit_time IS NOT NULL THEN 'EXIT'
                                 WHEN COALESCE(dwell_seconds, 0) > 0 THEN 'DWELL'
                                 WHEN entry_time IS NOT NULL THEN 'ENTER'
                                 ELSE 'ENTER' END) AS event_type,
                   entry_time, exit_time,
                   dwell_seconds, violation_type, action_taken, created_at
            FROM jnpa.geofence_events
            {where}
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            params,
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("geofence_events_db_unavailable", error=str(exc))
        return {"events": [], "count": 0}
    out = []
    for r in rows:
        d: Dict[str, Any] = dict(r)
        for k in ("entry_time", "exit_time", "created_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        out.append(d)
    REQUESTS.labels("geofence_events", "ok").inc()
    return {"events": out, "count": len(out)}


@router.get("/api/geo/violations")
async def list_geofence_violations(
    limit: int = 100,
    state: GatewayState = Depends(get_state),
) -> dict:
    """Geo-fence violations only (NO_PARKING_VIOLATION / RESTRICTED_ENTRY / DWELL)."""
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT id, vehicle_id, driver_id, zone_id, event_type, dwell_seconds,
                   violation_type, action_taken, created_at
            FROM jnpa.geofence_events
            WHERE violation_type IS NOT NULL
            ORDER BY created_at DESC LIMIT :limit
            """,
            {"limit": max(1, min(int(limit), 1000))},
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        return {"violations": [], "count": 0}
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    REQUESTS.labels("geofence_events", "ok").inc()
    return {"violations": out, "count": len(out)}


@router.post("/api/geo/evaluate")
async def evaluate_position(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Evaluate a vehicle position against the LIVE DB zones (mobile location /
    explicit push). Body: {vehicle_id, lat, lon, driver_id?}. Persists any
    enter/exit/dwell/violation transition and returns the emitted events + the
    zones the point is currently inside."""
    vehicle_id = (body.get("vehicle_id") or "").strip()
    lat, lon = body.get("lat"), body.get("lon")
    if not vehicle_id or lat is None or lon is None:
        raise HTTPException(status_code=422, detail={"error": "vehicle_id_lat_lon_required"})
    try:
        emitted = await state.geofence.evaluate_position(
            vehicle_id, float(lat), float(lon), driver_id=body.get("driver_id")
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail={"error": "geofence_unavailable", "reason": str(exc)})
    # Which zones is the point in right now (for the driver "current zone" view)?
    await state.geofence.refresh_zones()
    inside = [{"id": z.id, "name": z.name, "kind": z.kind}
              for z in state.geofence._zones if z.contains(float(lat), float(lon))]
    REQUESTS.labels("geofence_eval", "ok").inc()
    return {"vehicle_id": vehicle_id, "inside_zones": inside, "events": emitted}


@router.get("/api/geo/vehicles-in-zones")
async def vehicles_in_zones(state: GatewayState = Depends(get_state)) -> dict:
    """Live occupancy: which vehicles are currently inside which zones."""
    rows = state.geofence.vehicles_in_zones()
    REQUESTS.labels("geofence_events", "ok").inc()
    return {"count": len(rows), "vehicles": rows}


@router.get("/api/geo/zones-active")
async def zones_active(state: GatewayState = Depends(get_state)) -> dict:
    """The zones the engine is currently enforcing (loaded from jnpa.geofence_zones)."""
    await state.geofence.refresh_zones()
    snap = state.geofence.zones_snapshot()
    REQUESTS.labels("geofence_events", "ok").inc()
    return {"count": len(snap), "zones": snap, "source": "jnpa.geofence_zones"}
