"""UC-3 orchestration: peak yard utilisation -> truck-arrival management.

Clean-architecture seam, exactly like :mod:`services.congestion_alert`: this
module owns the decisions and the persistence and receives EVERY external effect
as an injected async callable, so it never imports the gateway.

    YardCapacityService
        .board()               yard utilisation board (read)
        .adjust()              audited occupancy change (demo/ops control)
        .evaluate()            detect -> alert -> hold -> recommend -> notify
        .release()             capacity recovered -> release holds -> notify

Injected ports (all optional; every one degrades to a no-op):

    arrivals_fn()   -> {"devices":[...], "registered_devices":[...]}
                       the EXISTING fleet list (GET /api/trucks?state=AT_GATE_QUEUE),
                       so simulator trucks and enrolled PWA driver vehicles come
                       from the one source the dashboard already trusts.
    parking_fn()    -> [facility rows]   the EXISTING parking availability board.
    alert_fn(...)   -> [alerts]          services.congestion_alert.raise_congestion_alerts,
                       so a yard constraint raises the SAME auditable
                       TRAFFIC_CONGESTION alert a corridor jam does.
    dispatch_fn(device_id, advisory)     the EXISTING notification dispatcher
                                         (WebSocket + WebPush + FCM).
    broadcast_fn(type, payload)          dashboard WS fan-out for live refresh.

Nothing here fabricates a parking location, a wait time or a capacity figure: a
value that was not measured or configured is returned as ``None`` and the
payload says which source answered.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from jnpa_shared.logging import get_logger

from . import model
from .repository import YardCapacityRepository

log = get_logger("services.yard_capacity.service")

ArrivalsFn = Callable[[], Awaitable[Dict[str, Any]]]
ParkingFn = Callable[[], Awaitable[Sequence[Dict[str, Any]]]]
AlertFn = Callable[..., Awaitable[List[Dict[str, Any]]]]
DispatchFn = Callable[[str, Dict[str, Any]], Awaitable[Any]]
BroadcastFn = Callable[[str, Dict[str, Any]], Awaitable[Any]]

#: Alert kinds pushed to the driver's PWA. Distinct from the corridor
#: TRAFFIC_CONGESTION payload so the device can render the right copy, while the
#: DURABLE alert row stays TRAFFIC_CONGESTION (raised by the shared service).
ADVISORY_HOLD = "YARD_CAPACITY_HOLD"
ADVISORY_RELEASE = "YARD_CAPACITY_RELEASE"

#: WebSocket frame type the dashboard listens on for live yard updates.
WS_YARD = "yard_capacity"
#: WebSocket frame type carrying the arrival-management table delta.
WS_ARRIVAL = "arrival_management"


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class YardThresholds:
    """Effective thresholds for one yard: per-yard override, else the config."""

    __slots__ = ("high_pct", "critical_pct", "slots_per_truck",
                 "release_rate_slots_per_hour", "preferred_facility_id")

    def __init__(self, *, high_pct: float, critical_pct: float, slots_per_truck: int,
                 release_rate_slots_per_hour: Optional[float],
                 preferred_facility_id: Optional[str]) -> None:
        self.high_pct = float(high_pct)
        self.critical_pct = float(critical_pct)
        self.slots_per_truck = int(slots_per_truck)
        self.release_rate_slots_per_hour = release_rate_slots_per_hour
        self.preferred_facility_id = preferred_facility_id

    def as_dict(self) -> Dict[str, Any]:
        return {
            "high_utilization_pct": self.high_pct,
            "critical_utilization_pct": self.critical_pct,
            "slots_per_truck": self.slots_per_truck,
            "release_rate_slots_per_hour": self.release_rate_slots_per_hour,
            "preferred_parking_facility_id": self.preferred_facility_id,
        }


class YardCapacityService:
    def __init__(
        self,
        *,
        dsn: Optional[str],
        thresholds: YardThresholds,
        repo: Optional[YardCapacityRepository] = None,
        arrivals_fn: Optional[ArrivalsFn] = None,
        parking_fn: Optional[ParkingFn] = None,
        alert_fn: Optional[AlertFn] = None,
        dispatch_fn: Optional[DispatchFn] = None,
        broadcast_fn: Optional[BroadcastFn] = None,
    ) -> None:
        self._repo = repo or YardCapacityRepository(dsn)
        self._dsn = dsn
        self.thresholds = thresholds
        self._arrivals = arrivals_fn
        self._parking = parking_fn
        self._alert = alert_fn
        self._dispatch = dispatch_fn
        self._broadcast = broadcast_fn

    # ------------------------------------------------------------- helpers
    def _thresholds_for(self, yard: Dict[str, Any]) -> YardThresholds:
        t = self.thresholds
        return YardThresholds(
            high_pct=float(yard.get("high_threshold_pct") or t.high_pct),
            critical_pct=float(yard.get("critical_threshold_pct") or t.critical_pct),
            slots_per_truck=t.slots_per_truck,
            release_rate_slots_per_hour=t.release_rate_slots_per_hour,
            preferred_facility_id=t.preferred_facility_id,
        )

    async def _capacity(self, yard: Dict[str, Any]) -> tuple[int, str, bool]:
        """(capacity_slots, capacity_source, declared) for a yard row.

        Prefers ``core.yard_block`` (the capacity master, migration 0130); falls
        back to the declared seed and says so. ``declared`` is what the UI renders
        as the "assumed" marker — it is never hidden.
        """
        measured = await self._repo.capacity_for(str(yard.get("terminal_code") or ""))
        if measured:
            return measured, "core.yard_block", False
        return int(yard.get("capacity_slots") or 0), "core.yard_capacity_state", True

    async def _yard_view(self, yard: Dict[str, Any]) -> Dict[str, Any]:
        capacity, source, declared = await self._capacity(yard)
        occupied = int(yard.get("occupied_slots") or 0)
        thr = self._thresholds_for(yard)
        pct = model.utilization_pct(occupied, capacity)
        status = model.utilization_status(pct, high_pct=thr.high_pct,
                                          critical_pct=thr.critical_pct)
        avail = model.available_slots(occupied, capacity)
        ceiling = model.operating_ceiling(capacity, thr.critical_pct)
        headroom = model.headroom_slots(occupied, capacity, thr.critical_pct)
        return {
            "yard_id": yard.get("yard_id"),
            "terminal_code": yard.get("terminal_code"),
            "name": yard.get("name"),
            "capacity_slots": capacity,
            "occupied_slots": occupied,
            "available_slots": avail,
            "utilization_pct": pct,
            "capacity_status": status,
            "constrained": model.constrained(status),
            # Physical free space (available_slots) vs bookable space below the
            # operating ceiling (headroom_slots). Both are reported so the
            # reserve the arrival manager plans against is visible, not implied.
            "operating_ceiling_slots": ceiling,
            "headroom_slots": headroom,
            "admissible_trucks": model.admissible_trucks(headroom, thr.slots_per_truck),
            "capacity_source": source,
            "capacity_declared": declared,
            "occupancy_source": yard.get("source"),
            "source_note": yard.get("source_note"),
            "thresholds": thr.as_dict(),
            "updated_at": _iso(yard.get("updated_at")),
        }

    # --------------------------------------------------------------- board
    async def board(self, *, yard_id: Optional[str] = None,
                    include_events: int = 10) -> Dict[str, Any]:
        """Yard utilisation board: every active yard plus the selected one."""
        yards = await self._repo.list_yards()
        views = [await self._yard_view(y) for y in yards]
        selected = None
        if yard_id:
            selected = next((v for v in views if v["yard_id"] == yard_id), None)
        if selected is None and views:
            selected = views[0]
        events: List[Dict[str, Any]] = []
        if selected and include_events:
            events = [_event_view(e) for e in
                      await self._repo.recent_events(selected["yard_id"], include_events)]
        active = await self._repo.active_holds(selected["yard_id"] if selected else None)
        return {
            "yard": selected,
            "yards": views,
            "recent_events": events,
            "active_holds": len(active),
            "ts": _utcnow_iso(),
        }

    # -------------------------------------------------------------- adjust
    async def adjust(self, *, yard_id: str, delta_slots: Optional[int] = None,
                     set_occupied: Optional[int] = None,
                     target_utilization_pct: Optional[float] = None,
                     event_type: str = "SET", reason: Optional[str] = None,
                     actor: Optional[str] = None,
                     detail: Optional[dict] = None) -> Optional[Dict[str, Any]]:
        """Audited occupancy change. Returns the refreshed yard view + the event.

        ``target_utilization_pct`` is resolved against the CURRENT effective
        capacity (block master when populated, declared otherwise) so "take the
        yard to 95%" always means 95% of the capacity the board is showing.
        """
        yard = await self._repo.get_yard(yard_id)
        if yard is None:
            return None
        capacity, _, _ = await self._capacity(yard)
        if target_utilization_pct is not None:
            pct = max(0.0, min(100.0, float(target_utilization_pct)))
            set_occupied = int(round(capacity * pct / 100.0))
            delta_slots = None

        thr = self._thresholds_for(yard)

        def status_fn(pct: float) -> str:
            return model.utilization_status(pct, high_pct=thr.high_pct,
                                            critical_pct=thr.critical_pct)

        updated = await self._repo.adjust_occupancy(
            yard_id=yard_id, delta_slots=delta_slots, set_occupied=set_occupied,
            event_type=event_type, reason=reason, actor=actor,
            status_fn=status_fn, detail=detail)
        if updated is None:
            return None
        view = await self._yard_view(updated)
        out = {"yard": view, "event": _event_view(updated.get("last_event") or {})}
        await self._emit(WS_YARD, {"yard": view, "event": out["event"]})
        return out

    # ------------------------------------------------------------ evaluate
    async def evaluate(self, *, yard_id: str, actor: Optional[str] = None,
                       notify: bool = True,
                       dry_run: bool = False) -> Dict[str, Any]:
        """Detect the capacity constraint and manage the approaching arrivals.

        Steps, in order, each recorded:
          1. read the yard and the live AT_GATE_QUEUE arrivals (simulator trucks
             AND enrolled PWA driver devices);
          2. compute the congestion pressure from utilisation + arrival surplus;
          3. raise a TRAFFIC_CONGESTION alert through the EXISTING congestion
             alert service (deduped per hour by that service);
          4. hold the trucks the yard cannot absorb, each with a REAL parking
             recommendation from the live parking board;
          5. push the driver advisory over WS + WebPush + FCM and audit the
             delivery result per truck.

        ``dry_run`` runs 1-2 only and writes nothing — the "what would happen"
        probe for the console.
        """
        yard = await self._repo.get_yard(yard_id)
        if yard is None:
            return {"error": "unknown_yard", "yard_id": yard_id}
        view = await self._yard_view(yard)
        thr = self._thresholds_for(yard)

        arrivals_payload = await self._read_arrivals()
        candidates = (
            model.candidates_from_devices(arrivals_payload.get("devices") or [],
                                          default_source="truck-sim")
            + model.candidates_from_devices(arrivals_payload.get("registered_devices") or [],
                                            default_source="pwa-registered")
        )
        existing = await self._repo.active_holds(yard_id)
        # ONE line answering "which trucks did arrival management actually see,
        # and which did it filter, and why" — the trace the live TFC-4 diagnosis
        # needed. already-held is the ONLY filter this service applies; anything
        # missing beyond that was absent from the queue read itself (see the
        # queue_* provenance echoed in the response below).
        held_ids = [h["device_id"] for h in existing]
        log.info("yard_evaluate_arrivals", yard_id=yard_id,
                 simulator=sum(1 for c in candidates if c.source == "truck-sim"),
                 enrolled_pwa=sum(1 for c in candidates if c.source == "pwa-registered"),
                 filtered_already_held=[c.device_id for c in candidates
                                        if c.device_id in set(held_ids)],
                 queue_degraded=bool(arrivals_payload.get("degraded")),
                 queue_decision_path=arrivals_payload.get("decision_path"),
                 candidate_ids_sample=[c.device_id for c in candidates][:15])
        plan = model.plan_holds(
            yard_id=yard_id,
            capacity_slots=view["capacity_slots"],
            occupied_slots=view["occupied_slots"],
            candidates=candidates,
            high_pct=thr.high_pct, critical_pct=thr.critical_pct,
            slots_per_truck=thr.slots_per_truck,
            already_held=held_ids,
        )

        result: Dict[str, Any] = {
            "yard": view,
            "arrivals": {
                "total": plan.arrivals,
                "simulator": sum(1 for c in candidates if c.source == "truck-sim"),
                "enrolled_pwa": sum(1 for c in candidates if c.source == "pwa-registered"),
                "already_held": len(existing),
                "queue_source": arrivals_payload.get("source"),
                "queue_degraded": bool(arrivals_payload.get("degraded")),
                # Which rung of the fleet list answered, and how old a cached
                # answer was — so "0 arrivals" is always distinguishable from
                # "the queue could not be read" in the response itself.
                "queue_decision_path": arrivals_payload.get("decision_path"),
                "queue_cache_age_s": arrivals_payload.get("cache_age_s"),
            },
            "congestion_pressure": plan.pressure,
            "constrained": plan.constrained,
            "reason": model.HOLD_REASON if plan.constrained else None,
            "alerts": [],
            "held": [],
            "proceeding": [c.device_id for c in plan.proceed],
            "parking": None,
            "dry_run": dry_run,
            "ts": _utcnow_iso(),
        }

        if not plan.constrained:
            result["detail"] = (
                f"Yard at {view['utilization_pct']}% ({view['capacity_status']}) with "
                f"{plan.arrivals} arriving and room for {plan.admissible_trucks}. "
                "No arrival management required.")
            return result

        # --- 3) TRAFFIC_CONGESTION alert through the shared service -----------
        alert_id: Optional[str] = None
        if not dry_run and self._alert is not None:
            segment_id = f"YARD-{view['terminal_code']}"
            try:
                created = await self._alert(
                    predictions={segment_id: plan.pressure},
                    segment_meta={segment_id: {
                        "route": f"{view['name']} — yard capacity",
                        "gate": _dominant_gate(plan.hold) or None,
                    }},
                )
                result["alerts"] = created or []
                if created:
                    alert_id = created[0].get("alert_id")
            except Exception as exc:  # noqa: BLE001 — alerting never blocks the hold
                log.warning("yard_capacity.alert_failed", error=str(exc))

        # --- 4) parking recommendation from the LIVE parking board ------------
        facilities = await self._read_parking()
        facility = model.select_parking(facilities,
                                        preferred_id=thr.preferred_facility_id)
        slots_needed = max(0, len(plan.hold)) * thr.slots_per_truck
        wait_min = model.estimated_wait_min(
            slots_needed=slots_needed,
            release_rate_slots_per_hour=thr.release_rate_slots_per_hour)
        result["parking"] = _facility_view(facility, wait_min,
                                           thr.preferred_facility_id, len(facilities))

        if dry_run:
            result["would_hold"] = [c.device_id for c in plan.hold]
            return result

        # --- 5) hold + notify -------------------------------------------------
        held: List[Dict[str, Any]] = []
        for cand in plan.hold:
            row = await self._repo.create_hold({
                "device_id": cand.device_id, "plate": cand.plate,
                "driver_id": cand.driver_id, "driver_name": cand.driver_name,
                "source": cand.source, "gate_id": cand.gate_id, "eta_s": cand.eta_s,
                "yard_id": yard_id, "yard_utilization_pct": view["utilization_pct"],
                "reason": model.HOLD_REASON,
                "recommended_facility_id": (facility or {}).get("facility_id"),
                "recommended_facility_name": (facility or {}).get("name"),
                "facility_available": (facility or {}).get("available"),
                "facility_lat": (facility or {}).get("lat"),
                "facility_lon": (facility or {}).get("lon"),
                "estimated_wait_min": wait_min,
                "alert_id": alert_id,
                "actor": actor,
                "detail": {
                    "congestion_pressure": plan.pressure,
                    "capacity_status": view["capacity_status"],
                    "capacity_source": view["capacity_source"],
                    "admissible_trucks": plan.admissible_trucks,
                    "parking_available": bool(facility),
                },
            })
            if row is None:  # already held by a concurrent evaluation
                continue
            if notify:
                # Reflect the outcome on the row we are about to return; the DB
                # row is updated by _push, but this dict predates that write.
                row["notified"] = await self._notify_hold(row, view, facility, wait_min)
            held.append(_hold_view(row))

        result["held"] = held
        await self._emit(WS_ARRIVAL, {
            "yard_id": yard_id, "action": "HELD", "count": len(held),
            "utilization_pct": view["utilization_pct"],
            "capacity_status": view["capacity_status"],
            "devices": [h["device_id"] for h in held],
        })
        return result

    # ------------------------------------------------------------- release
    async def release(self, *, yard_id: str, device_ids: Optional[Sequence[str]] = None,
                      actor: Optional[str] = None, notify: bool = True,
                      force: bool = False) -> Dict[str, Any]:
        """Release held trucks once the yard has room again.

        Without ``device_ids`` the service releases as many holds as the recovered
        capacity can absorb, oldest hold first (first held, first released).
        ``force`` releases every outstanding hold regardless of capacity — the
        control-room override, recorded in the audit trail like any other action.
        """
        yard = await self._repo.get_yard(yard_id)
        if yard is None:
            return {"error": "unknown_yard", "yard_id": yard_id}
        view = await self._yard_view(yard)
        thr = self._thresholds_for(yard)
        active = await self._repo.active_holds(yard_id)

        if device_ids:
            wanted = {str(d) for d in device_ids}
            targets = [h for h in active if h["device_id"] in wanted]
        elif force:
            targets = list(active)
        else:
            n = model.releasable(
                capacity_slots=view["capacity_slots"],
                occupied_slots=view["occupied_slots"],
                active_holds=len(active),
                high_pct=thr.high_pct, critical_pct=thr.critical_pct,
                slots_per_truck=thr.slots_per_truck)
            targets = active[:n]

        reason = ("operator override" if force and not device_ids
                  else f"yard capacity available ({view['available_slots']} slots, "
                       f"{view['utilization_pct']}% utilised)")
        released = await self._repo.release_holds([h["id"] for h in targets],
                                                  actor=actor, reason=reason)
        if notify:
            for row in released:
                row["release_notified"] = await self._notify_release(row, view)

        out = {
            "yard": view,
            "released": [_hold_view(r) for r in released],
            "released_count": len(released),
            "still_held": max(0, len(active) - len(released)),
            "reason": reason,
            "ts": _utcnow_iso(),
        }
        if released:
            await self._emit(WS_ARRIVAL, {
                "yard_id": yard_id, "action": "RELEASED", "count": len(released),
                "utilization_pct": view["utilization_pct"],
                "capacity_status": view["capacity_status"],
                "devices": [r["device_id"] for r in released],
            })
        return out

    # ---------------------------------------------------------------- reads
    async def arrival_board(self, *, yard_id: Optional[str] = None,
                            include_history: int = 25) -> Dict[str, Any]:
        """The Congestion-Rerouting console's arrival-management table."""
        active = await self._repo.active_holds(yard_id)
        history = await self._repo.holds(yard_id=yard_id, status=model.HOLD_RELEASED,
                                         limit=include_history) if include_history else []
        yard = await self._repo.get_yard(yard_id) if yard_id else None
        view = await self._yard_view(yard) if yard else None
        return {
            "yard": view,
            "holds": [_hold_view(h) for h in active],
            "active_count": len(active),
            "released_recent": [_hold_view(h) for h in history],
            "by_source": {
                "truck-sim": sum(1 for h in active if h.get("source") == "truck-sim"),
                "pwa-registered": sum(1 for h in active
                                      if h.get("source") == "pwa-registered"),
            },
            "ts": _utcnow_iso(),
        }

    async def device_hold(self, device_id: str) -> Dict[str, Any]:
        """The driver-facing view: this device's current/most-recent hold."""
        row = await self._repo.latest_hold_for(device_id)
        return {"device_id": device_id,
                "hold": _hold_view(row) if row else None,
                "events": [_event_view(e) for e in
                           await self._repo.hold_events(device_id, limit=20)]}

    # -------------------------------------------------------------- effects
    async def _read_arrivals(self) -> Dict[str, Any]:
        if self._arrivals is None:
            return {"devices": [], "registered_devices": []}
        try:
            return await self._arrivals() or {}
        except Exception as exc:  # noqa: BLE001 — a queue outage must not 500 here
            log.warning("yard_capacity.arrivals_unavailable", error=str(exc))
            return {"devices": [], "registered_devices": [], "degraded": True}

    async def _read_parking(self) -> List[Dict[str, Any]]:
        if self._parking is None:
            return []
        try:
            return list(await self._parking() or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("yard_capacity.parking_unavailable", error=str(exc))
            return []

    async def _emit(self, frame: str, payload: Dict[str, Any]) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast(frame, {**payload, "ts": _utcnow_iso()})
        except Exception as exc:  # noqa: BLE001
            log.debug("yard_capacity.broadcast_failed", frame=frame, error=str(exc))

    async def _notify_hold(self, row: Dict[str, Any], view: Dict[str, Any],
                           facility: Optional[Dict[str, Any]],
                           wait_min: Optional[int]) -> bool:
        where = (facility or {}).get("name") or "the authorised parking facility"
        body = (f"JNPA yard capacity is currently at {view['utilization_pct']:.0f}%. "
                f"Please proceed to {where} and wait until yard capacity becomes "
                "available.")
        advisory = {
            "type": ADVISORY_HOLD,
            "kind": ADVISORY_HOLD,
            "title": "Hold at authorised parking",
            "body": body,
            "message": body,
            "category": "parking",
            "href": "#/parking",
            "severity": view["capacity_status"],
            "reason": model.HOLD_REASON,
            "yard_id": view["yard_id"],
            "yard_utilization_pct": view["utilization_pct"],
            "yard_available_slots": view["available_slots"],
            "recommended_facility_id": (facility or {}).get("facility_id"),
            "recommended_facility": where,
            "facility_available": (facility or {}).get("available"),
            "facility_lat": (facility or {}).get("lat"),
            "facility_lon": (facility or {}).get("lon"),
            "estimated_wait_min": wait_min,
            "hold_id": row.get("id"),
            "alert_id": row.get("alert_id"),
            "requires_ack": False,
        }
        return await self._push(row, advisory, release=False)

    async def _notify_release(self, row: Dict[str, Any], view: Dict[str, Any]) -> bool:
        # One decimal here, unlike the hold advisory: the release message quotes a
        # figure that is deliberately just below the threshold, and rounding it to
        # a whole number printed "95% utilised" next to "capacity is available".
        body = (f"Yard capacity is available ({view['available_slots']} slots free, "
                f"{view['utilization_pct']:.1f}% utilised). You may now proceed to "
                f"{row.get('gate_id') or 'your assigned terminal gate'}.")
        advisory = {
            "type": ADVISORY_RELEASE,
            "kind": ADVISORY_RELEASE,
            "title": "Proceed to terminal gate",
            "body": body,
            "message": body,
            "category": "parking",
            "href": "#/trip",
            "severity": view["capacity_status"],
            "yard_id": view["yard_id"],
            "yard_utilization_pct": view["utilization_pct"],
            "yard_available_slots": view["available_slots"],
            "gate_id": row.get("gate_id"),
            "hold_id": row.get("id"),
            "requires_ack": False,
        }
        return await self._push(row, advisory, release=True)

    async def _push(self, row: Dict[str, Any], advisory: Dict[str, Any],
                    *, release: bool) -> bool:
        """Dispatch one advisory and audit the outcome. Returns whether it landed.

        The caller MUTATES its in-memory row with this result: ``create_hold`` /
        ``release_holds`` return the row as it was written, i.e. with
        ``notified``/``release_notified`` still false, because the push happens
        after the INSERT. Returning the flag (rather than re-reading the row) is
        what stops the API response from reporting "0 drivers notified" while the
        table and the audit trail both say every one of them was.
        """
        device_id = str(row.get("device_id"))
        delivered = False
        detail: Dict[str, Any] = {}
        if self._dispatch is not None:
            try:
                res = await self._dispatch(device_id, advisory)
                as_dict = getattr(res, "as_dict", None)
                detail = as_dict() if callable(as_dict) else (
                    res if isinstance(res, dict) else {"result": bool(res)})
                delivered = bool(detail.get("ws") or detail.get("webpush")
                                 or detail.get("fcm") or detail.get("result"))
            except Exception as exc:  # noqa: BLE001
                detail = {"error": str(exc)}
        else:
            detail = {"error": "no_dispatch_transport"}
        try:
            await self._repo.mark_notified(int(row["id"]), device_id,
                                           delivered=delivered, detail=detail,
                                           release=release)
        except Exception as exc:  # noqa: BLE001 — audit best-effort
            log.warning("yard_capacity.notify_audit_failed", device_id=device_id,
                        error=str(exc))
        return delivered


# ------------------------------------------------------------------ shaping
def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _dominant_gate(candidates: Sequence[model.ArrivalCandidate]) -> Optional[str]:
    """The gate most of the held trucks were heading for (for the alert label)."""
    counts: Dict[str, int] = {}
    for c in candidates:
        if c.gate_id:
            counts[c.gate_id] = counts.get(c.gate_id, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda g: (counts[g], g))


def _facility_view(facility: Optional[Dict[str, Any]], wait_min: Optional[int],
                   preferred_id: Optional[str], considered: int) -> Dict[str, Any]:
    """The parking recommendation block — explicit when nothing has room."""
    if facility is None:
        return {
            "recommended": False,
            "facility_id": None, "name": None,
            "reason": ("No authorised parking facility currently has available "
                       "capacity."),
            "estimated_wait_min": wait_min,
            "facilities_considered": considered,
            "preferred_facility_id": preferred_id,
        }
    return {
        "recommended": True,
        "facility_id": facility.get("facility_id"),
        "name": facility.get("name"),
        "lat": facility.get("lat"),
        "lon": facility.get("lon"),
        "capacity": facility.get("capacity"),
        "available": facility.get("available"),
        "status": facility.get("status"),
        "is_preferred": str(facility.get("facility_id")) == str(preferred_id),
        "reason": model.HOLD_REASON,
        "estimated_wait_min": wait_min,
        "facilities_considered": considered,
        "preferred_facility_id": preferred_id,
        "source": "core.parking_facility / core.parking_slot",
    }


def _hold_view(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    d = dict(row)
    for k in ("held_at", "released_at", "updated_at"):
        if k in d:
            d[k] = _iso(d[k])
    for k in ("yard_utilization_pct",):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return d


def _event_view(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    d = dict(row)
    for k in ("created_at",):
        if k in d:
            d[k] = _iso(d[k])
    if d.get("utilization_pct") is not None:
        d["utilization_pct"] = float(d["utilization_pct"])
    return d


__all__ = ["YardCapacityService", "YardThresholds",
           "ADVISORY_HOLD", "ADVISORY_RELEASE", "WS_YARD", "WS_ARRIVAL"]
