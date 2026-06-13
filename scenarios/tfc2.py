"""TFC-2 — Wrong-Way Detection at the NH Junction.

Params: {camera_id: "C-KARAL-EXIT"}

Reactive chain:
  1. Inject a synthetic wrong-way GPS track to Kafka ``truck.telemetry`` (the
     anomaly service's TelemetryWorker picks it up). The track heads ~315°
     against the ~135° with-traffic corridor bearing for >2 s (the wrong-way
     hold window), at the Karal Phata junction.
  2. The anomaly service emits a WRONG_WAY alert (with an evidence URL when a
     frame is on the bus). We poll /alerts/recent for it.
  3. The gateway e-Challan stub /api/echallan/issue resolves the plate through
     the Vahan adapter chain (shows the fallback rung) and returns a fake
     echallan_id + PDF url.
  4. We stamp echallan_id + echallan_pdf_url back onto the alert payload.
  5. (Dashboard) plays the last-10s evidence MP4 from the frame bus in the alert
     drawer — we attach an ``evidence_mp4_url`` the dashboard renders.

reset(): mark the synthetic alert resolved, remove injected telemetry tag.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from jnpa_shared import kafka_io, tracing
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import TOPIC_TELEMETRY

from .base import Upstreams
from .config import ScenarioConfig
from .handle import ScenarioHandle, new_handle_id

log = get_logger("scenarios.tfc2")

NAME = "tfc2"
# Karal Phata junction (corridor end). A track going wrong-way exits north-west
# (~315°) against the ~135° downstream bearing -> 180° divergence.
KARAL = (18.7800, 73.0800)
WRONG_WAY_HEADING = 315.0
SYNTH_PLATE = "MH04WW1234"
PING_COUNT = 6
PING_DT_S = 0.6  # 6 pings * 0.6s ~= 3s span > the 2s wrong-way hold window


async def run(params: Dict[str, Any]) -> ScenarioHandle:
    cfg = ScenarioConfig.from_env()
    camera_id = params.get("camera_id", "C-KARAL-EXIT")
    h = ScenarioHandle(handle_id=new_handle_id(NAME), name=NAME,
                       params={"camera_id": camera_id}, cfg=cfg)
    up = Upstreams(cfg)
    device_id = f"SYN-TFC2-{h.handle_id}"
    h.cleanup = {"plate": SYNTH_PLATE, "device_id": device_id}

    with tracing.span("scenario.tfc2.run", {"camera_id": camera_id, "handle": h.handle_id}):
        await h.create_row()
        try:
            # --- Step 1: inject the wrong-way track to truck.telemetry ---
            published = _publish_wrongway_track(cfg, device_id, camera_id)
            await h.step(
                f"Injected wrong-way track at {camera_id} ({published} pings)",
                trigger="kafka:truck.telemetry",
                detail={"device_id": device_id, "plate": SYNTH_PLATE,
                        "heading_deg": WRONG_WAY_HEADING, "camera_id": camera_id},
            )

            # --- Step 2: anomaly emits WRONG_WAY ---
            alert = await _await_alert(up, plate=SYNTH_PLATE, device_id=device_id)
            await h.step(
                "Anomaly service emitted WRONG_WAY alert" + ("" if alert else " (not observed yet)"),
                trigger="anomaly:/alerts/recent",
                status="ok" if alert else "degraded",
                detail={"alert_id": alert.get("id") if alert else None,
                        "evidence_url": (alert or {}).get("payload", {}).get("evidence_url")},
            )
            h.cleanup["alert_id"] = (alert or {}).get("id")

            # --- Step 3: e-Challan via gateway (Vahan fallback chain) ---
            challan = await up.gw_post("/api/echallan/issue",
                                       {"plate": SYNTH_PLATE, "kind": "WRONG_WAY"})
            await h.step(
                f"e-Challan issued ({(challan or {}).get('echallan_id', 'n/a')}) — "
                f"plate resolved via Vahan {(challan or {}).get('vahan_decision_path', '?')}",
                trigger="gateway:/api/echallan/issue",
                status="ok" if challan else "degraded",
                detail=challan or {},
            )

            # --- Step 4: stamp echallan onto the alert payload ---
            mp4_url = None
            if alert and challan:
                mp4_url = _evidence_mp4_url(cfg, camera_id)
                await _enrich_alert(cfg, alert["id"], {
                    "echallan_id": challan.get("echallan_id"),
                    "echallan_pdf_url": challan.get("echallan_pdf_url"),
                    "evidence_mp4_url": mp4_url,
                    "scenario": h.handle_id,
                })
            await h.step(
                "Alert payload updated with echallan_id + echallan_pdf_url",
                trigger="scenario.tfc2",
                status="ok" if (alert and challan) else "degraded",
                detail={"echallan_id": (challan or {}).get("echallan_id"),
                        "echallan_pdf_url": (challan or {}).get("echallan_pdf_url")},
            )

            # --- Step 5: evidence MP4 for the dashboard drawer ---
            await h.step(
                "Evidence clip (last 10 s) available for the alert drawer",
                trigger="frame-bus",
                detail={"evidence_mp4_url": mp4_url or _evidence_mp4_url(cfg, camera_id),
                        "camera_id": camera_id},
            )

            await h.finish("DONE")
        except Exception as exc:  # noqa: BLE001
            log.warning("tfc2_failed", handle=h.handle_id, error=str(exc))
            await h.step("Scenario error", trigger="scenario.tfc2", status="failed",
                         detail={"error": str(exc)})
            await h.finish("FAILED")
        finally:
            await up.aclose()
    return h


async def reset(handle: ScenarioHandle) -> None:
    cfg = handle.cfg
    up = Upstreams(cfg)
    with tracing.span("scenario.tfc2.reset", {"handle": handle.handle_id}):
        try:
            await _resolve_scenario_alerts(cfg, handle.handle_id)
            await handle.step("Reset to baseline complete (alert resolved)",
                              trigger="scenario.tfc2.reset",
                              detail={"alert_id": handle.cleanup.get("alert_id")})
            await handle.finish("RESET")
        finally:
            await up.aclose()


# --------------------------------------------------------------------------- helpers
def _publish_wrongway_track(cfg: ScenarioConfig, device_id: str, camera_id: str) -> int:
    """Publish a short wrong-way GPS track to truck.telemetry (trace-propagated).

    The kafka_io producer injects the active trace context into the message
    headers; the anomaly TelemetryWorker extracts it, so the WRONG_WAY alert is
    in the same Jaeger trace as this scenario step.
    """
    producer = kafka_io.get_producer()
    base = datetime.now(tz=timezone.utc)
    n = 0
    # Walk a few metres north-west each ping so derived bearing also reads ~315°.
    lat, lon = KARAL
    for i in range(PING_COUNT):
        evt = {
            "ts": (base + timedelta(seconds=i * PING_DT_S)).isoformat(),
            "device_id": device_id,
            "plate": SYNTH_PLATE,
            "lat": lat + i * 0.0002,     # moving north
            "lon": lon - i * 0.0002,     # and west -> NW ~315°
            "speed_kmh": 25.0,
            "heading": WRONG_WAY_HEADING,
            "camera_id": camera_id,
        }
        kafka_io.produce(producer, TOPIC_TELEMETRY, evt, key=device_id, flush=False)
        n += 1
    producer.flush(10)
    return n


async def _await_alert(up: Upstreams, *, plate: str, device_id: str,
                       attempts: int = 8, interval_s: float = 1.5) -> Optional[dict]:
    """Poll the gateway /api/alerts for the WRONG_WAY alert for our plate."""
    for _ in range(attempts):
        data = await up.gw_get("/api/alerts", {"kind": "WRONG_WAY", "since": "PT10M", "limit": "50"})
        for a in (data or {}).get("alerts", []):
            payload = a.get("payload") or {}
            if a.get("plate") == plate or payload.get("device_id") == device_id:
                return a
        await asyncio.sleep(interval_s)
    return None


def _evidence_mp4_url(cfg: ScenarioConfig, camera_id: str) -> str:
    """URL the dashboard uses to play the last-10s evidence clip (MinIO)."""
    return f"http://localhost:9000/evidence/{camera_id}-last10s.mp4"


async def _enrich_alert(cfg: ScenarioConfig, alert_id: str, extra: Dict[str, Any]) -> None:
    from jnpa_shared.db import execute
    import json
    try:
        await execute(
            "UPDATE jnpa.alerts SET payload = coalesce(payload,'{}'::jsonb) || CAST(:extra AS jsonb) "
            "WHERE id = CAST(:id AS uuid)",
            {"extra": json.dumps(extra), "id": alert_id}, dsn=cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("alert_enrich_failed", alert_id=alert_id, error=str(exc))


async def _resolve_scenario_alerts(cfg: ScenarioConfig, handle_id: str) -> None:
    from jnpa_shared.db import execute
    try:
        await execute(
            "UPDATE jnpa.alerts SET ack = true WHERE payload->>'scenario' = :hid OR plate = :plate",
            {"hid": handle_id, "plate": SYNTH_PLATE}, dsn=cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("resolve_alerts_failed", error=str(exc))


__all__ = ["NAME", "run", "reset"]
