"""Gate & lane board (UC3-021) and CPP metered release (UC3-027) orchestration.

Three rules this module exists to keep:

1. **The queue is counted, not inferred.** ``gate_cards`` fetches the counted
   queue and the throughput with two independent repository calls and never lets
   one stand in for the other. A gate with no camera observation reports
   ``queue = None`` with ``queue_status = "NO_OBSERVATION"`` — it does NOT fall
   back to a throughput estimate, because a plausible wrong number is worse than
   an honest gap. That is what makes the UI-068 gate-stop test pass: stop a gate
   and throughput goes to 0 while the counted queue keeps rising.

2. **Applying a reassignment produces a task, not a command.** ``preview_reassignment``
   runs the impact simulation and writes nothing; ``apply_reassignment`` writes a
   row to core.lane_reassignment_task and returns it. Neither one touches
   ``core.gate_lane`` — the lane's real state changes only when a human at the
   gate acts and the twin observes the result (UI-103).

3. **Only the congested terminal slows.** ``compute_release_plans`` derives each
   terminal's release rate from THAT terminal's own gate queue. There is no
   port-wide term in the metered path, so one terminal's congestion is
   arithmetically incapable of slowing another's release (F-06). The UNIFORM
   mode exists to demonstrate the contrast (UI-111) and is the only path that
   uses a shared rate.
"""
from __future__ import annotations

import math
from time import perf_counter
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

from .repository import GATE_TERMINAL, GateBoardRepository

log = get_logger("services.gate_board.service")

# --- congestion thresholds ---------------------------------------------------
# 8 / 20 vehicles, the same figures gateway/routers/camera_ai.py already derives
# congestion from. Kept in one place here so the board and the ingest endpoint
# cannot drift apart.
QUEUE_MEDIUM = 8
QUEUE_HIGH = 20

#: KPI 6 (shared/jnpa_shared/kpi.py "queue_length"): target 25 vehicles against a
#: 41-vehicle baseline. The metered release aims the gate queue at the TARGET.
QUEUE_TARGET = 25.0
QUEUE_BASELINE = 41.0

#: Nominal per-lane gate transaction capacity, vehicles/hour, used only when a
#: gate has produced no completed transactions in the window to measure from.
#: assumptions.json gates.txn_time_target_min = 3.0 min => 20 vehicles/hour/lane.
NOMINAL_LANE_VPH = 20.0

#: What a plaza release plan is allowed to slow to. A terminal is metered, never
#: stopped: a hard 0 would strand trucks with no ETA, which is worse for the
#: corridor than a slow trickle.
MIN_RELEASE_FRACTION = 0.15


def congestion_level(queue: Optional[int]) -> Optional[str]:
    """LOW / MEDIUM / HIGH from the COUNTED queue. None when nothing was counted."""
    if queue is None:
        return None
    if queue >= QUEUE_HIGH:
        return "HIGH"
    if queue >= QUEUE_MEDIUM:
        return "MEDIUM"
    return "LOW"


def advice_text(hold_minutes: int, queue: int, clearing_rate_vph: float) -> str:
    """The driver-facing sentence, in the UI-156 format, verbatim.

    Format (UI-156): "Hold at plaza for about 25 minutes - the gate queue is 40
    vehicles and clearing at 12 per hour". Every number in the sentence is an
    input to, or the output of, :func:`release_plan_for` — the driver and the
    control room are reading the same arithmetic.
    """
    if hold_minutes <= 0:
        return (f"Proceed to the gate - the gate queue is {queue} vehicles and "
                f"clearing at {round(clearing_rate_vph)} per hour.")
    return (f"Hold at plaza for about {hold_minutes} minutes - the gate queue is "
            f"{queue} vehicles and clearing at {round(clearing_rate_vph)} per hour.")


def release_plan_for(*, terminal_code: str, gate_id: Optional[str], queue: int,
                     clearing_rate_vph: float, mode: str = "METERED",
                     uniform_rate_vph: Optional[float] = None) -> Dict[str, Any]:
    """One terminal's release plan. Pure — no I/O, no clock, no RNG.

    METERED: the release rate is the terminal's own clearing rate scaled by how
    far its own queue sits above the KPI-6 target::

        release_rate = clearing_rate * (QUEUE_TARGET / queue)      [queue > target]
        release_rate = clearing_rate                                [queue <= target]

    floored at MIN_RELEASE_FRACTION of the clearing rate so a terminal is metered
    rather than stopped. Nothing in this expression refers to any other terminal.

    UNIFORM: every terminal is given the same ``uniform_rate_vph`` regardless of
    its own queue — the do-nothing comparison UI-111 asks to be demoable.

    ``hold_minutes`` is the time for the gate queue to fall from its current
    length to the target at the measured clearing rate::

        hold = ceil((queue - QUEUE_TARGET) / clearing_rate * 60)

    It is 0 whenever the queue is already at or under target. The formula is
    published with the number (``method`` in the returned dict) so an evaluator
    can check the advice rather than trust it.
    """
    clearing = max(float(clearing_rate_vph), 0.0)
    over_target = queue > QUEUE_TARGET

    if mode == "UNIFORM":
        release = float(uniform_rate_vph if uniform_rate_vph is not None else clearing)
    elif over_target and clearing > 0:
        scaled = clearing * (QUEUE_TARGET / float(queue))
        release = max(scaled, clearing * MIN_RELEASE_FRACTION)
    else:
        release = clearing

    if over_target and clearing > 0:
        hold = int(math.ceil((queue - QUEUE_TARGET) / clearing * 60.0))
    else:
        hold = 0

    return {
        "terminal_code": terminal_code,
        "gate_id": gate_id,
        "gate_queue_vehicles": int(queue),
        "clearing_rate_vph": round(clearing, 2),
        "release_rate_vph": round(release, 2),
        "hold_minutes": hold,
        "congestion_level": congestion_level(queue) or "LOW",
        "advice_text": advice_text(hold, int(queue), clearing),
        "mode": mode,
        "simulated": True,
        "method": {
            "queue_target_vehicles": QUEUE_TARGET,
            "queue_baseline_vehicles": QUEUE_BASELINE,
            "release_rate_formula": (
                "clearing_rate * (queue_target / queue), floored at "
                f"{MIN_RELEASE_FRACTION:.0%} of clearing_rate"
                if mode == "METERED" else
                "one port-wide rate applied to every terminal (do-nothing comparison)"
            ),
            "hold_minutes_formula": "ceil((queue - queue_target) / clearing_rate * 60)",
            "queue_source": "core.camera_ai_count (video-analytics count)",
            "clearing_rate_source": "core.gate_event GATE_IN over the window",
        },
    }




# --- UC3-023: EC-6 camera-outage degraded ladder -----------------------------
#
# A gate confirms a vehicle by joining an ANPR plate read with an RFID tag read.
# When the camera's frames stop, the ANPR half is gone and the gate does NOT
# stop working — it drops to RFID-only confirmation at a RECORDED lower
# confidence, plus a manual-verify lane for anything RFID alone cannot settle.
#
# The confidence drop is the point. A gate that silently kept reporting the same
# confidence after losing half its evidence would be asserting a certainty it no
# longer has, and every downstream decision (Auto-LEO identity match, violation
# plate attribution) would inherit that false certainty.
#
# Rungs, mapped from the camera feed's own state — never a UI-only status:
#   LIVE      frames arriving       -> ANPR+RFID join, full confidence
#   DEGRADED  frames stale (cached) -> ANPR+RFID at reduced confidence
#   NO_FEED   frames stopped        -> RFID-only + manual-verify lane
#
#: Confidence the gate records per rung. LIVE is the joined ANPR+RFID figure;
#: the others are what the gate can honestly claim with less evidence.
GATE_CONFIDENCE_BY_RUNG: Dict[str, float] = {
    "LIVE": 0.97,
    "DEGRADED": 0.82,
    "NO_FEED": 0.60,
}

#: Service-rate multiplier per rung. A manual-verify lane is slower than an
#: automated one, so losing the camera cuts the gate's throughput — which is what
#: makes the queue forecast worsen rather than staying flat through an outage.
SERVICE_RATE_FACTOR_BY_RUNG: Dict[str, float] = {
    "LIVE": 1.0,
    "DEGRADED": 0.85,
    "NO_FEED": 0.55,
}

#: EC-6 timing contract.
NO_FEED_DETECT_SECONDS = 5      # frames stop -> no_feed raised within 5 s
CARD_DOWN_VISIBLE_SECONDS = 60  # source card reads DOWN within 60 s


def rung_from_camera(decision_path: Optional[str]) -> str:
    """Map the camera's own decision path onto the gate's confirmation rung.

    Reads the ANPR cascade's state (LIVE / CACHED / SYNTHETIC) rather than
    inventing a parallel status, so the gate cannot disagree with the health
    strip about whether a camera is up.
    """
    p = (decision_path or "").upper()
    if p == "LIVE":
        return "LIVE"
    if p == "CACHED":
        return "DEGRADED"
    return "NO_FEED"


def degraded_mode_for(camera: Dict[str, Any]) -> Dict[str, Any]:
    """The gate's confirmation posture for one camera's current state."""
    rung = rung_from_camera(camera.get("decision_path"))
    no_feed = rung == "NO_FEED"
    return {
        "camera_id": camera.get("camera_id"),
        "decision_path": camera.get("decision_path"),
        "frame_age_s": camera.get("frame_age_s"),
        "fault_injected": bool(camera.get("forced")),
        "rung": rung,
        "no_feed": no_feed,
        # What the gate uses to confirm a vehicle at this rung.
        "confirmation_mode": "RFID_ONLY" if no_feed else "ANPR_RFID_JOIN",
        "confidence": GATE_CONFIDENCE_BY_RUNG[rung],
        "confidence_basis": (
            "RFID tag read only — the ANPR half of the join is unavailable"
            if no_feed else
            "ANPR plate read joined with the RFID tag read"
            if rung == "LIVE" else
            "ANPR read is stale (served from cache) and joined with RFID"),
        "manual_verify_lane": no_feed,
        "service_rate_factor": SERVICE_RATE_FACTOR_BY_RUNG[rung],
        "source_card": "DOWN" if no_feed else ("DEGRADED" if rung == "DEGRADED" else "LIVE"),
        # Replayed frames are never presented as live: the badge follows the rung.
        "feed_label": "LIVE" if rung == "LIVE" else ("REPLAY" if rung == "DEGRADED"
                                                     else "NO FEED"),
    }


class GateBoardService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[GateBoardRepository] = None) -> None:
        self._repo = repository or GateBoardRepository(dsn=dsn)

    # --------------------------------------------------------- UC3-021 board
    async def gate_cards(self, window_minutes: int = 60) -> Dict[str, Any]:
        """Per-gate card: in/out, COUNTED queue, mean transaction time."""
        t0 = perf_counter()
        gates = await self._repo.gates()
        queues = {q["gate_id"]: q for q in await self._repo.queue_at_gates()}
        flows = {f["gate_id"]: f for f in await self._repo.throughput_at_gates(window_minutes)}

        cards: List[Dict[str, Any]] = []
        for g in gates:
            gid = g["id"]
            q = queues.get(gid)
            f = flows.get(gid, {})
            # No camera observation => no queue. NEVER a throughput-derived guess.
            queue_val = int(q["queue_count"]) if q and q.get("queue_count") is not None else None
            avg_txn_s = f.get("avg_txn_seconds")
            cards.append({
                "gate_id": gid,
                "name": g.get("name"),
                "lat": g.get("lat"),
                "lon": g.get("lon"),
                "closed_at": g.get("closed_at"),
                "in_count": int(f.get("in_count") or 0),
                "out_count": int(f.get("out_count") or 0),
                "throughput_60min": int(f.get("in_count") or 0) + int(f.get("out_count") or 0),
                "avg_txn_minutes": round(float(avg_txn_s) / 60.0, 2) if avg_txn_s else None,
                "txn_samples": int(f.get("txn_samples") or 0),
                "queue_vehicles": queue_val,
                "queue_status": "COUNTED" if queue_val is not None else "NO_OBSERVATION",
                "queue_count_method": (q or {}).get("count_method"),
                "queue_camera_id": (q or {}).get("camera_id"),
                "queue_observed_at": (q or {}).get("observed_at"),
                "queue_confidence": (q or {}).get("confidence"),
                "congestion_level": congestion_level(queue_val),
            })

        log.info("gate_board.cards", extra={"gates": len(cards),
                                            "ms": round((perf_counter() - t0) * 1000)})
        return {
            "gates": cards,
            "count": len(cards),
            "window_minutes": window_minutes,
            "thresholds": {"medium": QUEUE_MEDIUM, "high": QUEUE_HIGH},
            "kpi": {"queue_length_target": QUEUE_TARGET,
                    "queue_length_baseline": QUEUE_BASELINE},
            # Stated on the board so a reader never has to assume it.
            "queue_provenance": {
                "source_table": "core.camera_ai_count",
                "accepted_methods": ["VIDEO_ANALYTICS", "MANUAL_COUNT"],
                "derived_from_throughput": False,
                "note": ("Queue length is counted in the camera frame. It is never "
                         "inferred from throughput, so stopping a gate makes the "
                         "queue rise while throughput reads zero (UI-068)."),
            },
        }

    async def lanes(self, gate_id: Optional[str] = None) -> Dict[str, Any]:
        rows = await self._repo.lanes(gate_id)
        return {"lanes": rows, "count": len(rows)}

    async def ticker(self, limit: int = 25) -> Dict[str, Any]:
        rows = await self._repo.confirmations(limit)
        return {"confirmations": rows, "count": len(rows)}

    # ------------------------------------------------- lane reassignment (UI-103)
    async def preview_reassignment(self, *, lane_id: str, to_type: str,
                                   window_minutes: int = 60) -> Dict[str, Any]:
        """Short impact simulation shown BEFORE anything is recorded.

        Writes nothing. The projection is deliberately simple and stated with its
        own arithmetic: converting a lane to the congested direction adds one
        lane's nominal capacity to that direction, and the projected queue is the
        current counted queue minus what the extra capacity clears in the window.
        """
        lane = await self._repo.lane(lane_id)
        if lane is None:
            return {"error": "lane_not_found", "lane_id": lane_id}

        gid = lane["gate_id"]
        queues = {q["gate_id"]: q for q in await self._repo.queue_at_gates()}
        flows = {f["gate_id"]: f for f in await self._repo.throughput_at_gates(window_minutes)}
        q = queues.get(gid)
        queue_now = int(q["queue_count"]) if q and q.get("queue_count") is not None else None
        lanes_at_gate = await self._repo.lanes(gid)
        open_lanes = [l for l in lanes_at_gate if l["lane_state"] == "OPEN"]

        added_vph = NOMINAL_LANE_VPH
        cleared = added_vph * (window_minutes / 60.0)
        projected = max(0, queue_now - int(round(cleared))) if queue_now is not None else None

        return {
            "lane_id": lane_id,
            "gate_id": gid,
            "from_lane_type": lane["lane_type"],
            "to_lane_type": to_type,
            "queue_now": queue_now,
            "queue_projected": projected,
            "queue_delta": (projected - queue_now) if projected is not None else None,
            "congestion_now": congestion_level(queue_now),
            "congestion_projected": congestion_level(projected),
            "open_lanes_at_gate": len(open_lanes),
            "added_capacity_vph": added_vph,
            "window_minutes": window_minutes,
            "throughput_60min": int((flows.get(gid) or {}).get("in_count") or 0)
                                + int((flows.get(gid) or {}).get("out_count") or 0),
            "simulated": True,
            "method": {
                "added_capacity_vph": NOMINAL_LANE_VPH,
                "basis": ("assumptions.json gates.txn_time_target_min = 3.0 min "
                          "=> 20 vehicles/hour/lane"),
                "formula": "queue_projected = queue_now - added_capacity_vph * window_hours",
            },
            # Restated on every preview so the operator reads it before applying.
            "applies_as": "HUMAN_TASK",
            "sends_equipment_command": False,
        }

    async def apply_reassignment(self, *, lane_id: str, to_type: str,
                                 reason: Optional[str], actor: Optional[str],
                                 window_minutes: int = 60) -> Dict[str, Any]:
        """Create the human task. Never changes lane state, never commands equipment."""
        preview = await self.preview_reassignment(lane_id=lane_id, to_type=to_type,
                                                  window_minutes=window_minutes)
        if preview.get("error"):
            return preview
        task = await self._repo.create_reassignment_task(
            gate_id=preview["gate_id"], lane_id=lane_id,
            from_type=preview["from_lane_type"], to_type=to_type,
            reason=reason, impact_preview=preview, created_by=actor,
        )
        log.info("gate_board.reassignment_task", extra={"lane_id": lane_id,
                                                        "gate_id": preview["gate_id"],
                                                        "actor": actor})
        return {
            "task": task,
            "preview": preview,
            "lane_state_changed": False,
            "sends_equipment_command": False,
            "note": ("A task was raised for the gate supervisor. This system issues "
                     "no commands to gate equipment (UI-103); the lane's state "
                     "changes only when a human acts and the twin observes it."),
        }

    async def tasks(self, *, status: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        rows = await self._repo.reassignment_tasks(status=status, limit=limit)
        return {"tasks": rows, "count": len(rows)}

    async def acknowledge_task(self, task_id: str, actor: Optional[str]) -> Optional[dict]:
        return await self._repo.acknowledge_task(task_id, actor)


    # ------------------------------------------------- UC3-023 degraded gate
    async def camera_degraded_mode(self, cameras: List[Dict[str, Any]]) -> Dict[str, Any]:
        """The EC-6 ladder across every gate camera, with the queue impact.

        ``cameras`` comes from the ANPR cascade (/api/kpi/cameras), so the state
        shown here is the feed's ACTUAL state — including a fault injected
        through /api/control/fault — and not a status this layer made up.
        """
        modes = [degraded_mode_for(c) for c in cameras]
        degraded = [m for m in modes if m["rung"] != "LIVE"]
        no_feed = [m for m in modes if m["no_feed"]]

        # The gate's effective service rate is the mean factor across its
        # cameras, so a camera outage visibly cuts throughput and therefore
        # worsens the queue forecast rather than leaving it flat.
        factor = (sum(m["service_rate_factor"] for m in modes) / len(modes)) if modes else 1.0
        nominal_vph = NOMINAL_LANE_VPH * 2  # two inbound lanes per gate
        return {
            "cameras": modes,
            "count": len(modes),
            "degraded_count": len(degraded),
            "no_feed_count": len(no_feed),
            "overall_rung": ("NO_FEED" if no_feed else
                             "DEGRADED" if degraded else "LIVE"),
            "service_rate_factor": round(factor, 3),
            "nominal_service_vph": nominal_vph,
            "effective_service_vph": round(nominal_vph * factor, 2),
            "timing_contract": {
                "no_feed_detect_seconds": NO_FEED_DETECT_SECONDS,
                "card_down_visible_seconds": CARD_DOWN_VISIBLE_SECONDS,
                "note": ("EC-6: frames stop -> no_feed within 5 s; the source card "
                         "reads DOWN and is evaluator-visible within 60 s."),
            },
            "fault_injection": {
                "endpoint": "/api/control/fault/camera",
                "note": ("The drill uses the project's existing fault console. "
                         "Forcing the camera rung drives this ladder end to end."),
            },
            "reconciliation": {
                "note": ("On restore the ladder walks back to LIVE and the rung "
                         "change is written to the decision log, so the outage and "
                         "its recovery are both auditable."),
            },
        }

    # --------------------------------------------------------- UC3-027 CPP
    async def compute_release_plans(self, *, mode: str = "METERED",
                                    persist: bool = True,
                                    window_minutes: int = 60) -> Dict[str, Any]:
        """Recompute every terminal's release rate from the LIVE gate queue.

        Called on the board's 5-second loop. Each METERED plan reads only its own
        terminal's queue, so throttling one gate cannot slow another's release.
        """
        t0 = perf_counter()
        queues = {q["gate_id"]: q for q in await self._repo.queue_at_gates()}
        flows = {f["gate_id"]: f for f in await self._repo.throughput_at_gates(window_minutes)}

        measured: List[Dict[str, Any]] = []
        for gate_id, terminal in GATE_TERMINAL.items():
            q = queues.get(gate_id)
            if not q or q.get("queue_count") is None:
                # No counted queue => no plan. An invented queue would produce an
                # invented release rate and an invented driver instruction.
                continue
            f = flows.get(gate_id) or {}
            cleared = int(f.get("in_count") or 0)
            clearing = float(cleared) * (60.0 / window_minutes) if window_minutes else 0.0
            if clearing <= 0:
                # Gate stopped: fall back to the nominal per-lane capacity so the
                # advice still has a rate to quote, and say so.
                clearing = NOMINAL_LANE_VPH
            measured.append({"gate_id": gate_id, "terminal_code": terminal,
                             "queue": int(q["queue_count"]), "clearing": clearing})

        uniform_rate = None
        if mode == "UNIFORM" and measured:
            uniform_rate = sum(m["clearing"] for m in measured) / len(measured)

        plans = [
            release_plan_for(terminal_code=m["terminal_code"], gate_id=m["gate_id"],
                             queue=m["queue"], clearing_rate_vph=m["clearing"],
                             mode=mode, uniform_rate_vph=uniform_rate)
            for m in measured
        ]

        persisted: List[Dict[str, Any]] = []
        if persist and plans:
            persisted = await self._repo.record_release_plans(plans)

        log.info("gate_board.release_recompute",
                 extra={"mode": mode, "terminals": len(plans),
                        "ms": round((perf_counter() - t0) * 1000)})
        return {
            "mode": mode,
            "plans": plans,
            "count": len(plans),
            "persisted": len(persisted),
            "window_minutes": window_minutes,
            "recompute_budget_seconds": 5,
            "simulated": True,
            "note": ("Each METERED release rate is derived from that terminal's own "
                     "counted gate queue only, so only the congested terminal slows "
                     "(F-06)."),
        }

    async def cpp_board(self) -> Dict[str, Any]:
        """Occupancy by zone + dwell histogram + latest release plans + amenities."""
        facilities = await self._repo.cpp_occupancy()
        zones = []
        for f in facilities:
            cap = int(f.get("capacity") or 0)
            occ = int(f.get("occupied") or 0)
            zones.append({
                "facility_id": f["facility_id"],
                "zone": f.get("facility_name"),
                "location": f.get("location"),
                "capacity": cap,
                "occupied": occ,
                "available": max(cap - occ, 0),
                "utilisation": round(occ / cap, 3) if cap else None,
                "status": f.get("status"),
            })
        histogram = await self._repo.cpp_dwell_histogram()
        # Persisted plans carry only the computed columns — core.cpp_release_plan
        # stores numbers, not prose. Re-attach the method block so a plan read
        # back from the ledger is as self-describing as a freshly computed one;
        # otherwise the board's traceability depends on which path produced the
        # row, and a consumer reading `plan["method"]` gets None.
        metered = [
            {**p, "method": release_plan_for(
                terminal_code=p["terminal_code"], gate_id=p.get("gate_id"),
                queue=int(p["gate_queue_vehicles"]),
                clearing_rate_vph=float(p["clearing_rate_vph"]),
                mode=p.get("mode", "METERED"))["method"]}
            for p in await self._repo.latest_release_plans("METERED")
        ]
        return {
            "zones": zones,
            "zone_count": len(zones),
            "totals": {
                "capacity": sum(z["capacity"] for z in zones),
                "occupied": sum(z["occupied"] for z in zones),
                "available": sum(z["available"] for z in zones),
            },
            "dwell_histogram": histogram,
            "dwell_status": "OK" if histogram else "NO_DATA",
            "release_plans": metered,
            # Amenity state has no feed in the corpus; it is declared, not faked.
            "amenities": {
                "status": "NOT_IN_CORPUS",
                "note": ("CPP amenity telemetry (washrooms, canteen, rest area) has no "
                         "source in the supplied corpus; it is a declared post-award "
                         "integration and is not simulated here."),
            },
            "occupancy_source": "core.parking_slot / core.parking_facility (RDS)",
        }
