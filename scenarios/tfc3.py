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

from .base import (Upstreams, clear_nudge, nudge_segments, poll_forecaster,
                   resolve_scenario_alerts)
from .config import ScenarioConfig
from .handle import ScenarioHandle, new_handle_id
from . import uc2_bridge
from .uc2_bridge import TOPIC_DPD_RELEASE, translate_release

log = get_logger("scenarios.tfc3")

NAME = "tfc3"
# "NH-348 segments 8-14" — the corridor has SEG-00..SEG-12, so the high-index
# downstream stretch SEG-07..SEG-12 (6 segments) stands in for "8-14".
SURGE_SEGMENTS = ["SEG-07", "SEG-08", "SEG-09", "SEG-10", "SEG-11", "SEG-12"]
# Cap how many of the translated demand we actually instantiate (PoC bound; the
# profile.total_trucks is recorded in full for the timeline).
MAX_INJECT = 300


def stub_cleanup(handle_id: str) -> Dict[str, Any]:
    """Cleanup dict for a post-restart stub reset — SAME tag format as run()."""
    return {"truck_tag": f"TFC-3:{handle_id}"}


async def run(params: Dict[str, Any], handle_id: str | None = None) -> ScenarioHandle:
    cfg = ScenarioConfig.from_env()
    spike = float(params.get("dpd_release_spike", 2.5))
    window_min = int(params.get("window_min", 40))
    h = ScenarioHandle(handle_id=handle_id or new_handle_id(NAME), name=NAME,
                       params={"dpd_release_spike": spike, "window_min": window_min}, cfg=cfg)
    up = Upstreams(cfg)
    tag = f"TFC-3:{h.handle_id}"
    h.cleanup = {"truck_tag": tag}

    with tracing.span("scenario.tfc3.run", {"spike": spike, "handle": h.handle_id}):
        await h.create_row()
        try:
            # --- Step 1: publish the cross-twin DPD release spike ---
            # correlation_id marks this event as scenario-owned so the bridge
            # listener queues it for us instead of handling it autonomously.
            event = {"dpd_release_spike": spike, "window_min": window_min,
                     "source": "UC-II", "correlation_id": h.handle_id}
            _publish_dpd_release(event)
            await h.step(
                f"UC-II published cargo.dpd_release spike x{spike}",
                trigger=f"kafka:{TOPIC_DPD_RELEASE}",
                detail={"cross_twin": "UC-II -> UC-III", "event": event},
            )

            # --- Step 2: consume the event BACK through the broker, translate,
            # and instantiate trucks. The uc2_bridge listener (group
            # uc3-uc2-bridge) proves publish -> Kafka -> consume end-to-end;
            # if the broker/listener is unavailable we fall back to the local
            # copy and record the step degraded — the demo never stalls.
            consumed = await _await_consumed(h.handle_id, timeout_s=10.0)
            via = "kafka" if consumed else "inline-fallback"
            profile = translate_release(consumed or event)
            inject_count = min(profile.total_trucks, MAX_INJECT)
            inj = await up.truck_post("/devices/inject", {
                "count": inject_count, "tag": tag, "state": "EN_ROUTE_TO_PORT",
            })
            await h.step(
                f"uc2_bridge consumed the release ({via}) -> {profile.trucks_per_h} trucks/h "
                f"over {profile.window_min} min; instantiated "
                f"{inj.get('injected', 0) if inj else 0} on the corridor",
                trigger=(f"kafka-consumer:{uc2_bridge.CONSUMER_GROUP}"
                         if via == "kafka" else "scenarios.uc2_bridge"),
                status="ok" if (inj and via == "kafka") else "degraded",
                detail={"consumed": via,
                        "consumer_group": uc2_bridge.CONSUMER_GROUP,
                        "demand_profile": profile.to_dict(),
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


async def _await_consumed(correlation_id: str, *, timeout_s: float = 10.0) -> Dict[str, Any] | None:
    """Wait for OUR event to come back through the uc2_bridge Kafka consumer.

    Drains any unrelated correlation-tagged events (e.g. an older aborted run)
    while waiting. Returns None on timeout / listener down — the caller falls
    back to its inline copy and records the step degraded.
    """
    import asyncio as _asyncio
    import time as _time

    # Not listening / no partition assignment yet -> the 10 s wait would be
    # pure dead time; fall back immediately (recorded degraded).
    if not uc2_bridge.is_listening() or not uc2_bridge.wait_assigned(0.001):
        return None

    deadline = _time.monotonic() + timeout_s
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return None
        try:
            evt = await uc2_bridge.next_event(timeout=remaining)
        except _asyncio.TimeoutError:
            return None
        if evt.get("correlation_id") == correlation_id:
            return evt
        log.info("dpd_release_drained_foreign_event",
                 got=evt.get("correlation_id"), want=correlation_id)


async def _reissue_slots(up: Upstreams, h: ScenarioHandle, tag: str) -> List[dict]:
    """Reissue gate-slot windows for the injected trucks (records PWA push intents)."""
    listing = await up.truck_get("/devices/list", {"state": "EN_ROUTE_TO_PORT", "limit": "1000"})
    pushes: List[dict] = []
    if not listing:
        return pushes
    targets = [d for d in listing.get("devices", []) if str(d.get("device_id", "")).startswith(f"SYN-{tag}")][:50]
    for d in targets:
        # Reissue toward the same gate (a fresh route/slot window); record a push.
        # Go through the GATEWAY so the reissue also broadcasts the type=reroute WS
        # frame + caches LAST_REROUTE — that is what populates the driver PWA's
        # Inbox / Re-route screen. Posting straight to the sim skips that notify.
        ok = await up.gw_post(f"/api/trucks/{d['device_id']}/route",
                              {"gate_id": d.get("gate_id"), "force_state": "EN_ROUTE_TO_PORT",
                               "reason": "Gate-slot window reissued — confirm new arrival window"})
        if ok:
            pushes.append({"device_id": d["device_id"], "gate_id": d.get("gate_id"),
                           "pwa_push": "gate-slot-window-reissued"})
    return pushes


async def _resolve_alerts(cfg: ScenarioConfig, handle_id: str) -> None:
    # Shared helper also acks the untagged TRAFFIC_CONGESTION alerts our
    # segment nudges caused the gateway to auto-raise (reset must not leak).
    await resolve_scenario_alerts(cfg, handle_id, segment_ids=SURGE_SEGMENTS)


__all__ = ["NAME", "run", "reset"]
