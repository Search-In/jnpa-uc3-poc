"""TFC-3 — Cargo Surge Cross-Twin (Use Case II <-> III).

Params: {dpd_release_spike: 2.5}   # 2.5x baseline

Reactive chain:
  1. Publish a synthetic spike to Kafka ``cargo.dpd_release`` (the cross-twin
     link — UC-II would normally produce this).
  2. scenarios/uc2_bridge.py translates it into expected upstream truck demand
     (bursts of ~600 trucks/h over 40 min at 2.5x) and the trucking-app sim
     instantiates those trucks on the corridor (tagged for reset).
  3. The congestion forecaster predicts build-up on NH-348 segments 8-14 within
     30 min — we nudge those segments + poll /predict and assert >=5 cross P>=0.6
     (best-effort + nudge).
  4. The driver-advisory engine reissues gate-slot windows via
     /api/trucks/{id}/route; affected trucks would receive a PWA push (Prompt 11)
     — we record the push intents.
  5. (Dashboard) timeline shows the cross-twin link as a labelled arrow
     "UC-II DPD release -> UC-III demand".

reset(): remove injected trucks, clear the nudge, mark synthetic alerts
resolved, re-warm caches.
"""
from __future__ import annotations

from typing import Any, Dict, List

from jnpa_shared import kafka_io, tracing
from jnpa_shared.logging import get_logger

from .base import Upstreams, clear_nudge, nudge_segments, poll_forecaster
from .config import ScenarioConfig
from .handle import ScenarioHandle, new_handle_id
from .uc2_bridge import TOPIC_DPD_RELEASE, translate_release

log = get_logger("scenarios.tfc3")

NAME = "tfc3"
# "NH-348 segments 8-14" — the corridor has SEG-00..SEG-12, so the high-index
# downstream stretch SEG-07..SEG-12 (6 segments) stands in for "8-14".
SURGE_SEGMENTS = ["SEG-07", "SEG-08", "SEG-09", "SEG-10", "SEG-11", "SEG-12"]
# Cap how many of the translated demand we actually instantiate (PoC bound; the
# profile.total_trucks is recorded in full for the timeline).
MAX_INJECT = 300


async def run(params: Dict[str, Any]) -> ScenarioHandle:
    cfg = ScenarioConfig.from_env()
    spike = float(params.get("dpd_release_spike", 2.5))
    window_min = int(params.get("window_min", 40))
    h = ScenarioHandle(handle_id=new_handle_id(NAME), name=NAME,
                       params={"dpd_release_spike": spike, "window_min": window_min}, cfg=cfg)
    up = Upstreams(cfg)
    tag = f"TFC-3:{h.handle_id}"
    h.cleanup = {"truck_tag": tag}

    with tracing.span("scenario.tfc3.run", {"spike": spike, "handle": h.handle_id}):
        await h.create_row()
        try:
            # --- Step 1: publish the cross-twin DPD release spike ---
            event = {"dpd_release_spike": spike, "window_min": window_min, "source": "UC-II"}
            _publish_dpd_release(event)
            await h.step(
                f"UC-II published cargo.dpd_release spike x{spike}",
                trigger=f"kafka:{TOPIC_DPD_RELEASE}",
                detail={"cross_twin": "UC-II -> UC-III", "event": event},
            )

            # --- Step 2: bridge translates to demand + sim instantiates trucks ---
            profile = translate_release(event)
            inject_count = min(profile.total_trucks, MAX_INJECT)
            inj = await up.truck_post("/devices/inject", {
                "count": inject_count, "tag": tag, "state": "EN_ROUTE_TO_PORT",
            })
            await h.step(
                f"uc2_bridge -> {profile.trucks_per_h} trucks/h over {profile.window_min} min; "
                f"instantiated {inj.get('injected', 0) if inj else 0} on the corridor",
                trigger="scenarios.uc2_bridge",
                status="ok" if inj else "degraded",
                detail={"demand_profile": profile.to_dict(),
                        "injected": inj.get("injected") if inj else 0,
                        "capped_at": MAX_INJECT},
            )

            # --- Step 3: forecaster predicts build-up on segments 8-14 ---
            await nudge_segments(cfg, SURGE_SEGMENTS, handle_id=h.handle_id)
            met, probs, crossed = await poll_forecaster(
                up, segment_ids=SURGE_SEGMENTS, threshold=0.6, need=5, horizon_min=30,
            )
            await h.step(
                f"Forecaster predicts build-up on NH-348 segments 8-14 "
                f"({len(crossed)} segments >= P0.6)",
                trigger="congestion:/predict",
                status="ok" if met else "degraded",
                detail={"assert_threshold": 0.6, "need": 5, "met": met,
                        "crossed_segments": crossed,
                        "probs": {s: round(float(probs.get(s, 0.0)), 3) for s in SURGE_SEGMENTS}},
            )

            # --- Step 4: driver-advisory reissues gate-slot windows + PWA push ---
            pushes = await _reissue_slots(up, h, tag)
            await h.step(
                f"Driver-advisory reissued gate-slot windows for {len(pushes)} trucks "
                f"(PWA push queued)",
                trigger="driver-advisory:/api/trucks/{id}/route",
                detail={"push_count": len(pushes), "pushes": pushes[:20]},
            )

            # --- Step 5: cross-twin link annotation for the timeline ---
            await h.step(
                "Cross-twin link: UC-II DPD release -> UC-III corridor demand",
                trigger="cross-twin",
                detail={"arrow": {"from": "UC-II DPD release", "to": "UC-III demand"},
                        "multiplier": spike},
            )

            await h.finish("DONE")
        except Exception as exc:  # noqa: BLE001
            log.warning("tfc3_failed", handle=h.handle_id, error=str(exc))
            await h.step("Scenario error", trigger="scenario.tfc3", status="failed",
                         detail={"error": str(exc)})
            await h.finish("FAILED")
        finally:
            await up.aclose()
    return h


async def reset(handle: ScenarioHandle) -> None:
    cfg = handle.cfg
    up = Upstreams(cfg)
    tag = handle.cleanup.get("truck_tag", f"TFC-3:{handle.handle_id}")
    with tracing.span("scenario.tfc3.reset", {"handle": handle.handle_id}):
        try:
            await up.truck_delete(f"/devices/tagged/{tag}")
            await clear_nudge(cfg, handle.handle_id)
            await _resolve_alerts(cfg, handle.handle_id)
            await up.predict(15)  # force a fresh poll cycle to re-warm caches
            await handle.step("Reset to baseline complete", trigger="scenario.tfc3.reset",
                              detail={"truck_tag": tag})
            await handle.finish("RESET")
        finally:
            await up.aclose()


# --------------------------------------------------------------------------- helpers
def _publish_dpd_release(event: Dict[str, Any]) -> None:
    producer = kafka_io.get_producer()
    kafka_io.produce(
        producer, TOPIC_DPD_RELEASE, event, key="dpd", flush=True,
        event_type="jnpa.crosstwin.dpd_release",
        source_system="SIM",     # cross-twin surge stub emitted by the UC-3 console
        raw_ref="scenario://tfc3#dpd_release",
    )


async def _reissue_slots(up: Upstreams, h: ScenarioHandle, tag: str) -> List[dict]:
    """Reissue gate-slot windows for the injected trucks (records PWA push intents)."""
    listing = await up.truck_get("/devices/list", {"state": "EN_ROUTE_TO_PORT", "limit": "1000"})
    pushes: List[dict] = []
    if not listing:
        return pushes
    targets = [d for d in listing.get("devices", []) if str(d.get("device_id", "")).startswith(f"SYN-{tag}")][:50]
    for d in targets:
        # Reissue toward the same gate (a fresh route/slot window); record a push.
        ok = await up.truck_post(f"/devices/{d['device_id']}/route",
                                 {"gate_id": d.get("gate_id"), "force_state": "EN_ROUTE_TO_PORT"})
        if ok:
            pushes.append({"device_id": d["device_id"], "gate_id": d.get("gate_id"),
                           "pwa_push": "gate-slot-window-reissued"})
    return pushes


async def _resolve_alerts(cfg: ScenarioConfig, handle_id: str) -> None:
    from jnpa_shared.db import execute
    try:
        await execute(
            "UPDATE jnpa.alerts SET ack = true WHERE payload->>'scenario' = :hid",
            {"hid": handle_id}, dsn=cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("resolve_alerts_failed", error=str(exc))


__all__ = ["NAME", "run", "reset"]
