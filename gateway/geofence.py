"""Production geo-fence enforcement engine — DB-driven (Phase 2 · Track 3).

Fixes the audit's headline geo-fence gap: detection now reads the LIVE
``jnpa.geofence_zones`` the dashboard editor writes (NOT the hardcoded
``jnpa_shared.corridor.NO_PARK_ZONES``). For every vehicle position it runs
point-in-zone, tracks per-vehicle enter/exit state, measures dwell, and persists:

    ENTER / EXIT / DWELL / NO_PARKING_VIOLATION / RESTRICTED_ENTRY
        -> jnpa.geofence_events          (durable event log, this engine writes it)
        -> jnpa.alerts                    (violations, reusing the alerts framework)
        -> jnpa.digital_twin_events       (unified timeline, reused)
        -> jnpa.notifications             (driver warning, reused)

Reuses the framework TABLES directly via jnpa_shared.db — the audit framework
CODE (gateway/audit.py) is untouched. Every write is best-effort: a DB blip must
never break the telemetry pump.

DB polygons are stored GeoJSON-order ``[[lon,lat], ...]``; point_in_polygon wants
a ``(lat,lon)`` ring, so rings are converted on load.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from jnpa_shared.corridor import point_in_polygon

from .logging import get_logger

log = get_logger("gateway.geofence")

_ALERT_NS = uuid.UUID("7c2d3e4f-5a6b-7c8d-9e0f-1a2b3c4d5e6f")
_ZONE_TTL_S = 30.0  # refresh the zone cache at most every 30s

_DDL_EXT = (
    "ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS driver_id text",
    "ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS event_type text",
    "ALTER TABLE jnpa.geofence_events ADD COLUMN IF NOT EXISTS dwell_seconds integer",
)


def _j(v: Any) -> str:
    try:
        return json.dumps(v if v is not None else {}, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


class _Zone:
    __slots__ = ("id", "name", "kind", "ring", "warn_min", "notice_min", "challan_min")

    def __init__(self, row: Dict[str, Any]) -> None:
        self.id = row["id"]
        self.name = row.get("name")
        self.kind = row.get("kind") or "no_parking"
        poly = row.get("polygon") or []
        if isinstance(poly, str):
            try:
                poly = json.loads(poly)
            except Exception:  # noqa: BLE001
                poly = []
        # DB stores [lon,lat]; point_in_polygon wants (lat,lon).
        self.ring: List[Tuple[float, float]] = [(float(p[1]), float(p[0])) for p in poly
                                                if isinstance(p, (list, tuple)) and len(p) >= 2]
        esc = row.get("escalation") or {}
        if isinstance(esc, str):
            try:
                esc = json.loads(esc)
            except Exception:  # noqa: BLE001
                esc = {}
        self.warn_min = float(esc.get("warn_min", 5))
        self.notice_min = float(esc.get("notice_min", 15))
        self.challan_min = float(esc.get("challan_min", 30))

    def contains(self, lat: float, lon: float) -> bool:
        return bool(self.ring) and point_in_polygon(lat, lon, self.ring)


class GeofenceEngine:
    """Stateful, DB-zone-driven geo-fence enforcement."""

    def __init__(self, dsn: Optional[str]) -> None:
        self.dsn = dsn or None
        self._zones: List[_Zone] = []
        self._zones_loaded_at = 0.0
        self._schema_ready = False
        # vehicle_id -> {zone_id -> {"entry": datetime, "violated": bool}}
        self._state: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Optional driver push notifier, wired by GatewayState. Signature:
        #   async (driver_id, vehicle_id, advisory: dict) -> None
        # Default None keeps the engine standalone (tests / no-gw contexts). When
        # set, a violation ALSO fans out to the bound driver over WebPush + FCM
        # (the WS leg is already covered by the alerts pump). Best-effort.
        self._driver_notifier = None

    def set_driver_notifier(self, notifier) -> None:
        """Wire a best-effort per-driver push callback (WebPush + FCM)."""
        self._driver_notifier = notifier

    async def ensure_schema(self) -> None:
        if self._schema_ready or not self.dsn:
            return
        from jnpa_shared.db import execute

        for stmt in _DDL_EXT:
            try:
                await execute(stmt, dsn=self.dsn)
            except Exception as exc:  # noqa: BLE001
                log.debug("geofence_ddl_skipped", error=str(exc))
        self._schema_ready = True

    async def refresh_zones(self, force: bool = False) -> int:
        if not self.dsn:
            return 0
        now = time.monotonic()
        if not force and self._zones and (now - self._zones_loaded_at) < _ZONE_TTL_S:
            return len(self._zones)
        from jnpa_shared.db import fetch_all

        try:
            rows = await fetch_all(
                "SELECT id, name, kind, polygon, escalation FROM jnpa.geofence_zones WHERE enabled = true",
                dsn=self.dsn,
            )
            self._zones = [_Zone(dict(r)) for r in rows]
            self._zones_loaded_at = now
            log.debug("geofence_zones_loaded", zones=len(self._zones))
        except Exception as exc:  # noqa: BLE001
            log.debug("geofence_zones_load_failed", error=str(exc))
        return len(self._zones)

    def zones_snapshot(self) -> List[Dict[str, Any]]:
        return [{"id": z.id, "name": z.name, "kind": z.kind, "points": len(z.ring)}
                for z in self._zones]

    def vehicles_in_zones(self) -> List[Dict[str, Any]]:
        """Current (in-memory) occupancy: which vehicles are inside which zones."""
        out = []
        for vid, zones in self._state.items():
            for zid, st in zones.items():
                out.append({"vehicle_id": vid, "zone_id": zid,
                            "entry_time": st["entry"].isoformat(),
                            "dwell_s": int((datetime.now(timezone.utc) - st["entry"]).total_seconds()),
                            "violated": st.get("violated", False)})
        return out

    async def evaluate_position(self, vehicle_id: str, lat: float, lon: float,
                                driver_id: Optional[str] = None) -> List[dict]:
        """Evaluate one position against the DB zones; persist any transitions.

        Returns the list of events emitted (for the /evaluate response). Cheap
        when nothing changes (point-in-polygon only, no DB write)."""
        if not self.dsn or lat is None or lon is None:
            return []
        await self.ensure_schema()
        await self.refresh_zones()
        now = datetime.now(timezone.utc)
        inside = {z.id: z for z in self._zones if z.contains(float(lat), float(lon))}
        prev = self._state.get(vehicle_id, {})
        emitted: List[dict] = []

        # ENTER: zones newly containing the vehicle.
        for zid, z in inside.items():
            if zid not in prev:
                prev[zid] = {"entry": now, "violated": False}
                await self._event(vehicle_id, driver_id, zid, "ENTER",
                                  entry_time=now, dwell_s=None,
                                  violation_type=None, action="LOGGED")
                emitted.append({"zone_id": zid, "event": "ENTER", "kind": z.kind})
                if z.kind == "restricted":
                    await self._violation(vehicle_id, driver_id, z, now, dwell_s=0,
                                          vtype="RESTRICTED_ENTRY", severity="critical")
                    prev[zid]["violated"] = True
                    emitted.append({"zone_id": zid, "event": "RESTRICTED_ENTRY"})

        # DWELL / NO_PARKING_VIOLATION: still inside a no_parking zone past warn_min.
        for zid, st in list(prev.items()):
            if zid in inside:
                z = inside[zid]
                dwell_s = (now - st["entry"]).total_seconds()
                if z.kind == "no_parking" and not st["violated"] and dwell_s >= z.warn_min * 60:
                    await self._violation(vehicle_id, driver_id, z, now, dwell_s=int(dwell_s),
                                          vtype="NO_PARKING_VIOLATION",
                                          severity="critical" if dwell_s >= z.challan_min * 60 else "warning")
                    st["violated"] = True
                    emitted.append({"zone_id": zid, "event": "NO_PARKING_VIOLATION",
                                    "dwell_s": int(dwell_s)})

        # EXIT: zones the vehicle has left.
        for zid in list(prev.keys()):
            if zid not in inside:
                st = prev.pop(zid)
                dwell_s = int((now - st["entry"]).total_seconds())
                await self._event(vehicle_id, driver_id, zid, "EXIT",
                                  entry_time=st["entry"], exit_time=now, dwell_s=dwell_s,
                                  violation_type=None, action="LOGGED")
                emitted.append({"zone_id": zid, "event": "EXIT", "dwell_s": dwell_s})

        if prev:
            self._state[vehicle_id] = prev
        else:
            self._state.pop(vehicle_id, None)
        return emitted

    # --- persistence (reuses framework tables; best-effort) -----------------
    async def _event(self, vehicle_id, driver_id, zone_id, event_type, *,
                     entry_time=None, exit_time=None, dwell_s=None,
                     violation_type=None, action=None) -> None:
        from jnpa_shared.db import execute

        try:
            await execute(
                """
                INSERT INTO jnpa.geofence_events
                    (vehicle_id, driver_id, zone_id, event_type, entry_time, exit_time,
                     dwell_seconds, violation_type, action_taken)
                VALUES (:v, :d, :z, :et, :entry, :exit, :dw, :vt, :act)
                """,
                {"v": vehicle_id, "d": driver_id, "z": zone_id, "et": event_type,
                 "entry": entry_time, "exit": exit_time, "dw": dwell_s,
                 "vt": violation_type, "act": action},
                dsn=self.dsn,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("geofence_event_write_failed", error=str(exc))

    async def _violation(self, vehicle_id, driver_id, zone: _Zone, ts, *, dwell_s,
                         vtype, severity) -> None:
        """Persist a violation: geofence_events + alert + dt_event + notification."""
        from jnpa_shared.db import execute

        await self._event(vehicle_id, driver_id, zone.id, vtype, entry_time=None,
                          exit_time=None, dwell_s=dwell_s, violation_type=vtype,
                          action="ALERT_RAISED")
        alert_id = str(uuid.uuid5(_ALERT_NS, f"{vehicle_id}|{zone.id}|{vtype}"))
        body = {"source": "geofence-engine", "zone_id": zone.id, "zone_name": zone.name,
                "zone_kind": zone.kind, "vehicle_id": vehicle_id, "driver_id": driver_id,
                "violation_type": vtype, "dwell_seconds": dwell_s}
        # Alert (dedup by deterministic id so a re-fire doesn't duplicate).
        try:
            await execute(
                """
                INSERT INTO jnpa.alerts (id, kind, severity, plate, payload)
                VALUES (CAST(:id AS uuid), :kind, :sev, :plate, CAST(:p AS jsonb))
                ON CONFLICT (id) DO NOTHING
                """,
                {"id": alert_id, "kind": vtype, "sev": severity, "plate": vehicle_id,
                 "p": _j(body)},
                dsn=self.dsn,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("geofence_alert_write_failed", error=str(exc))
        # Unified event timeline.
        try:
            await execute(
                """
                INSERT INTO jnpa.digital_twin_events (event_type, vehicle_id, driver_id, location, payload)
                VALUES ('GEOFENCE_VIOLATION', :v, :d, CAST(:loc AS jsonb), CAST(:p AS jsonb))
                """,
                {"v": vehicle_id, "d": driver_id,
                 "loc": _j({"zone_id": zone.id, "zone_kind": zone.kind}),
                 "p": _j({"alert_id": alert_id, **body})},
                dsn=self.dsn,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("geofence_dt_event_failed", error=str(exc))
        # Driver notification (delivery trail).
        try:
            msg = (f"{'Restricted zone' if vtype == 'RESTRICTED_ENTRY' else 'No-parking'} "
                   f"alert: {zone.name or zone.id}")
            await execute(
                """
                INSERT INTO jnpa.notifications (event_id, channel, receiver, message, delivery_status, provider_response)
                VALUES (:e, 'push', :r, :m, 'SENT', CAST(:p AS jsonb))
                """,
                {"e": alert_id, "r": driver_id or vehicle_id, "m": msg,
                 "p": _j({"kind": vtype, "zone_id": zone.id})},
                dsn=self.dsn,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("geofence_notify_failed", error=str(exc))

        # Best-effort real push to the bound driver (WebPush + FCM) when a
        # notifier is wired and the driver has a registered device. No-op
        # otherwise — the alert still reaches the dashboard over the WS pump.
        if self._driver_notifier is not None:
            restricted = vtype == "RESTRICTED_ENTRY"
            advisory = {
                "type": vtype,
                "alert_id": alert_id,
                "title": "Restricted zone" if restricted else "No-parking violation",
                "body": (f"Leave {zone.name or 'the restricted zone'} immediately."
                         if restricted
                         else f"Move your vehicle from {zone.name or 'the no-parking zone'} within 5 minutes."),
                "category": "emergency",
                "href": "#/zones",
                "zone_id": zone.id,
            }
            try:
                await self._driver_notifier(driver_id, vehicle_id, advisory)
            except Exception as exc:  # noqa: BLE001
                log.debug("geofence_driver_push_skipped", error=str(exc))


__all__ = ["GeofenceEngine"]
