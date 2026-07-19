"""/api/nvr — NVR integration (UC-III completion, Feature 7).

Network-Video-Recorder registry + channel->camera mapping + stream-metadata
catalogue + camera-event generation. The audit found the NVR enterprise system
was declared as an integration seam (gateway/integrations.py) but had no
persistence or operational surface. Adds:
  * NVR device master (recorder host/protocol/channels/location/status),
  * per-channel camera mapping with derived stream metadata (URL/codec/res/fps),
  * a stream-metadata catalogue every viewer/AI pipeline can enumerate,
  * a channel->event generator that resolves the camera and funnels into the
    shared timeline (jnpa.digital_twin_events) + control-room WS.

RDS-backed (jnpa.nvr_devices / jnpa.nvr_camera_map). LIVE-vs-MOCK posture goes
through the same integrations seam as PDP/LDB/RMS-TAS: NVR is LIVE when
NVR_BASE_URL is configured, otherwise MOCK — never a silent hardcode. Stream URLs
are DERIVED METADATA only; this router never opens a real RTSP/ONVIF stream.

    POST /api/nvr/devices                 -> register/upsert an NVR
    GET  /api/nvr/devices                 -> list devices (+ channel counts)
    GET  /api/nvr/devices/{id}            -> one device + its channel mappings
    POST /api/nvr/devices/{id}/channels   -> map a channel (derives stream_url)
    GET  /api/nvr/streams                 -> stream-metadata catalogue
    POST /api/nvr/{id}/event              -> generate a camera event from a channel
    GET  /api/nvr/health                  -> integration health + device counts
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from ..integrations import health as integ_health, system_config
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.nvr")

router = APIRouter(prefix="/api/nvr", tags=["nvr"])

_PROTOCOLS = {"RTSP", "ONVIF", "HTTP"}
_JSONB_KEYS = {"location"}


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize datetimes to ISO strings and decode jsonb text columns."""
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k in _JSONB_KEYS:
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


# --- device registry ---------------------------------------------------------
@router.post("/devices")
async def register_device(body: Dict[str, Any] = Body(...),
                          state: GatewayState = Depends(get_state)) -> dict:
    """Register / upsert an NVR. source = LIVE when NVR_BASE_URL is configured
    (a real recorder is reachable through the integration seam), else CONFIG
    (declared-but-not-live)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning
    dev_id = (body.get("id") or "").strip()
    name = (body.get("name") or "").strip()
    if not dev_id:
        raise HTTPException(400, "id required")
    if not name:
        raise HTTPException(400, "name required")
    protocol = str(body.get("protocol") or "RTSP").upper()
    if protocol not in _PROTOCOLS:
        raise HTTPException(400, f"protocol must be one of {sorted(_PROTOCOLS)}")
    source = "LIVE" if system_config("NVR").configured else "CONFIG"
    row = await execute_returning(
        """INSERT INTO jnpa.nvr_devices
               (id, name, vendor, host, port, protocol, channels, location, source)
           VALUES (:id, :name, :vendor, :host, COALESCE(:port, 554), :protocol,
                   COALESCE(:channels, 0), CAST(:location AS jsonb), :source)
           ON CONFLICT (id) DO UPDATE SET
               name = EXCLUDED.name, vendor = EXCLUDED.vendor, host = EXCLUDED.host,
               port = EXCLUDED.port, protocol = EXCLUDED.protocol,
               channels = EXCLUDED.channels, location = EXCLUDED.location,
               source = EXCLUDED.source, updated_at = now()
           RETURNING *""",
        {"id": dev_id, "name": name, "vendor": body.get("vendor"),
         "host": body.get("host"), "port": body.get("port"), "protocol": protocol,
         "channels": body.get("channels"),
         "location": json.dumps(body.get("location") or {}), "source": source},
        dsn=dsn)
    REQUESTS.labels("nvr", "ok").inc()
    return {"registered": True, "device": _iso(dict(row)) if row else None}


@router.get("/devices")
async def list_devices(state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "devices": []}
    from jnpa_shared.db import fetch_all
    rows = await fetch_all(
        """SELECT d.*,
              (SELECT count(*) FROM jnpa.nvr_camera_map m WHERE m.nvr_id = d.id)
                AS channel_count
           FROM jnpa.nvr_devices d
           ORDER BY d.created_at DESC""",
        {}, dsn=dsn)
    REQUESTS.labels("nvr", "ok").inc()
    return {"count": len(rows), "devices": [_iso(dict(r)) for r in rows]}


@router.get("/streams")
async def list_streams(state: GatewayState = Depends(get_state)) -> dict:
    """Stream-metadata catalogue: every mapped channel joined to its recorder.
    Metadata only — enumerating this never opens a stream."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "streams": []}
    from jnpa_shared.db import fetch_all
    rows = await fetch_all(
        """SELECT m.id, m.nvr_id, d.name AS nvr_name, m.channel, m.camera_id,
                  m.stream_url, m.codec, m.resolution, m.fps, m.status
           FROM jnpa.nvr_camera_map m
           JOIN jnpa.nvr_devices d ON d.id = m.nvr_id
           ORDER BY m.nvr_id, m.channel""",
        {}, dsn=dsn)
    REQUESTS.labels("nvr", "ok").inc()
    return {"count": len(rows), "streams": [_iso(dict(r)) for r in rows]}


@router.get("/health")
async def nvr_health(state: GatewayState = Depends(get_state)) -> dict:
    """Integration health (LIVE/MOCK from the shared seam) merged with the
    device online/offline census so the external dependency is visible."""
    out = dict(integ_health("NVR"))
    dsn = state.cfg.postgres_dsn
    devices = {"total": 0, "online": 0, "offline": 0, "degraded": 0, "unknown": 0}
    if dsn:
        from jnpa_shared.db import fetch_all
        rows = await fetch_all(
            "SELECT status, count(*) AS n FROM jnpa.nvr_devices GROUP BY status",
            {}, dsn=dsn)
        for r in rows:
            n = int(r["n"])
            devices["total"] += n
            devices[str(r["status"]).lower()] = n
    out["devices"] = devices
    REQUESTS.labels("nvr", "ok").inc()
    return out


@router.get("/devices/{device_id}")
async def get_device(device_id: str = Path(...),
                     state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"device": None, "channels": []}
    from jnpa_shared.db import fetch_all, fetch_one
    row = await fetch_one("SELECT * FROM jnpa.nvr_devices WHERE id = :id",
                          {"id": device_id}, dsn=dsn)
    if not row:
        raise HTTPException(404, "nvr_not_found")
    channels = await fetch_all(
        "SELECT * FROM jnpa.nvr_camera_map WHERE nvr_id = :id ORDER BY channel",
        {"id": device_id}, dsn=dsn)
    REQUESTS.labels("nvr", "ok").inc()
    return {"device": _iso(dict(row)),
            "channels": [_iso(dict(c)) for c in channels]}


@router.post("/devices/{device_id}/channels")
async def map_channel(device_id: str = Path(...), body: Dict[str, Any] = Body(...),
                      state: GatewayState = Depends(get_state)) -> dict:
    """Map an NVR channel to a camera. The stream_url is DERIVED from the device
    (protocol://host:port/chN) — metadata only; no stream is opened."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning, fetch_one
    dev = await fetch_one(
        "SELECT id, protocol, host, port FROM jnpa.nvr_devices WHERE id = :id",
        {"id": device_id}, dsn=dsn)
    if not dev:
        raise HTTPException(404, "nvr_not_found")
    channel = body.get("channel")
    if channel is None:
        raise HTTPException(400, "channel required")
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        raise HTTPException(400, "channel must be an integer")
    stream_url = (f"{str(dev['protocol']).lower()}://"
                  f"{dev['host']}:{dev['port']}/ch{channel}")
    row = await execute_returning(
        """INSERT INTO jnpa.nvr_camera_map
               (nvr_id, channel, camera_id, stream_url, codec, resolution, fps)
           VALUES (:nvr_id, :channel, :camera_id, :stream_url,
                   COALESCE(:codec, 'H264'), COALESCE(:resolution, '1920x1080'),
                   COALESCE(:fps, 25))
           ON CONFLICT (nvr_id, channel) DO UPDATE SET
               camera_id = EXCLUDED.camera_id, stream_url = EXCLUDED.stream_url,
               codec = EXCLUDED.codec, resolution = EXCLUDED.resolution,
               fps = EXCLUDED.fps
           RETURNING *""",
        {"nvr_id": device_id, "channel": channel, "camera_id": body.get("camera_id"),
         "stream_url": stream_url, "codec": body.get("codec"),
         "resolution": body.get("resolution"), "fps": body.get("fps")},
        dsn=dsn)
    REQUESTS.labels("nvr", "ok").inc()
    return {"mapped": True, "mapping": _iso(dict(row)) if row else None}


@router.post("/{device_id}/event")
async def generate_event(device_id: str = Path(...), body: Dict[str, Any] = Body(...),
                         state: GatewayState = Depends(get_state)) -> dict:
    """Generate a camera event from an NVR channel. Resolves the mapped camera_id,
    broadcasts a control-room WS 'alert', and persists to jnpa.digital_twin_events
    (source='NVR') so the event lands on the shared operational timeline."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute, fetch_one
    channel = body.get("channel")
    if channel is None:
        raise HTTPException(400, "channel required")
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        raise HTTPException(400, "channel must be an integer")
    event_type = (body.get("event_type") or "").strip().upper()
    if not event_type:
        raise HTTPException(400, "event_type required")
    dev = await fetch_one("SELECT id, name FROM jnpa.nvr_devices WHERE id = :id",
                          {"id": device_id}, dsn=dsn)
    if not dev:
        raise HTTPException(404, "nvr_not_found")
    mapping = await fetch_one(
        "SELECT camera_id, stream_url FROM jnpa.nvr_camera_map WHERE nvr_id = :id AND channel = :ch",
        {"id": device_id, "ch": channel}, dsn=dsn)
    if not mapping:
        raise HTTPException(404, "channel_not_mapped")
    camera_id = mapping["camera_id"]
    plate = body.get("plate")
    detail = body.get("detail")

    payload = {"source": "NVR", "nvr_id": device_id, "nvr_name": dev["name"],
               "channel": channel, "camera_id": camera_id,
               "stream_url": mapping["stream_url"], "plate": plate, "detail": detail,
               "title": f"NVR event: {event_type}",
               "body": detail or f"{event_type} on {dev['name']} ch{channel}"}

    # Control-room WS frame (best-effort — a socket outage never fails the write).
    try:
        await state.ws.broadcast("alert", payload)
    except Exception:  # noqa: BLE001
        pass

    # Shared operational timeline (source stamped in payload — the table has no
    # source column; matches audit.record_event / parking mirrors).
    try:
        await execute(
            """INSERT INTO jnpa.digital_twin_events
                   (event_type, vehicle_id, driver_id, location, payload)
               VALUES (:event_type, :vehicle_id, NULL,
                       CAST(:location AS jsonb), CAST(:payload AS jsonb))""",
            {"event_type": event_type, "vehicle_id": plate,
             "location": json.dumps({"camera_id": camera_id, "nvr_id": device_id,
                                     "channel": channel}),
             "payload": json.dumps(payload, default=str)},
            dsn=dsn)
    except Exception as exc:  # noqa: BLE001
        log.warning("nvr_event_write_failed", event_type=event_type, error=str(exc))

    REQUESTS.labels("nvr", "ok").inc()
    return {"generated": True, "camera_id": camera_id, "nvr_id": device_id,
            "channel": channel, "event_type": event_type, "plate": plate}


__all__ = ["router"]
