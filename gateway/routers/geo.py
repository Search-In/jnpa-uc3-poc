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

Zones are stored in ``core.geofence_zone`` (see infra/postgres/init.sql). The
route degrades gracefully if the table is missing on an older volume (returns
the static corridor.NO_PARK_ZONES seed so the editor still has something to
show / edit).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

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
            "SELECT id, name, lat, lon FROM core.gate ORDER BY id",
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
            FROM core.anpr_read a
            LEFT JOIN core.camera c ON c.id = a.camera_id
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
            FROM core.geofence_zone
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
                INSERT INTO core.geofence_zone
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
                f"DELETE FROM core.geofence_zone WHERE id NOT IN ({placeholders})",
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
# here; it is persisted to core.geofence_event for audit/analytics. The alerts
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
            FROM core.geofence_event
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
            FROM core.geofence_event
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
    """The zones the engine is currently enforcing (loaded from core.geofence_zone)."""
    await state.geofence.refresh_zones()
    snap = state.geofence.zones_snapshot()
    REQUESTS.labels("geofence_events", "ok").inc()
    return {"count": len(snap), "zones": snap, "source": "core.geofence_zone"}


# ------------------------------------------------- operator zone notification
# Deterministic namespace for trigger alert ids, so the same occupancy always
# maps to the same id (see the dedup note on the endpoint below).
_ZONE_TRIGGER_NS = uuid.UUID("2f7c9a10-6d5b-4e21-9f38-7c1a5b0d2e64")
_ZONE_TRIGGER_KIND = "ZONE_TRIGGER"


def _alert_type(zone: Dict[str, Any], violated: bool) -> str:
    """Alert type for this occupancy, derived from data that already exists.

    Uses the SAME vocabulary the geofence engine emits for its automatic
    violations, so a triggered alert reads identically to a detected one:
    ``restricted`` zones -> RESTRICTED_ENTRY, a flagged ``no_parking`` dwell ->
    NO_PARKING_VIOLATION, otherwise a plain zone presence. Nothing is invented
    and no operator input is required.
    """
    kind = str(zone.get("kind") or "")
    if kind == "restricted":
        return "RESTRICTED_ENTRY"
    if violated:
        return "NO_PARKING_VIOLATION"
    return "ZONE_PRESENCE"


@router.post("/api/geo/zones/notify")
async def notify_zone_occupancy(
    request: Request,
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Raise ONE operator-triggered notification for a vehicle currently in a zone.

    Manual counterpart to the automatic geo-fence pipeline: the engine is only
    READ here (``vehicles_in_zones()``), never driven — ENTER/EXIT, dwell and
    violation handling are untouched and this endpoint raises nothing on its own.

    Body: ``{"vehicle_id": str, "zone_id": str, "entry_time": str}``. 409 when
    the vehicle has left the zone (``vehicle_not_in_zone``) or re-entered since
    the row was drawn (``occupancy_changed``), so a stale row can never mint a
    notification against a different occupancy.

    Deduplicated on (vehicle, zone, entry_time) via a uuid5 id + the existing
    ``persist_alert`` ``ON CONFLICT (id) DO NOTHING``: clicking Trigger ten times
    yields one alert and one email, while a genuine re-entry (new entry_time) is
    a new, triggerable event.

    Fan-out reuses what already exists and NOTHING else: core.alert (via
    ``persist_alert``) + the ``alert`` WS frame the Notification Center/Bell
    already consume, plus the ADMIN mailer seam (``ADMIN_ALERT_EMAILS``, the
    same list the congestion alert uses).

    The vehicle's OWN driver is notified through the shared dispatcher
    (``notifications.dispatch_alert``) the violations / AI-event consoles already
    use: the paired PWA device is resolved from the plate and the advisory goes
    out over WebPush + FCM. The WS ``alert`` frame is ADDRESSED to that device,
    so it reaches the control room and that ONE driver instead of every connected
    PWA — previously the payload was stamped ``audience: broadcast``, which made
    every driver's app treat one vehicle's zone alert as their own. Drivers are
    still NOT emailed. With no paired device nothing is pushed and the frame stays
    unaddressed, which is the previous behaviour exactly.
    Deliberately writes no core.notification row — the Bell reads alerts, not
    that table, so a delivery row would be new persistence for no consumer.
    """
    from ..provisional import persist_alert
    from jnpa_shared.schemas import Alert

    vehicle_id = str(body.get("vehicle_id") or "").strip()
    zone_id = str(body.get("zone_id") or "").strip()
    entry_time = str(body.get("entry_time") or "").strip()
    if not vehicle_id or not zone_id or not entry_time:
        raise HTTPException(
            status_code=422,
            detail={"error": "vehicle_id_zone_id_and_entry_time_required"})

    # 1) Verify the vehicle is STILL inside that zone, on the SAME occupancy the
    #    operator was looking at (engine read only — nothing is driven here).
    #    Matching entry_time too is what stops a stale row from notifying against
    #    a LATER occupancy: if the vehicle exited and re-entered, the live
    #    entry_time has moved on and this is a different event entirely.
    occupancy = next(
        (r for r in state.geofence.vehicles_in_zones()
         if r["vehicle_id"] == vehicle_id and r["zone_id"] == zone_id),
        None,
    )
    if occupancy is None:
        REQUESTS.labels("geofence_events", "error").inc()
        raise HTTPException(
            status_code=409,
            detail={"error": "vehicle_not_in_zone", "vehicle_id": vehicle_id,
                    "zone_id": zone_id,
                    "hint": "the vehicle has left this zone; refresh the list"},
        )
    if occupancy["entry_time"] != entry_time:
        REQUESTS.labels("geofence_events", "error").inc()
        raise HTTPException(
            status_code=409,
            detail={"error": "occupancy_changed", "vehicle_id": vehicle_id,
                    "zone_id": zone_id, "entry_time": occupancy["entry_time"],
                    "hint": "the vehicle re-entered this zone; refresh the list"},
        )

    zone = next((z for z in state.geofence.zones_snapshot() if z["id"] == zone_id), {})
    dsn = state.cfg.postgres_dsn or None
    principal = getattr(getattr(request, "state", None), "principal", None)
    actor = f"{principal.role}:{principal.sub}" if principal is not None else "operator"

    # Resolve the vehicle's paired PWA device BEFORE the payload is built, so the
    # advisory can be addressed to that driver and both return paths can report
    # the same delivery shape. Best-effort: an unpaired vehicle resolves to None
    # and every driver-facing leg below no-ops.
    device_id: Optional[str] = None
    try:
        from . import push

        device_id = await push.resolve_device(state, vehicle_id=vehicle_id)
    except Exception as exc:  # noqa: BLE001 — resolution must never block the trigger
        log.debug("zone_trigger_device_resolve_failed", vehicle_id=vehicle_id, error=str(exc))

    alert_id = str(uuid.uuid5(
        _ZONE_TRIGGER_NS,
        f"zone-trigger|{vehicle_id}|{zone_id}|{occupancy['entry_time']}",
    ))
    payload = {
        # zone_id is load-bearing: the dashboard's categoryOf() files an alert
        # under the existing "geofence" category on its presence.
        "zone_id": zone_id,
        "zone_name": zone.get("name") or zone_id,
        "zone_kind": zone.get("kind"),
        "vehicle_id": vehicle_id,
        "alert_type": _alert_type(zone, bool(occupancy.get("violated"))),
        "status": "VIOLATION" if occupancy.get("violated") else "OK",
        "entry_time": occupancy["entry_time"],
        "dwell_s": occupancy["dwell_s"],
        "triggered_by": actor,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "source": "geo-zone-trigger",
        # Addressing marker the PWA reads (mobile-pwa/src/lib/addressing.ts):
        # "driver" + device_id = personal, so ONLY that driver's app raises it.
        # Without a paired device we keep the historical unaddressed broadcast so
        # the control room still receives the frame.
        "audience": "driver" if device_id else "broadcast",
    }
    if device_id:
        payload["device_id"] = device_id
    alert = Alert(id=uuid.UUID(alert_id), kind=_ZONE_TRIGGER_KIND, severity="warning",
                  plate=vehicle_id, payload=payload)

    # 2) Already triggered for THIS occupancy? ``persist_alert`` dedups the row
    #    (ON CONFLICT DO NOTHING) but does not report whether it inserted, so a
    #    repeat click would still re-broadcast and re-email. One id lookup makes
    #    the whole endpoint idempotent — which matters because the button's
    #    disabled state is per-browser and is lost on reload or a second operator.
    if dsn:
        try:
            from jnpa_shared.db import fetch_one

            if await fetch_one(
                "SELECT 1 AS x FROM core.alert WHERE id = CAST(:id AS uuid)",
                {"id": alert_id}, dsn=dsn,
            ):
                REQUESTS.labels("geofence_events", "ok").inc()
                return {
                    "alert_id": alert_id, "created": False,
                    "vehicle_id": vehicle_id, "zone_id": zone_id,
                    "entry_time": occupancy["entry_time"],
                    "email": {"attempted": False, "delivered": False},
                    # Already triggered for this occupancy: the driver was pushed
                    # on the first click and is deliberately not pushed again.
                    "driver": {"device_resolved": bool(device_id),
                               "webpush": False, "fcm": False},
                }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — a lookup blip must not block
            log.debug("zone_trigger_dedup_check_failed", alert_id=alert_id, error=str(exc))

    # 3) Persist (dedup by id) — reuses the shared writer, no new SQL.
    if dsn:
        try:
            await persist_alert(alert, dsn=dsn)
        except Exception as exc:  # noqa: BLE001 — best-effort, still fan out on WS
            log.warning("zone_trigger_persist_failed", alert_id=alert_id, error=str(exc))

    # 4) WS broadcast — the frame the Notification Center + Bell already consume.
    #    Addressed when the vehicle has a paired device: ws.broadcast() then
    #    delivers to the control room AND that driver's socket only. device_id=None
    #    keeps the original unaddressed fan-out, so the dashboard is unaffected
    #    either way.
    try:
        await state.ws.broadcast(
            "alert", alert.model_dump(mode="json"), device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("zone_trigger_broadcast_failed", alert_id=alert_id, error=str(exc))

    # 5) ADMIN email only, through the same seam + the same ADMIN_ALERT_EMAILS
    #    list the congestion alert already uses. Drivers are deliberately NOT
    #    mailed — they are reached over WebPush/FCM in step 6 instead. No admin
    #    address configured => nothing is sent and the alert still reaches the
    #    Notification Center.
    from .. import mailer

    recipients = mailer.admin_recipients()
    mail = None
    try:
        mail = await mailer.notify_zone_trigger(alert_id, payload, recipients)
    except Exception as exc:  # noqa: BLE001
        log.warning("zone_trigger_email_failed", alert_id=alert_id, error=str(exc))

    # 6) Push the advisory to THAT driver's device over WebPush + FCM, through the
    #    same dispatcher the violations / AI-event consoles use. ws=False: step 4
    #    already emitted the (addressed) ``alert`` frame and must not duplicate it.
    #    Best-effort — an unpaired vehicle is a no-op and the alert still reaches
    #    the control room.
    driver_push: Optional[Dict[str, bool]] = None
    if device_id:
        try:
            from .. import notifications

            zone_label = payload["zone_name"]
            result = await notifications.dispatch_alert(
                state, device_id,
                kind=_ZONE_TRIGGER_KIND,
                title=("Restricted zone alert"
                       if payload["alert_type"] == "RESTRICTED_ENTRY"
                       else "Zone alert"),
                body=(f"{vehicle_id} is flagged in {zone_label}. "
                      "Please follow the control room's instructions."),
                # NotifyCategory is a closed union in mobile-pwa/src/lib/notify.ts;
                # "compliance" is the member a zone violation belongs to (an
                # unknown value would render an undefined icon).
                category="compliance", href="#/profile",
                extra={"alert_id": alert_id, "zone_id": zone_id,
                       "zone_name": zone_label, "plate": vehicle_id,
                       "alert_type": payload["alert_type"]},
                ws=False,
            )
            driver_push = result.as_dict() if result else None
        except Exception as exc:  # noqa: BLE001 — device push is non-critical
            log.warning("zone_trigger_driver_push_failed",
                        alert_id=alert_id, error=str(exc))

    REQUESTS.labels("geofence_events", "ok").inc()
    return {
        "alert_id": alert_id,
        "created": True,
        "vehicle_id": vehicle_id,
        "zone_id": zone_id,
        "entry_time": occupancy["entry_time"],
        "email": {"attempted": bool(recipients),
                  "delivered": bool(mail and mail.get("delivered"))},
        "driver": {
            "device_resolved": bool(device_id),
            "webpush": bool(driver_push and driver_push.get("webpush")),
            "fcm": bool(driver_push and driver_push.get("fcm")),
        },
    }
