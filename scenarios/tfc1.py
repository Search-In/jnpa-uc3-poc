"""TFC-1 — Gate Closure.

Params: {gate_id: "G-NSICT", duration_minutes: 120}

Reactive chain (each step idempotent, recorded with its trigger source):
  1. Mark the gate closed in core.gate (closed_at = now()).
  2. Inject a synthetic high volume of AT_GATE_QUEUE trucks at the gate
     (tagged for reset) + nudge the near-port corridor segments so the build-up
     is real in the data.
  3. The congestion forecaster detects the build-up and predicts spillover to
     G-JNPCT / G-NSIGT within 15 min — we poll /predict and assert P>=0.7 at the
     segments feeding both (best-effort + nudge: recorded met/degraded).
  4. Auto-re-route EN_ROUTE_TO_PORT trucks targeting the closed gate to the
     best alternate gate via /api/routing/best_alt_gate -> /api/trucks/{id}/route.
  5. The TAS mock marks the closed gate's slots RESCHEDULED.

reset(): reopen the gate, remove injected trucks, clear the nudge, restore TAS
slots, mark synthetic alerts resolved.
"""
from __future__ import annotations

from typing import Any, Dict, List

from jnpa_shared import tracing
from jnpa_shared.logging import get_logger

from .base import Upstreams, clear_nudge, nudge_segments, poll_forecaster
from .config import ScenarioConfig
from .handle import ScenarioHandle, new_handle_id

log = get_logger("scenarios.tfc1")

NAME = "tfc1"
# Segments that feed the spillover gates (near-port cluster).
SPILLOVER_SEGMENTS = ["SEG-00", "SEG-01", "SEG-02", "SEG-03"]
SPILLOVER_GATES = ["G-JNPCT", "G-NSIGT"]
SYNTH_QUEUE = 80  # injected AT_GATE_QUEUE trucks for the first 20 min


async def run(params: Dict[str, Any], handle_id: str | None = None) -> ScenarioHandle:
    cfg = ScenarioConfig.from_env()
    gate_id = params.get("gate_id", "G-NSICT")
    duration = int(params.get("duration_minutes", 120))
    h = ScenarioHandle(handle_id=handle_id or new_handle_id(NAME), name=NAME,
                       params={"gate_id": gate_id, "duration_minutes": duration}, cfg=cfg)
    up = Upstreams(cfg)
    tag = f"TFC-1:{h.handle_id}"
    h.cleanup = {"gate_id": gate_id, "truck_tag": tag, "spillover_gates": SPILLOVER_GATES}

    with tracing.span("scenario.tfc1.run", {"gate_id": gate_id, "handle": h.handle_id}):
        await h.create_row()
        try:
            # --- Step 1: close the gate ---
            await _set_gate_closed(cfg, gate_id, closed=True)
            await h.step(f"Gate {gate_id} marked CLOSED", trigger="scenario.tfc1",
                         detail={"gate_id": gate_id, "duration_minutes": duration})

            # --- Step 2: inject AT_GATE_QUEUE build-up + nudge segments ---
            inj = await up.truck_post("/devices/inject", {
                "count": SYNTH_QUEUE, "tag": tag, "gate_id": gate_id, "state": "AT_GATE_QUEUE",
            })
            nudged = await nudge_segments(cfg, SPILLOVER_SEGMENTS, handle_id=h.handle_id)
            await h.step(
                f"Injected {inj.get('injected', 0) if inj else 0} AT_GATE_QUEUE trucks at {gate_id}",
                trigger="truck-sim:/devices/inject",
                status="ok" if inj else "degraded",
                detail={"injected": inj.get("injected") if inj else 0,
                        "segments_nudged": nudged},
            )

            # --- Step 3: forecaster detects spillover (assert P>=0.7) ---
            met, probs, crossed = await poll_forecaster(
                up, segment_ids=SPILLOVER_SEGMENTS, threshold=0.7, need=2, horizon_min=15,
            )
            await h.step(
                "Congestion forecaster predicts spillover to G-JNPCT & G-NSIGT (P>=0.7)",
                trigger="congestion:/predict",
                status="ok" if met else "degraded",
                detail={"assert_threshold": 0.7, "met": met, "crossed_segments": crossed,
                        "spillover_gates": SPILLOVER_GATES,
                        "probs": {s: round(float(probs.get(s, 0.0)), 3) for s in SPILLOVER_SEGMENTS}},
            )

            # --- Step 4: auto-re-route inbound trucks off the closed gate ---
            rerouted = await _reroute_inbound(up, h, gate_id)
            await h.step(
                f"Auto-re-routed {len(rerouted)} EN_ROUTE_TO_PORT trucks off {gate_id}",
                trigger="driver-advisory:/api/routing/best_alt_gate",
                detail={"rerouted_count": len(rerouted), "trucks": rerouted[:20],
                        "links": [f"/api/trucks/{t['device_id']}" for t in rerouted[:20]]},
            )

            # --- Step 5: TAS slots RESCHEDULED ---
            best = h.cleanup.get("best_alt_gate")
            tas = await up.gw_post("/api/tas/reschedule", {"gate_id": gate_id, "to_gate": best})
            await h.step(
                f"TAS marked {tas.get('rescheduled', 0) if tas else 0} slots RESCHEDULED at {gate_id}",
                trigger="tas-mock:/api/tas/reschedule",
                status="ok" if tas else "degraded",
                detail={"rescheduled": tas.get("rescheduled") if tas else 0, "to_gate": best},
            )

            await h.finish("DONE")
        except Exception as exc:  # noqa: BLE001
            log.warning("tfc1_failed", handle=h.handle_id, error=str(exc))
            await h.step("Scenario error", trigger="scenario.tfc1", status="failed",
                         detail={"error": str(exc)})
            await h.finish("FAILED")
        finally:
            await up.aclose()
    return h


async def reset(handle: ScenarioHandle) -> None:
    cfg = handle.cfg
    up = Upstreams(cfg)
    gate_id = handle.cleanup.get("gate_id", handle.params.get("gate_id", "G-NSICT"))
    tag = handle.cleanup.get("truck_tag", f"TFC-1:{handle.handle_id}")
    with tracing.span("scenario.tfc1.reset", {"handle": handle.handle_id}):
        try:
            await _set_gate_closed(cfg, gate_id, closed=False)
            await up.truck_delete(f"/devices/tagged/{tag}")
            await clear_nudge(cfg, handle.handle_id)
            await up.gw_post("/api/tas/restore", {"gate_id": gate_id})
            await _resolve_alerts(cfg, handle.handle_id)
            await _rewarm_caches(up)
            await handle.step("Reset to baseline complete", trigger="scenario.tfc1.reset",
                              detail={"gate_id": gate_id, "truck_tag": tag})
            await handle.finish("RESET")
        finally:
            await up.aclose()


# --------------------------------------------------------------------------- helpers
async def _set_gate_closed(cfg: ScenarioConfig, gate_id: str, *, closed: bool) -> None:
    """Mark a gate closed (closed_at = now()) or reopen it (closed_at = NULL)."""
    from jnpa_shared.db import execute
    sql = (
        "UPDATE core.gate SET closed_at = now() WHERE id = :id"
        if closed else
        "UPDATE core.gate SET closed_at = NULL WHERE id = :id"
    )
    try:
        await execute(sql, {"id": gate_id}, dsn=cfg.postgres_dsn)
    except Exception as exc:  # noqa: BLE001
        log.warning("gate_close_failed", gate_id=gate_id, closed=closed, error=str(exc))


async def _reroute_inbound(up: Upstreams, h: ScenarioHandle, gate_id: str) -> List[dict]:
    """Reroute EN_ROUTE_TO_PORT trucks targeting the closed gate to the best alt."""
    best = await up.gw_post("/api/routing/best_alt_gate", {"exclude": [gate_id], "eta_min": 15})
    best_gate = (best or {}).get("best_gate")
    h.cleanup["best_alt_gate"] = best_gate
    listing = await up.truck_get("/devices/list", {"state": "EN_ROUTE_TO_PORT", "limit": "1000"})
    rerouted: List[dict] = []
    if not best_gate or not listing:
        return rerouted
    targets = [d for d in listing.get("devices", []) if d.get("gate_id") == gate_id][:50]
    for d in targets:
        # Reroute via the GATEWAY (not the sim directly): the gateway forwards the
        # body to the truck-sim AND broadcasts the type=reroute WS frame + caches
        # LAST_REROUTE, so the driver PWA's Inbox / Re-route screen actually lights
        # up. truck_post() would move the truck but never notify the driver.
        ok = await up.gw_post(f"/api/trucks/{d['device_id']}/route",
                              {"gate_id": best_gate, "force_state": "EN_ROUTE_TO_PORT",
                               "reason": f"Gate {gate_id} congested — proceed to {best_gate}"})
        if ok:
            rerouted.append({"device_id": d["device_id"], "from": gate_id, "to": best_gate})
    return rerouted


async def _resolve_alerts(cfg: ScenarioConfig, handle_id: str) -> None:
    from jnpa_shared.db import execute
    try:
        await execute(
            "UPDATE core.alert SET ack = true WHERE payload->>'scenario' = :hid",
            {"hid": handle_id}, dsn=cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("resolve_alerts_failed", error=str(exc))


async def _rewarm_caches(up: Upstreams) -> None:
    """Force a fresh poll cycle so Redis caches re-warm post-reset (best-effort)."""
    await up.predict(15)
    await up.gw_get("/api/traffic/predict", {"horizon_min": "15"})


__all__ = ["NAME", "run", "reset"]
