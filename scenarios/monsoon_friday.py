"""Monsoon Friday — the master end-to-end scenario (UC-3 audit P1 Task 6).

One trigger stacks a weather disruption onto the weekly demand peak and walks the
full reactive chain the dashboard visualises end-to-end:

    Weather disruption  (heavy monsoon rain, Friday PM peak)
      -> Port congestion       (segment speeds drop; forecaster onset P>=0.7)
      -> Truck demand increase (arrival surge on the corridor)
      -> Gate queue            (AT_GATE_QUEUE build-up at the primary gate)
      -> Traffic rerouting     (best-alt-gate reroute of inbound trucks)
      -> Carbon impact         (idle-CO2e read; avoided idle from the reroute)

Each step is idempotent, records its trigger source, broadcasts a
``type=scenario_step`` frame to the dashboard (map impact + timeline), and the
Reactive Guide's MONSOON-FRIDAY causal chain narrates the WHY. Every figure is a
simulated propagation under the stated assumptions (never a claimed baseline).

reset(): clear the rain nudge, remove injected demand/queue trucks, restore TAS
slots, resolve synthetic alerts, and re-warm the forecaster caches.
"""
from __future__ import annotations

from typing import Any, Dict, List

from jnpa_shared import tracing
from jnpa_shared.logging import get_logger

from .base import Upstreams, clear_nudge, nudge_segments, poll_forecaster
from .config import ScenarioConfig
from .handle import ScenarioHandle, new_handle_id

log = get_logger("scenarios.monsoon_friday")

NAME = "monsoon_friday"

# Rain slows a broad mid/near-port stretch (heavier than a single-gate closure).
RAIN_SEGMENTS = ["SEG-04", "SEG-05", "SEG-06", "SEG-07", "SEG-08", "SEG-09", "SEG-10", "SEG-11", "SEG-12"]
PRIMARY_GATE = "G-NSICT"
SPILLOVER_GATES = ["G-JNPCT", "G-NSIGT", "G-BMCT"]
DEMAND_SURGE = 120     # extra EN_ROUTE_TO_PORT trucks (Friday-peak arrival wave)
GATE_QUEUE = 90        # AT_GATE_QUEUE build-up at the primary gate


async def run(params: Dict[str, Any], handle_id: str | None = None) -> ScenarioHandle:
    cfg = ScenarioConfig.from_env()
    gate_id = params.get("gate_id", PRIMARY_GATE)
    rain = params.get("rain_intensity", "heavy")
    demand = int(params.get("demand_trucks", DEMAND_SURGE))
    h = ScenarioHandle(
        handle_id=handle_id or new_handle_id(NAME), name=NAME,
        params={"gate_id": gate_id, "rain_intensity": rain, "demand_trucks": demand}, cfg=cfg,
    )
    up = Upstreams(cfg)
    demand_tag = f"MONSOON:demand:{h.handle_id}"
    queue_tag = f"MONSOON:queue:{h.handle_id}"
    h.cleanup = {"gate_id": gate_id, "demand_tag": demand_tag, "queue_tag": queue_tag,
                 "spillover_gates": SPILLOVER_GATES}

    with tracing.span("scenario.monsoon_friday.run", {"gate_id": gate_id, "handle": h.handle_id}):
        await h.create_row()
        try:
            # --- Step 1: weather disruption ------------------------------------
            nudged = await nudge_segments(cfg, RAIN_SEGMENTS, handle_id=h.handle_id)
            await h.step(
                f"Heavy monsoon rain + Friday PM peak — speeds drop on {nudged} corridor segments",
                trigger="scenario.monsoon_friday",
                status="ok" if nudged else "degraded",
                detail={"weather": rain, "peak": "friday_pm", "segments_nudged": nudged,
                        "segments": RAIN_SEGMENTS},
            )

            # --- Step 2: port congestion ---------------------------------------
            met, probs, crossed = await poll_forecaster(
                up, segment_ids=RAIN_SEGMENTS, threshold=0.7, need=4, horizon_min=15,
            )
            await h.step(
                "Congestion forecaster flags onset across the wet corridor (P>=0.7)",
                trigger="congestion:/predict",
                status="ok" if met else "degraded",
                detail={"assert_threshold": 0.7, "met": met, "crossed_segments": crossed,
                        "probs": {s: round(float(probs.get(s, 0.0)), 3) for s in RAIN_SEGMENTS}},
            )

            # --- Step 3: truck demand increase ---------------------------------
            dem = await up.truck_post("/devices/inject", {
                "count": demand, "tag": demand_tag, "gate_id": gate_id, "state": "EN_ROUTE_TO_PORT",
            })
            await h.step(
                f"Friday-peak arrival surge: +{dem.get('injected', 0) if dem else 0} inbound trucks",
                trigger="truck-sim:/devices/inject",
                status="ok" if dem else "degraded",
                detail={"injected": dem.get("injected") if dem else 0, "gate_id": gate_id},
            )

            # --- Step 4: gate queue --------------------------------------------
            q = await up.truck_post("/devices/inject", {
                "count": GATE_QUEUE, "tag": queue_tag, "gate_id": gate_id, "state": "AT_GATE_QUEUE",
            })
            await h.step(
                f"Gate {gate_id} queue builds to {q.get('injected', 0) if q else 0} vehicles",
                trigger="truck-sim:/devices/inject",
                status="ok" if q else "degraded",
                detail={"injected": q.get("injected") if q else 0, "gate_id": gate_id},
            )

            # --- Step 5: traffic rerouting -------------------------------------
            rerouted = await _reroute_inbound(up, h, gate_id)
            best = h.cleanup.get("best_alt_gate")
            tas = await up.gw_post("/api/tas/reschedule", {"gate_id": gate_id, "to_gate": best})
            await h.step(
                f"Auto-re-routed {len(rerouted)} inbound trucks to {best or 'best alt gate'}; "
                f"TAS reslotted {tas.get('rescheduled', 0) if tas else 0}",
                trigger="driver-advisory:/api/routing/best_alt_gate",
                status="ok" if rerouted or tas else "degraded",
                detail={"rerouted_count": len(rerouted), "to_gate": best,
                        "rescheduled": tas.get("rescheduled") if tas else 0,
                        "trucks": rerouted[:20]},
            )

            # --- Step 6: carbon impact -----------------------------------------
            carbon = await up.gw_get("/api/carbon/rollup")
            total_kg = (carbon or {}).get("total_kg")
            idle_kg = ((carbon or {}).get("by_source") or {}).get("idle")
            # Simulated avoided idle: rerouting trims queue dwell. Anchored to the
            # documented idle emission share — a shadow-run delta, not a live saving.
            avoided_kg = round((idle_kg or 0.0) * 0.18, 1) if idle_kg else None
            await h.step(
                "Carbon impact: rerouting trims idle dwell, avoiding queue CO2e",
                trigger="carbon:/api/carbon/rollup",
                status="ok" if carbon else "degraded",
                detail={"total_kg": total_kg, "idle_kg": idle_kg,
                        "avoided_idle_kg_sim": avoided_kg, "simulated": True},
            )

            await h.finish("DONE")
        except Exception as exc:  # noqa: BLE001
            log.warning("monsoon_friday_failed", handle=h.handle_id, error=str(exc))
            await h.step("Scenario error", trigger="scenario.monsoon_friday", status="failed",
                         detail={"error": str(exc)})
            await h.finish("FAILED")
        finally:
            await up.aclose()
    return h


async def reset(handle: ScenarioHandle) -> None:
    cfg = handle.cfg
    up = Upstreams(cfg)
    gate_id = handle.cleanup.get("gate_id", handle.params.get("gate_id", PRIMARY_GATE))
    demand_tag = handle.cleanup.get("demand_tag", f"MONSOON:demand:{handle.handle_id}")
    queue_tag = handle.cleanup.get("queue_tag", f"MONSOON:queue:{handle.handle_id}")
    with tracing.span("scenario.monsoon_friday.reset", {"handle": handle.handle_id}):
        try:
            await clear_nudge(cfg, handle.handle_id)
            await up.truck_delete(f"/devices/tagged/{demand_tag}")
            await up.truck_delete(f"/devices/tagged/{queue_tag}")
            await up.gw_post("/api/tas/restore", {"gate_id": gate_id})
            await _resolve_alerts(cfg, handle.handle_id)
            await up.predict(15)
            await up.gw_get("/api/traffic/predict", {"horizon_min": "15"})
            await handle.step("Reset to baseline complete", trigger="scenario.monsoon_friday.reset",
                              detail={"gate_id": gate_id})
            await handle.finish("RESET")
        finally:
            await up.aclose()


# --------------------------------------------------------------------------- helpers
async def _reroute_inbound(up: Upstreams, h: ScenarioHandle, gate_id: str) -> List[dict]:
    """Reroute EN_ROUTE_TO_PORT trucks targeting the primary gate to the best alt."""
    best = await up.gw_post("/api/routing/best_alt_gate", {"exclude": [gate_id], "eta_min": 15})
    best_gate = (best or {}).get("best_gate")
    h.cleanup["best_alt_gate"] = best_gate
    listing = await up.truck_get("/devices/list", {"state": "EN_ROUTE_TO_PORT", "limit": "1000"})
    rerouted: List[dict] = []
    if not best_gate or not listing:
        return rerouted
    targets = [d for d in listing.get("devices", []) if d.get("gate_id") == gate_id][:50]
    for d in targets:
        ok = await up.gw_post(
            f"/api/trucks/{d['device_id']}/route",
            {"gate_id": best_gate, "force_state": "EN_ROUTE_TO_PORT",
             "reason": f"Monsoon congestion at {gate_id} — proceed to {best_gate}"},
        )
        if ok:
            rerouted.append({"device_id": d["device_id"], "from": gate_id, "to": best_gate})
    return rerouted


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
