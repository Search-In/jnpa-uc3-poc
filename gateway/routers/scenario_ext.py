"""Scenario-facing gateway endpoints (Sub-Criterion 5 plumbing).

These are the action/stub surfaces the what-if scenarios (scenarios/) call, kept
on the gateway because it is the one public service the dashboard already holds a
WebSocket to:

    POST /api/routing/best_alt_gate   -> pick the alternate gate with the lowest
                                         predicted queue at ETA (TFC-1 step 4).
    POST /api/echallan/issue          -> e-Challan workflow stub; resolves the
                                         plate through the Vahan fallback chain
                                         (shows LIVE_PRIMARY/.../PROVISIONAL) and
                                         returns a fake challan id + PDF url
                                         (TFC-2 step 3).
    GET  /api/tas/slots               -> TAS slot book (gateway/tas_mock.py).
    POST /api/tas/reschedule          -> mark a gate's slots RESCHEDULED (TFC-1).
    POST /api/tas/restore             -> restore a gate's slots (reset).
    POST /api/scenario_step           -> fan a scenario_step frame to dashboard
                                         WS clients (the scenarios-runner posts
                                         here so the storyline paints live).
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Body, Depends

from .. import tas_mock
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
from .geo import GATE_TARGETS

log = get_logger("gateway.scenario_ext")

router = APIRouter(tags=["scenario"])

ALL_GATES = list(GATE_TARGETS.keys())

# Segment -> the gate it predominantly feeds, for the queue heuristic. The four
# gates cluster at the port end (SEG-00..02), so near-port congestion is the
# strongest queue signal; we map each gate to the first corridor segment.
_GATE_FEED_SEGMENT = {g: "SEG-00" for g in ALL_GATES}


# ----------------------------------------------------------- best_alt_gate
@router.post("/api/routing/best_alt_gate")
async def best_alt_gate(
    body: Dict[str, Any] = Body(default_factory=dict),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Choose the alternate gate with the lowest predicted queue at ETA.

    Body: ``{exclude: ["G-NSICT"], eta_min?: 15}``. We score each candidate by
    (live AT_GATE_QUEUE depth) + (predicted corridor congestion near the gate at
    the ETA horizon), and return the minimum. Degrades to round-robin if upstream
    signals are unavailable so TFC-1 always gets an answer.
    """
    exclude = set(body.get("exclude") or [])
    eta_min = int(body.get("eta_min", 15))
    candidates = [g for g in ALL_GATES if g not in exclude] or ALL_GATES

    # Live queue depth per gate from the truck-sim.
    queue: Dict[str, int] = {g: 0 for g in candidates}
    try:
        url = state.cfg.truck_api_url.rstrip("/") + "/devices/list"
        resp = await state.http.get(url, params={"state": "AT_GATE_QUEUE", "limit": "2000"})
        if resp.status_code == 200:
            for d in resp.json().get("devices", []):
                g = d.get("gate_id")
                if g in queue:
                    queue[g] += 1
    except httpx.HTTPError as exc:
        log.debug("best_alt_queue_unavailable", error=str(exc))

    # Predicted congestion near each gate at the ETA horizon.
    pred: Dict[str, float] = {}
    try:
        url = state.cfg.congestion_url.rstrip("/") + "/predict"
        resp = await state.http.post(url, json={"horizon_min": eta_min})
        if resp.status_code == 200:
            probs = resp.json()
            for g in candidates:
                pred[g] = float(probs.get(_GATE_FEED_SEGMENT.get(g, "SEG-00"), 0.0))
    except httpx.HTTPError as exc:
        log.debug("best_alt_pred_unavailable", error=str(exc))

    def score(g: str) -> float:
        # queue weighted heavier (a truck in queue is a sure delay); congestion
        # is the forward-looking term.
        return queue.get(g, 0) * 1.0 + pred.get(g, 0.0) * 10.0

    ranked = sorted(candidates, key=score)
    best = ranked[0]
    await state.record_decision(
        api="routing", key=best, decision_path="BEST_ALT_GATE",
        detail={"exclude": list(exclude), "eta_min": eta_min,
                "scores": {g: round(score(g), 3) for g in candidates}},
    )
    REQUESTS.labels("routing", "ok").inc()
    return {
        "best_gate": best,
        "eta_min": eta_min,
        "ranking": [{"gate_id": g, "queue": queue.get(g, 0),
                     "pred_congestion": round(pred.get(g, 0.0), 3),
                     "score": round(score(g), 3)} for g in ranked],
    }


# ----------------------------------------------------------- e-Challan stub
@router.post("/api/echallan/issue")
async def echallan_issue(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """e-Challan workflow stub (TFC-2 step 3).

    Resolves the plate through the Vahan fallback chain first (so the response
    shows which rung served it — LIVE_PRIMARY when surepass is up, else
    LIVE_FALLBACK/CACHED/PROVISIONAL), then mints a deterministic fake challan id
    + PDF url. No real money/PII leaves the box.
    """
    plate = (body.get("plate") or "").upper().replace(" ", "")
    kind = body.get("kind", "WRONG_WAY")
    if not plate:
        REQUESTS.labels("echallan", "invalid").inc()
        return {"error": "plate_required"}

    # Resolve RC via the gateway's own Vahan chain (shows fallback path).
    vahan_path = "UNKNOWN"
    owner = None
    try:
        url = f"http://127.0.0.1:{state.cfg.port}/api/vahan/rc/{plate}"
        resp = await state.http.get(url)
        if resp.status_code == 200:
            v = resp.json()
            vahan_path = v.get("decision_path", "UNKNOWN")
            owner = (v.get("record") or {}).get("owner_name_masked")
    except httpx.HTTPError as exc:
        log.warning("echallan_vahan_unreachable", error=str(exc))

    # Deterministic fake challan id (no RNG) so the demo is reproducible.
    h = hashlib.sha256(f"{plate}:{kind}".encode()).hexdigest()[:10].upper()
    challan_id = f"ECH-{h}"
    pdf_url = f"http://localhost:9000/echallan/{challan_id}.pdf"

    _CHALLAN: Dict[str, Dict[str, Any]] = {
        "WRONG_WAY": {"section": "MVA s.184", "fine_inr": 5000},
        "OVERSPEEDING": {"section": "MVA s.183", "fine_inr": 2000},
        "ILLEGAL_PARKING": {"section": "MVA s.122/177", "fine_inr": 1000},
        "ROUTE_DEVIATION": {"section": "JNPA SOP", "fine_inr": 500},
    }
    detail = _CHALLAN.get(kind, {"section": "MVA s.177", "fine_inr": 500})

    await state.record_decision(
        api="echallan", key=plate, decision_path="ISSUED",
        detail={"challan_id": challan_id, "vahan_path": vahan_path, "kind": kind},
    )
    REQUESTS.labels("echallan", "ok").inc()
    return {
        "echallan_id": challan_id,
        "echallan_pdf_url": pdf_url,
        "plate": plate,
        "kind": kind,
        "owner_name_masked": owner,
        "vahan_decision_path": vahan_path,
        **detail,
    }


# ----------------------------------------------------------- TAS slot book
@router.get("/api/tas/slots")
async def tas_slots(gate_id: Optional[str] = None) -> dict:
    return {"slots": tas_mock.list_slots(gate_id)}


@router.post("/api/tas/reschedule")
async def tas_reschedule(body: Dict[str, Any] = Body(...)) -> dict:
    gate_id = body.get("gate_id")
    to_gate = body.get("to_gate")
    if not gate_id:
        return {"error": "gate_id_required"}
    affected = tas_mock.reschedule_gate(gate_id, to_gate=to_gate)
    return {"rescheduled": len(affected), "gate_id": gate_id, "slots": affected}


@router.post("/api/tas/restore")
async def tas_restore(body: Dict[str, Any] = Body(...)) -> dict:
    gate_id = body.get("gate_id")
    if not gate_id:
        return {"error": "gate_id_required"}
    restored = tas_mock.restore_gate(gate_id)
    return {"restored": restored, "gate_id": gate_id}


# ----------------------------------------------------------- scenario_step fan-out
@router.post("/api/scenario_step")
async def scenario_step(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Fan a scenario step out to dashboard WS clients as ``type=scenario_step``.

    The scenarios-runner posts each step here; the gateway holds the dashboard
    sockets, so this is how the storyline paints beneath the map in real time.
    """
    await state.ws.broadcast("scenario_step", body)
    REQUESTS.labels("scenario_step", "ok").inc()
    return {"broadcast": True, "ws_clients": state.ws.client_count}


__all__ = ["router"]
