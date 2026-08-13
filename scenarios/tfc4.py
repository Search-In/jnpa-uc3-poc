"""TFC-4 — Peak Yard Utilization & Truck Arrival Management.

Params: {yard_id: "JNPA-NSICT-YARD", gate_id: "G-NSICT", arrival_trucks: 14,
         target_utilization_pct: 95, release_containers: 5}

THIS SCENARIO OWNS NO LOGIC. Every step below is an HTTP call into the UC-3
implementation that already exists (migration 0144 + services/yard_capacity +
gateway/routers/yard.py). The scenario's only job is to drive that flow in
order and record what the backend actually answered, so the What-If Console
timeline shows real figures rather than a scripted animation:

  1. read the yard baseline            GET  /api/yard/capacity/board
  2. build arrival pressure            POST truck-sim /devices/inject (AT_GATE_QUEUE)
  3. drive the yard to peak            POST /api/yard/capacity/{yard}/adjust
  4. run arrival management            POST /api/yard/capacity/{yard}/evaluate
       -> detects the constraint, raises the EXISTING TRAFFIC_CONGESTION alert
          via services/congestion_alert.py, holds the surplus trucks, recommends
          the authorised CPP from live parking availability, and dispatches the
          driver advisory over the EXISTING WS + WebPush + FCM path
  5. report the holds + CPP + alert    (read back from the evaluate response)
  6. recover capacity + release        POST /api/yard/capacity/{yard}/release
  7. read the yard back to normal      GET  /api/yard/capacity/board

reset(): remove the injected trucks, force-release any remaining holds through
the same endpoint, and return the yard to its baseline occupancy — all through
the UC-3 API, so the scenario can never leave state the console cannot explain.

Auth: the runner presents INTERNAL_SERVICE_TOKEN, which the gateway maps to
DTCCC_ADMIN — inside CONTROL_ROOM, the audience the /api/yard/capacity writes
require. No RBAC rule is relaxed for this scenario.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from jnpa_shared import tracing
from jnpa_shared.logging import get_logger

from .base import Upstreams, resolve_scenario_alerts
from .config import ScenarioConfig
from .handle import ScenarioHandle, new_handle_id

log = get_logger("scenarios.tfc4")

NAME = "tfc4"

DEFAULT_YARD = "JNPA-NSICT-YARD"
DEFAULT_GATE = "G-NSICT"
#: Trucks injected into AT_GATE_QUEUE to create the arrival pressure. Enrolled
#: PWA driver vehicles are picked up on top of these by the evaluate step — it
#: reads the same fleet list the Congestion Rerouting console does.
DEFAULT_ARRIVALS = 14
DEFAULT_TARGET_PCT = 95.0
DEFAULT_RELEASE_CONTAINERS = 5


def _tag(handle_id: str) -> str:
    return f"TFC-4:{handle_id}"


def stub_cleanup(handle_id: str) -> Dict[str, Any]:
    """Cleanup dict for a post-restart stub reset (same tag ``run()`` mints)."""
    return {"truck_tag": _tag(handle_id), "yard_id": DEFAULT_YARD}


def _yard_facts(yard: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The yard figures every step reports, straight from the API payload.

    Only keys the backend actually returned are included — a missing board
    (gateway or DB unreachable) yields ``{}`` rather than zeros that would read
    as a measurement.
    """
    if not isinstance(yard, dict):
        return {}
    keys = ("yard_id", "name", "utilization_pct", "capacity_status", "capacity_slots",
            "occupied_slots", "available_slots", "headroom_slots", "operating_ceiling_slots",
            "admissible_trucks", "capacity_source", "capacity_declared")
    return {k: yard[k] for k in keys if k in yard}


async def _board(up: Upstreams, yard_id: str) -> Optional[Dict[str, Any]]:
    body = await up.gw_get("/api/yard/capacity/board", {"yard_id": yard_id, "events": "5"})
    return (body or {}).get("yard")


async def run(params: Dict[str, Any], handle_id: str | None = None) -> ScenarioHandle:
    cfg = ScenarioConfig.from_env()
    yard_id = str(params.get("yard_id") or DEFAULT_YARD)
    gate_id = str(params.get("gate_id") or DEFAULT_GATE)
    arrivals = int(params.get("arrival_trucks", DEFAULT_ARRIVALS))
    target_pct = float(params.get("target_utilization_pct", DEFAULT_TARGET_PCT))
    release_containers = int(params.get("release_containers", DEFAULT_RELEASE_CONTAINERS))

    h = ScenarioHandle(
        handle_id=handle_id or new_handle_id(NAME), name=NAME,
        params={"yard_id": yard_id, "gate_id": gate_id, "arrival_trucks": arrivals,
                "target_utilization_pct": target_pct,
                "release_containers": release_containers},
        cfg=cfg)
    up = Upstreams(cfg)
    tag = _tag(h.handle_id)
    h.cleanup = {"truck_tag": tag, "yard_id": yard_id, "gate_id": gate_id}

    with tracing.span("scenario.tfc4.run", {"yard_id": yard_id, "handle": h.handle_id}):
        await h.create_row()
        try:
            # --- Step 1: yard baseline (expected ~70% NORMAL) ----------------
            baseline = await _board(up, yard_id)
            base_facts = _yard_facts(baseline)
            # Remember the opening occupancy so reset can restore exactly it,
            # rather than a hardcoded number that would drift from the seed.
            if baseline:
                h.cleanup["baseline_occupied_slots"] = baseline.get("occupied_slots")
            await h.step(
                ("Yard baseline: "
                 f"{base_facts.get('utilization_pct', '?')}% "
                 f"{base_facts.get('capacity_status', 'UNKNOWN')}"),
                trigger="uc3:/api/yard/capacity/board",
                status="ok" if baseline else "degraded",
                detail={"phase": "baseline", "yard": base_facts,
                        **({} if baseline else
                           {"error": "yard capacity board unavailable — apply migration 0144"})},
            )

            # --- Step 2: arrival pressure into AT_GATE_QUEUE -----------------
            inj = await up.truck_post("/devices/inject", {
                "count": arrivals, "tag": tag, "gate_id": gate_id,
                "state": "AT_GATE_QUEUE",
            })
            injected = int((inj or {}).get("injected") or 0)
            await h.step(
                f"Injected {injected} AT_GATE_QUEUE truck arrivals at {gate_id}",
                trigger="truck-sim:/devices/inject",
                status="ok" if inj else "degraded",
                detail={"phase": "arrivals", "injected": injected, "requested": arrivals,
                        "gate_id": gate_id, "truck_tag": tag},
            )

            # --- Step 3: drive the yard to peak utilisation ------------------
            adj = await up.gw_post(f"/api/yard/capacity/{yard_id}/adjust", {
                "target_utilization_pct": target_pct,
                "event_type": "INCREASE",
                "reason": f"TFC-4 peak yard utilisation ({h.handle_id})",
            })
            peak_facts = _yard_facts((adj or {}).get("yard"))
            await h.step(
                ("Yard utilisation raised to "
                 f"{peak_facts.get('utilization_pct', target_pct)}% "
                 f"{peak_facts.get('capacity_status', 'CRITICAL')}"),
                trigger="uc3:/api/yard/capacity/{yard}/adjust",
                status="ok" if adj else "degraded",
                detail={"phase": "peak", "yard": peak_facts,
                        "audit_event": (adj or {}).get("event")},
            )

            # --- Step 4+5: arrival management (detect -> alert -> hold -> CPP
            #               -> driver advisory). ONE call into the existing UC-3
            #               service; everything below is read back from it.
            ev = await up.gw_post(f"/api/yard/capacity/{yard_id}/evaluate", {}) or {}
            ev_yard = _yard_facts(ev.get("yard"))
            arrivals_seen = ev.get("arrivals") or {}
            held = ev.get("held") or []
            alerts = ev.get("alerts") or []
            parking = ev.get("parking") or {}
            constrained = bool(ev.get("constrained"))

            await h.step(
                ("Arrival pressure detected — congestion pressure "
                 f"{ev.get('congestion_pressure', '?')} over "
                 f"{arrivals_seen.get('total', 0)} arriving trucks"),
                trigger="uc3:/api/yard/capacity/{yard}/evaluate",
                status="ok" if constrained else "degraded",
                detail={"phase": "detect", "yard": ev_yard,
                        "congestion_pressure": ev.get("congestion_pressure"),
                        "arrivals": arrivals_seen, "constrained": constrained,
                        "reason": ev.get("reason"),
                        # Present when the yard genuinely had room — the honest
                        # outcome, not a failure.
                        "detail_note": ev.get("detail")},
            )

            await h.step(
                (f"TRAFFIC_CONGESTION alert raised for {ev_yard.get('yard_id', yard_id)}"
                 if alerts else
                 "No new TRAFFIC_CONGESTION alert (already raised this hour — deduped)"),
                trigger="uc3:services/congestion_alert",
                status="ok" if alerts else "info",
                detail={"phase": "alert", "alerts": alerts,
                        "alert_id": (alerts[0].get("alert_id") if alerts else None),
                        "alert_kind": "TRAFFIC_CONGESTION",
                        "deduped": not alerts and constrained},
            )

            notified = sum(1 for x in held if x.get("notified"))
            await h.step(
                f"{len(held)} truck arrival(s) held — {ev.get('reason') or 'no hold required'}",
                trigger="uc3:core.truck_arrival_hold",
                status="ok" if held else "info",
                detail={"phase": "hold", "held_count": len(held),
                        "proceeding_count": len(ev.get("proceeding") or []),
                        "by_source": {
                            "truck-sim": sum(1 for x in held if x.get("source") == "truck-sim"),
                            "pwa-registered": sum(1 for x in held
                                                  if x.get("source") == "pwa-registered"),
                        },
                        "reason": ev.get("reason"),
                        "devices": [x.get("device_id") for x in held][:20]},
            )

            await h.step(
                (f"Recommended parking: {parking.get('name')} "
                 f"({parking.get('available')} bays free)"
                 if parking.get("recommended") else
                 "No authorised parking facility currently has available capacity"),
                trigger="uc3:/api/parking/availability",
                status="ok" if parking.get("recommended") else "degraded",
                detail={"phase": "parking", "parking": parking,
                        "facility_id": parking.get("facility_id"),
                        "facility_name": parking.get("name"),
                        "available": parking.get("available"),
                        "estimated_wait_min": parking.get("estimated_wait_min"),
                        "is_authorised_cpp": parking.get("is_preferred")},
            )

            await h.step(
                f"Driver advisory dispatched to {notified}/{len(held)} held driver(s)",
                trigger="uc3:gateway.notifications.dispatch (WS + WebPush + FCM)",
                status="ok" if notified else ("info" if not held else "degraded"),
                detail={"phase": "notify", "notified": notified,
                        "held_count": len(held),
                        "advisory_kind": "YARD_CAPACITY_HOLD",
                        "channels": ["websocket", "webpush", "fcm"],
                        "message_template": (
                            "JNPA yard capacity is currently at "
                            f"{ev_yard.get('utilization_pct', '?')}%. Please proceed to "
                            f"{parking.get('name') or 'the authorised parking facility'} "
                            "and wait until yard capacity becomes available.")},
            )

            # --- Step 6: capacity recovery + release ------------------------
            slots_per_truck = int(
                ((ev.get("yard") or {}).get("thresholds") or {}).get("slots_per_truck") or 2)
            free_slots = release_containers * slots_per_truck
            rel = await up.gw_post(f"/api/yard/capacity/{yard_id}/release", {
                "free_slots": free_slots,
                "reason": f"TFC-4 capacity recovery: release {release_containers} containers",
            }) or {}
            released = rel.get("released") or []
            rel_notified = sum(1 for x in released if x.get("release_notified"))
            await h.step(
                (f"Released {len(released)} truck(s) toward the gate after freeing "
                 f"{free_slots} slots ({release_containers} containers)"),
                trigger="uc3:/api/yard/capacity/{yard}/release",
                status="ok" if rel else "degraded",
                detail={"phase": "release", "released_count": len(released),
                        "still_held": rel.get("still_held"),
                        "freed_slots": free_slots,
                        "release_containers": release_containers,
                        "release_notified": rel_notified,
                        "advisory_kind": "YARD_CAPACITY_RELEASE",
                        "reason": rel.get("reason"),
                        "yard": _yard_facts(rel.get("yard")),
                        "devices": [x.get("device_id") for x in released][:20]},
            )

            # --- Step 7: closing yard position ------------------------------
            final = await _board(up, yard_id)
            final_facts = _yard_facts(final)
            await h.step(
                ("Scenario complete — yard at "
                 f"{final_facts.get('utilization_pct', '?')}% "
                 f"{final_facts.get('capacity_status', 'UNKNOWN')}"),
                trigger="uc3:/api/yard/capacity/board",
                status="ok" if final else "degraded",
                detail={"phase": "complete", "yard": final_facts,
                        "summary": {
                            "baseline_utilization_pct": base_facts.get("utilization_pct"),
                            "peak_utilization_pct": peak_facts.get("utilization_pct"),
                            "final_utilization_pct": final_facts.get("utilization_pct"),
                            "arrivals_evaluated": arrivals_seen.get("total"),
                            "held_count": len(held),
                            "released_count": len(released),
                            "parking_facility": parking.get("facility_id"),
                            "alert_id": (alerts[0].get("alert_id") if alerts else None),
                            "drivers_notified": notified,
                        }},
            )

            await h.finish("DONE")
        except Exception as exc:  # noqa: BLE001
            log.warning("tfc4_failed", handle=h.handle_id, error=str(exc))
            await h.step("Scenario error", trigger="scenario.tfc4", status="failed",
                         detail={"error": str(exc)})
            await h.finish("FAILED")
        finally:
            await up.aclose()
    return h


async def reset(handle: ScenarioHandle) -> None:
    """Return UC-3 demo state to baseline — and ONLY UC-3 demo state.

    Order matters: release the holds BEFORE restoring the occupancy, so each
    held driver still receives the proceed advisory through the normal path
    (a hold cancelled by a bare UPDATE would leave the driver's device showing
    a hold nobody ever cleared).
    """
    cfg = handle.cfg
    up = Upstreams(cfg)
    yard_id = handle.cleanup.get("yard_id") or handle.params.get("yard_id") or DEFAULT_YARD
    tag = handle.cleanup.get("truck_tag", _tag(handle.handle_id))
    baseline_occupied = handle.cleanup.get("baseline_occupied_slots")

    with tracing.span("scenario.tfc4.reset", {"handle": handle.handle_id}):
        try:
            # 1) release every outstanding hold through the UC-3 endpoint, so
            #    the drivers are notified and the audit trail is complete.
            rel = await up.gw_post(f"/api/yard/capacity/{yard_id}/release", {
                "force": True,
                "reason": f"TFC-4 reset ({handle.handle_id})",
            }) or {}

            # 2) restore the occupancy the yard opened at (recorded in step 1 of
            #    the run — never a hardcoded figure).
            restored = None
            if baseline_occupied is not None:
                restored = await up.gw_post(f"/api/yard/capacity/{yard_id}/adjust", {
                    "set_occupied": int(baseline_occupied),
                    "event_type": "RELEASE",
                    "reason": f"TFC-4 reset to baseline ({handle.handle_id})",
                })

            # 3) remove exactly the trucks this run injected.
            removed = await up.truck_delete(f"/devices/tagged/{tag}")

            # 4) ack the TRAFFIC_CONGESTION alert this run raised.
            await resolve_scenario_alerts(cfg, handle.handle_id,
                                          segment_ids=[f"YARD-{_terminal(yard_id)}"])

            await handle.step(
                "Reset to baseline complete",
                trigger="scenario.tfc4.reset",
                detail={"phase": "reset",
                        "released_count": rel.get("released_count"),
                        "trucks_removed": (removed or {}).get("removed"),
                        "truck_tag": tag,
                        "yard": _yard_facts((restored or {}).get("yard")),
                        "yard_id": yard_id},
            )
            await handle.finish("RESET")
        finally:
            await up.aclose()


def _terminal(yard_id: str) -> str:
    """Terminal code embedded in a yard id ('JNPA-NSICT-YARD' -> 'NSICT').

    Used only to name the alert segment the run raised so reset can ack it. An
    unrecognised shape falls back to the whole id, which simply matches nothing.
    """
    parts = str(yard_id).split("-")
    return parts[1] if len(parts) >= 3 else str(yard_id)


__all__ = ["NAME", "run", "reset", "stub_cleanup"]
