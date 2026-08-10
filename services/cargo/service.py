"""Cargo service orchestration — the single write/read entry point.

Thin over :class:`services.cargo.repository.CargoRepository`: it owns
observability (one structured log line per op) and the typed error envelope, and
keeps the router free of any SQL. Mirrors :mod:`services.fastag.service`:
stateless apart from the DSN, so one shared instance is safe.

The repository is dependency-injected (default: a real ``CargoRepository`` bound
to the DSN) so tests can pass an in-memory fake — the same override seam the
FASTag router uses.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Mapping, Optional

from jnpa_shared.logging import get_logger

from .repository import (
    CargoConflict,
    CargoCustomsBlocked,
    CargoNotFound,
    CargoRepository,
    CargoTransitionError,
)

log = get_logger("services.cargo.service")


# --------------------------------------------------------------------------- events
# Cargo lifecycle event names (the notifications contract UC-2 consumes). Stable
# string constants so the topic namespace is defined in exactly one place.
EVENT_CREATED = "cargo.created"
EVENT_RELEASED = "cargo.released"
EVENT_YARD_ASSIGNED = "cargo.yard_assigned"
EVENT_STATUS_CHANGED = "cargo.status_changed"
EVENT_GATE_MOVEMENT = "cargo.gate_movement"
EVENT_UPDATED = "cargo.updated"
EVENT_DELETED = "cargo.deleted"
# Granular lifecycle topics added for the POC-2 extension (all ADDITIVE — the
# legacy topics above still fire unchanged, so existing consumers are unaffected):
EVENT_CUSTOMS_STATUS_CHANGED = "cargo.customs_status_changed"
EVENT_GATE_IN = "cargo.gate_in"
EVENT_GATE_OUT = "cargo.gate_out"
EVENT_PENDENCY_CREATED = "cargo.pendency_created"
EVENT_QUEUE_UPDATED = "cargo.queue_updated"
# UC-II lifecycle topics (migration 0023 — all ADDITIVE). ``cargo.lifecycle_changed``
# fires on EVERY accepted transition; the specific topics fire on their step.
EVENT_LIFECYCLE_CHANGED = "cargo.lifecycle_changed"
EVENT_VESSEL_DISCHARGED = "cargo.vessel_discharged"
EVENT_YARD_POSITION_ALLOCATED = "cargo.yard_position_allocated"
EVENT_REEFER_PLANNED = "cargo.reefer_planned"
EVENT_RAKE_ASSIGNED = "cargo.rake_assigned"
EVENT_VERIFIED = "cargo.verified"
# Pendency (audit Phase 5). Distinct from the notification-driven
# ``cargo.pendency_created`` above: that one fires when a stakeholder notification
# is raised, this one when the CONTAINER itself enters the PENDENCY state.
EVENT_PENDENCY_RECORDED = "cargo.pendency_recorded"

# Milestones that are DISTRIBUTED (Kafka + WS) in addition to being logged to
# core.cargo_event. Deliberately a small set: the handover signals other systems
# and screens actually react to — not the full CRUD chatter.
_BUS_EVENTS = frozenset({
    EVENT_RELEASED, EVENT_VERIFIED, EVENT_VESSEL_DISCHARGED,
    EVENT_LIFECYCLE_CHANGED, EVENT_CUSTOMS_STATUS_CHANGED,
})

# --------------------------------------------------------------------- lifecycle
# The single source of truth for the cargo lifecycle state machine (task #1).
#
#   CREATED -> VESSEL_DISCHARGED -> [PENDENCY] -> YARD_ASSIGNED
#           -> [YARD_POSITION_ALLOCATED | REEFER_PLANNED | RAKE_ASSIGNED]  (optional)
#           -> SCAN_PENDING (derived queue label) -> VERIFIED -> RELEASED
#
# Each state carries an ordinal RANK. Transitions are FORWARD-ONLY, and a move may
# never skip a MANDATORY gate (discharge, yard-assign, verify, release). The
# optional planning states between YARD_ASSIGNED and VERIFIED may be skipped.
#
# PENDENCY (audit Phase 5) sits between discharge and yard-assignment: a box
# landed on the quay and awaiting evacuation. It is deliberately OPTIONAL, not a
# mandatory gate — making it mandatory would invalidate every container already
# recorded as VESSEL_DISCHARGED -> YARD_ASSIGNED and break the existing demo path.
# Skipping it stays legal; recording it is now possible and audited.
LC_CREATED = "CREATED"
LC_VESSEL_DISCHARGED = "VESSEL_DISCHARGED"
LC_PENDENCY = "PENDENCY"
LC_YARD_ASSIGNED = "YARD_ASSIGNED"
LC_YARD_POSITION_ALLOCATED = "YARD_POSITION_ALLOCATED"
LC_REEFER_PLANNED = "REEFER_PLANNED"
LC_RAKE_ASSIGNED = "RAKE_ASSIGNED"
LC_SCAN_PENDING = "SCAN_PENDING"
LC_VERIFIED = "VERIFIED"
LC_RELEASED = "RELEASED"

# Customs dispositions that forbid a release, whatever the lifecycle says.
#
# `lifecycle_status` and `customs_status` are independent tracks, but not
# orthogonal at this point: out-of-charge is what permits goods to leave customs
# control, so a container customs is holding or examining cannot lawfully be
# gate-out released. Release previously checked the lifecycle alone, which let
# UNDER_INSPECTION + RELEASED rows exist — a state no real container can be in.
#
# PENDING is deliberately NOT here. It means customs has said nothing yet, which
# is the state of most of the corpus; blocking on it would stop the whole demo
# on missing data rather than on a customs decision.
CUSTOMS_BLOCKS_RELEASE = frozenset({"HELD", "UNDER_INSPECTION"})

_LIFECYCLE_RANK: dict[str, int] = {
    LC_CREATED: 0,
    LC_VESSEL_DISCHARGED: 10,
    LC_PENDENCY: 15,                  # optional (awaiting evacuation)
    LC_YARD_ASSIGNED: 20,
    LC_YARD_POSITION_ALLOCATED: 21,   # optional
    LC_REEFER_PLANNED: 22,            # optional
    LC_RAKE_ASSIGNED: 23,            # optional
    LC_SCAN_PENDING: 24,            # optional (derived queue label)
    LC_VERIFIED: 30,
    LC_RELEASED: 40,
}
# Gates that can never be skipped. A transition is rejected if a mandatory gate's
# rank lies strictly between the current and target ranks.
_MANDATORY_STATES: frozenset[str] = frozenset(
    {LC_CREATED, LC_VESSEL_DISCHARGED, LC_YARD_ASSIGNED, LC_VERIFIED, LC_RELEASED})
# Every non-terminal state. Retained as a descriptive helper only: the PUT
# is_released=true path used to force RELEASED from anywhere via this set, which
# let a caller bypass VERIFY. update_cargo now applies the same VERIFY gate as
# POST /release, so no write path may use this as a predecessor set.
_ALL_NON_RELEASED: frozenset[str] = frozenset(
    s for s in _LIFECYCLE_RANK if s != LC_RELEASED)


def can_transition(current: str, target: str) -> bool:
    """True iff ``current`` -> ``target`` is a legal lifecycle move: strictly
    forward, skipping no mandatory gate. Unknown states are never transitionable."""
    if current not in _LIFECYCLE_RANK or target not in _LIFECYCLE_RANK:
        return False
    cr, tr = _LIFECYCLE_RANK[current], _LIFECYCLE_RANK[target]
    if tr <= cr:
        return False
    return not any(cr < _LIFECYCLE_RANK[m] < tr for m in _MANDATORY_STATES)


def allowed_predecessors(target: str) -> set[str]:
    """The set of states from which ``target`` is a legal next step."""
    return {s for s in _LIFECYCLE_RANK if can_transition(s, target)}

# Workflow action -> resulting status. The single source of truth for the
# TRIGGER → APPROVE / REJECT lifecycle (migration 0016).
WORKFLOW_TRANSITIONS: dict[str, str] = {
    "TRIGGER": "TRIGGERED",
    "APPROVE": "APPROVED",
    "REJECT": "REJECTED",
}

# Nominal slots per yard block letter-zone — a POC capacity constant used only to
# derive a 0..1 congestion score for GET /api/cargo/yard-optimization.
_YARD_BLOCK_CAPACITY = 10


# --------------------------------------------------------------------------- RBAC
# Role -> extra list/count filter overrides. A role that constrains visibility maps
# to equality filters that WIN over any client-supplied filter (a hard scope, not a
# hint). Roles not listed here (operator / terminal_ops / control room / police /
# unknown / none) see everything — so the existing contract is unchanged. Keys are
# normalised (lower-case); the auth Role enum values fold onto them (CUSTOMS ->
# "customs", DRIVER -> "driver").
_ROLE_SCOPES: dict[str, dict[str, Any]] = {
    "driver": {"is_released": True},     # a driver only sees boxes released for haulage
    "customs": {"is_released": False},   # customs works the pre-release clearance pipeline
}


def scope_filters_for_role(role: Optional[str]) -> dict[str, Any]:
    """The hard filter overrides a role imposes on list/count (empty = see all).

    Backward compatible: an absent/blank/unknown role imposes no scope, so callers
    that pass no role behave exactly as before."""
    if not role:
        return {}
    return dict(_ROLE_SCOPES.get(str(role).strip().lower(), {}))


class CargoService:
    """CRUD orchestration for cargo records.

    Raises :class:`CargoConflict` (duplicate container) and :class:`CargoNotFound`
    (absent container); the router maps these to 409 / 404. Every other failure
    propagates as-is (the router maps to 500).
    """

    def __init__(self, dsn: Optional[str] = None, repository: Optional[CargoRepository] = None) -> None:
        self._repo = repository or CargoRepository(dsn)

    @staticmethod
    def _ms(t0: float) -> float:
        return round((perf_counter() - t0) * 1000, 1)

    def _observe(self, op: str, status: str, t0: float, *, container: Optional[str] = None) -> None:
        log.info("cargo.service", module="cargo", operation=op, status=status,
                 container_number=container, latency_ms=self._ms(t0))

    # ------------------------------------------------------------------ events
    async def _emit(self, event: str, container_number: str,
                    payload: Mapping[str, Any]) -> None:
        """Append a lifecycle event to the notifications log. Best-effort: a
        failure here (e.g. the events table missing on an un-migrated DB) is logged
        and swallowed so it can NEVER fail the underlying cargo mutation. Only the
        repository is asked to record — the repo may be a fake in tests."""
        recorder = getattr(self._repo, "record_event", None)
        if recorder is not None:
            try:
                await recorder(event, container_number, payload)
            except Exception as exc:  # noqa: BLE001 — never let notification I/O break CRUD
                log.warning("cargo.event.record_failed", event=event,
                            container_number=container_number, error=str(exc))
        # Distribute the milestone (Kafka + WS) so downstream twins/screens do not
        # have to poll the event table. Best-effort by construction: lifecycle_bus
        # swallows every failure, and the DB row above is the source of truth.
        if event in _BUS_EVENTS:
            try:
                from services.lifecycle_bus import publish
                await publish(event, {"container_number": container_number, **dict(payload)})
            except Exception as exc:  # noqa: BLE001
                log.warning("cargo.event.publish_failed", event=event, error=str(exc))

    @staticmethod
    def _derive_update_events(old: Mapping[str, Any],
                              new: Mapping[str, Any]) -> list[tuple[str, dict]]:
        """Map an old->new cargo diff to the specific lifecycle events it implies.

        A single PUT can trigger several (e.g. cleared + released + yarded). If no
        specific transition matched but something changed, a generic cargo.updated
        is emitted so every mutation is observable."""
        events: list[tuple[str, dict]] = []
        if not old.get("is_released") and new.get("is_released"):
            events.append((EVENT_RELEASED, {"is_released": True}))
        if old.get("customs_status") != new.get("customs_status"):
            payload = {"customs_status": new.get("customs_status"),
                       "previous_customs_status": old.get("customs_status")}
            # Legacy topic (unchanged) + the new granular one, both additive.
            events.append((EVENT_STATUS_CHANGED, dict(payload)))
            events.append((EVENT_CUSTOMS_STATUS_CHANGED, dict(payload)))
        if old.get("yard_block") != new.get("yard_block") and new.get("yard_block"):
            events.append((EVENT_YARD_ASSIGNED, {"yard_block": new.get("yard_block")}))
        old_gate, new_gate = old.get("gate"), new.get("gate")
        if old_gate != new_gate:
            # Legacy gate_movement fires on any transition to a gate (unchanged);
            # the new gate_in / gate_out classify the direction of the movement.
            if new_gate:
                events.append((EVENT_GATE_MOVEMENT, {
                    "gate": new_gate, "previous_gate": old_gate}))
            if not old_gate and new_gate:
                events.append((EVENT_GATE_IN, {"gate": new_gate}))
            elif old_gate and not new_gate:
                events.append((EVENT_GATE_OUT, {"previous_gate": old_gate}))
        if not events:
            events.append((EVENT_UPDATED, {}))
        return events

    async def list_events(
        self,
        *,
        container_number: Optional[str] = None,
        event: Optional[str] = None,
        since_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Recent cargo lifecycle events (newest first) for the UC-2 notifications
        poll. Returns [] when the repo has no event log (e.g. a fake without one)."""
        lister = getattr(self._repo, "list_events", None)
        if lister is None:
            return []
        return await lister(container_number=container_number, event=event,
                            since_id=since_id, limit=limit, offset=offset)

    # ------------------------------------------------------------------ create
    async def create_cargo(self, row: Mapping[str, Any], *,
                           actor_role: Optional[str] = None) -> dict:
        """Create a cargo record. The repository writes the container's opening
        lifecycle audit row (NULL -> CREATED, action CREATE) inside the same
        transaction, so a container never exists without an audit trail.

        A create always lands on CREATED (the column DEFAULT), so ``is_released``
        may NOT be true here — that pair is the exact inconsistency migration 0115
        had to backfill away (5 rows with is_released=true, lifecycle CREATED).
        The PUT path has enforced this since 0115; the POST path did not, which is
        audit finding W1. Rejected as an illegal CREATED -> RELEASED transition, so
        the router renders the same 409 envelope as every other lifecycle refusal."""
        t0 = perf_counter()
        if row.get("is_released"):
            self._observe("create", "illegal_transition", t0,
                          container=row.get("container_number"))
            raise CargoTransitionError(
                str(row.get("container_number")), LC_CREATED, LC_RELEASED)
        try:
            out = await self._repo.create(row, actor_role=actor_role)
        except CargoConflict:
            self._observe("create", "conflict", t0, container=row.get("container_number"))
            raise
        self._observe("create", "success", t0, container=out.get("container_number"))
        await self._emit(EVENT_CREATED, out.get("container_number"), {
            "customs_status": out.get("customs_status"),
            "is_released": out.get("is_released"),
            "origin_stream": out.get("origin_stream"),
        })
        return out

    # -------------------------------------------------------------------- read
    async def get_cargo(self, container_number: str) -> Optional[dict]:
        t0 = perf_counter()
        out = await self._repo.get(container_number)
        self._observe("get", "success" if out else "not_found", t0, container=container_number)
        return out

    async def list_cargo(
        self,
        *,
        container_number: Optional[str] = None,
        customs_status: Optional[str] = None,
        yard_block: Optional[str] = None,
        is_released: Optional[bool] = None,
        vehicle_number: Optional[str] = None,
        eseal_status: Optional[str] = None,
        pre_document_status: Optional[str] = None,
        origin_stream: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        t0 = perf_counter()
        out = await self._repo.list(
            container_number=container_number, customs_status=customs_status,
            yard_block=yard_block, is_released=is_released,
            vehicle_number=vehicle_number, eseal_status=eseal_status,
            pre_document_status=pre_document_status, origin_stream=origin_stream,
            lifecycle_status=lifecycle_status,
            limit=limit, offset=offset,
        )
        self._observe("list", "success", t0)
        return out

    async def count_cargo(
        self,
        *,
        container_number: Optional[str] = None,
        customs_status: Optional[str] = None,
        yard_block: Optional[str] = None,
        is_released: Optional[bool] = None,
        vehicle_number: Optional[str] = None,
        eseal_status: Optional[str] = None,
        pre_document_status: Optional[str] = None,
        origin_stream: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
    ) -> int:
        return await self._repo.count(
            container_number=container_number, customs_status=customs_status,
            yard_block=yard_block, is_released=is_released,
            vehicle_number=vehicle_number, eseal_status=eseal_status,
            pre_document_status=pre_document_status, origin_stream=origin_stream,
            lifecycle_status=lifecycle_status,
        )

    # ------------------------------------------------------------------ update
    async def update_cargo(self, container_number: str, fields: Mapping[str, Any]) -> dict:
        """Patch a cargo record. A PUT that flips ``is_released`` -> true IS a
        release and faces the same VERIFY gate as POST /release.

        Audit W3: that gate used to be a read-then-check-then-write with no row
        lock, so two concurrent ``PUT {is_released:true}`` calls could both read a
        pre-release snapshot and both pass. The release branch now routes the whole
        patch through :meth:`_advance`, where the repository evaluates the gate
        *under* ``SELECT … FOR UPDATE`` and writes the columns in the same
        statement — one commit, one winner, and the loser gets a 409."""
        t0 = perf_counter()
        # Snapshot the pre-image so the diff can be turned into specific lifecycle
        # events (released / status_changed / yard_assigned / gate_movement).
        old = await self._repo.get(container_number) or {}
        releasing = bool(fields.get("is_released")) and not old.get("is_released")
        if releasing:
            if not old:
                self._observe("update", "not_found", t0, container=container_number)
                raise CargoNotFound(container_number)
            try:
                # strict=True: the repository raises CargoTransitionError from
                # inside the lock when the container has not passed VERIFY, and
                # CargoNotFound if it vanished between the snapshot and the lock.
                out = await self._advance(
                    container_number, target=LC_RELEASED, action="RELEASE",
                    set_fields=fields, blocked_customs=CUSTOMS_BLOCKS_RELEASE)
            except CargoTransitionError:
                self._observe("update", "illegal_transition", t0, container=container_number)
                raise
            except CargoCustomsBlocked:
                # Same customs gate as POST /release — otherwise the guard would be
                # bypassable by patching is_released instead of calling release.
                self._observe("update", "customs_not_cleared", t0, container=container_number)
                raise
        else:
            try:
                out = await self._repo.update(container_number, fields)
            except CargoNotFound:
                self._observe("update", "not_found", t0, container=container_number)
                raise
        self._observe("update", "success", t0, container=container_number)
        # cargo.lifecycle_changed already fired inside _advance on the release
        # branch; these are the business-diff topics (released / status_changed /
        # yard_assigned / gate_movement) and fire on both branches unchanged.
        for event, payload in self._derive_update_events(old, out):
            await self._emit(event, container_number, payload)
        return out

    # ------------------------------------------------------------------ delete
    async def delete_cargo(self, container_number: str) -> bool:
        t0 = perf_counter()
        removed = await self._repo.delete(container_number)
        self._observe("delete", "success" if removed else "not_found", t0, container=container_number)
        if removed:
            await self._emit(EVENT_DELETED, container_number, {})
        return removed

    # ------------------------------------------------------- lifecycle (0023)
    async def _advance(self, container_number: str, *, target: str, action: str,
                       allowed_from: Optional[set[str]] = None, strict: bool = True,
                       actor_role: Optional[str] = None,
                       note: Optional[str] = None,
                       set_fields: Optional[Mapping[str, Any]] = None,
                       blocked_customs: Optional[frozenset[str]] = None) -> Optional[dict]:
        """Drive one lifecycle transition through the repository (atomic + audited)
        and, when applied, emit ``cargo.lifecycle_changed``. Returns the updated
        cargo row, or ``None`` when a best-effort (``strict=False``) transition was
        not applicable. In ``strict`` mode the repository raises
        :class:`CargoNotFound` / :class:`CargoTransitionError`, which the router maps
        to 404 / 409. ``allowed_from`` defaults to the state machine's legal
        predecessors of ``target``; callers pass a custom set only for the lenient
        legacy paths (yard-assign / reefer / rake / PUT-release).

        ``set_fields`` patches business columns inside the SAME locked transaction
        as the status change (audit W2/W3) — used by the release paths so
        ``is_released`` and ``lifecycle_status`` commit together and the gate is
        evaluated under the row lock."""
        af = allowed_from if allowed_from is not None else allowed_predecessors(target)
        row = await self._repo.transition_lifecycle(
            container_number, target=target, allowed_from=af, action=action,
            actor_role=actor_role, note=note, strict=strict, set_fields=set_fields,
            blocked_customs=blocked_customs)
        if row is None:
            return None
        old = row.pop("_old_status", None)
        new = row.pop("_new_status", target)
        await self._emit(EVENT_LIFECYCLE_CHANGED, container_number,
                         {"from": old, "to": new, "action": action})
        return row

    async def discharge_cargo(self, container_number: str, *,
                              vessel_name: Optional[str] = None,
                              discharge_time: Any = None,
                              actor_role: Optional[str] = None) -> dict:
        """Mark a container discharged from the vessel: CREATED -> VESSEL_DISCHARGED
        (task #2). Does NOT auto-assign a yard. Emits ``cargo.vessel_discharged``.
        404 if the container is unknown; 409 if it is not in a dischargeable state."""
        t0 = perf_counter()
        row = await self._advance(container_number, target=LC_VESSEL_DISCHARGED,
                                  action="DISCHARGE", actor_role=actor_role,
                                  note=vessel_name)
        if vessel_name:  # persist the discharging vessel on the record (additive)
            row = await self._repo.update(container_number, {"vessel_name": vessel_name})
        payload: dict[str, Any] = {}
        if vessel_name:
            payload["vessel_name"] = vessel_name
        if discharge_time is not None:
            payload["discharge_time"] = (
                discharge_time.isoformat() if hasattr(discharge_time, "isoformat")
                else str(discharge_time))
        await self._emit(EVENT_VESSEL_DISCHARGED, container_number, payload)
        self._observe("discharge", "success", t0, container=container_number)
        return row

    async def record_pendency(self, container_number: str, *,
                              reason: Optional[str] = None,
                              actor_role: Optional[str] = None) -> dict:
        """Record that a discharged container is PENDING evacuation (Phase 5).

        VESSEL_DISCHARGED -> PENDENCY. Optional by design: a container may still go
        straight to YARD_ASSIGNED, so every existing flow is unaffected. Emits
        ``cargo.pendency_recorded``. 404 if unknown; 409 if not discharged."""
        t0 = perf_counter()
        row = await self._advance(container_number, target=LC_PENDENCY,
                                  action="PENDENCY", actor_role=actor_role,
                                  note=reason)
        await self._emit(EVENT_PENDENCY_RECORDED, container_number,
                         {"status": LC_PENDENCY, "reason": reason})
        self._observe("pendency", "success", t0, container=container_number)
        return row

    async def assign_yard(self, container_number: str, yard_block: str, *,
                          actor_role: Optional[str] = None) -> dict:
        """Yard-assignment write (task #3). Sets ``yard_block`` via the same
        update path (so ``cargo.yard_assigned`` still fires) and best-effort advances
        the lifecycle to YARD_ASSIGNED. Lenient on lifecycle for backward
        compatibility (a container created straight to yard-assign still works)."""
        t0 = perf_counter()
        row = await self.update_cargo(container_number, {"yard_block": yard_block})
        await self._advance(container_number, target=LC_YARD_ASSIGNED,
                            action="YARD_ASSIGN", strict=False,
                            allowed_from={LC_CREATED, LC_VESSEL_DISCHARGED, LC_PENDENCY},
                            actor_role=actor_role)
        self._observe("yard_assign", "success", t0, container=container_number)
        return row

    async def allocate_yard_position(self, container_number: str, *,
                                     yard_block: str, yard_row: Optional[str] = None,
                                     yard_slot: Optional[str] = None,
                                     yard_position: Optional[str] = None,
                                     priority: str = "MEDIUM",
                                     actor_role: Optional[str] = None) -> dict:
        """Allocate a physical yard position (block / row / slot / position) for a
        container (task #4). Requires the container to exist (404). Records a
        position row and best-effort advances the lifecycle to
        YARD_POSITION_ALLOCATED. Always emits ``cargo.yard_position_allocated``."""
        t0 = perf_counter()
        existing = await self._repo.get(container_number)
        if existing is None:
            raise CargoNotFound(container_number)
        plan = await self._repo.create_yard_position(
            container_number, assigned_block=yard_block, yard_row=yard_row,
            yard_slot=yard_slot, yard_position=yard_position, priority=priority)
        await self._advance(container_number, target=LC_YARD_POSITION_ALLOCATED,
                            action="YARD_POSITION", strict=False,
                            allowed_from={LC_YARD_ASSIGNED, LC_YARD_POSITION_ALLOCATED},
                            actor_role=actor_role)
        await self._emit(EVENT_YARD_POSITION_ALLOCATED, container_number, {
            "yard_block": yard_block, "row": yard_row, "slot": yard_slot,
            "position": yard_position})
        self._observe("yard_position", "success", t0, container=container_number)
        return plan

    async def scan_queue(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """Containers awaiting a customs scan (task #5): yard-assigned, not released,
        not yet verified. The router labels each row status ``SCAN_PENDING``."""
        return await self._repo.list_scan_queue(limit=limit, offset=offset)

    async def verify_cargo(self, container_number: str, *, verified: bool = True,
                           remarks: Optional[str] = None,
                           actor_role: Optional[str] = None) -> dict:
        """Record a customs/scan verification (task #6). When ``verified`` is true,
        advances the lifecycle to VERIFIED (requires yard-assignment; 409 otherwise)
        and emits ``cargo.verified``. When false, records the failed check without
        advancing. 404 if the container is unknown."""
        t0 = perf_counter()
        if verified:
            row = await self._advance(container_number, target=LC_VERIFIED,
                                      action="VERIFY", actor_role=actor_role,
                                      note=remarks)
        else:
            row = await self._repo.get(container_number)
            if row is None:
                raise CargoNotFound(container_number)
        await self._repo.record_scan_verification(
            container_number, bool(verified), remarks, actor_role)
        await self._emit(EVENT_VERIFIED, container_number,
                         {"verified": bool(verified), "remarks": remarks})
        self._observe("verify", "success" if verified else "rejected", t0,
                      container=container_number)
        return row

    async def release_cargo(self, container_number: str, *,
                            actor_role: Optional[str] = None,
                            note: Optional[str] = None) -> dict:
        """Validated release (task #7): requires the lifecycle to be VERIFIED
        (which itself requires yard-assignment), so release-before-verification is a
        409 and a duplicate release is a 409. Flips ``is_released`` for legacy
        consumers/filters and emits the UC-III handover ``cargo.released`` event with
        the yard location + vehicle details (task #8). 404 if unknown.

        Audit W2: the status change and the ``is_released`` flag used to commit in
        two separate transactions, so a failure between them left the row RELEASED
        to the state machine but invisible to every ``is_released`` filter. They now
        travel as ``set_fields`` on the single locked UPDATE — one commit, or
        neither."""
        t0 = perf_counter()
        row = await self._advance(container_number, target=LC_RELEASED, action="RELEASE",
                                  actor_role=actor_role, note=note,
                                  set_fields={"is_released": True},
                                  blocked_customs=CUSTOMS_BLOCKS_RELEASE)
        await self._emit(EVENT_RELEASED, container_number, {
            "status": LC_RELEASED,
            "is_released": True,
            "yard_location": row.get("yard_block"),
            "vehicle_details": row.get("vehicle_number"),
        })
        self._observe("release", "success", t0, container=container_number)
        return row

    async def list_lifecycle_history(self, container_number: str, *,
                                     limit: int = 100, offset: int = 0) -> list[dict]:
        """Append-only lifecycle transition audit for one container (task #1)."""
        return await self._repo.list_lifecycle_events(
            container_number, limit=limit, offset=offset)

    # ----------------------------------------------------- notifications (0017)
    async def create_notification(self, *, container_number: str, notification_type: str,
                                  severity: str, message: Optional[str],
                                  stakeholders: Any) -> dict:
        """Persist a stakeholder notification and emit a ``cargo.pendency_created``
        lifecycle event (so a notification is also visible on the events poll)."""
        t0 = perf_counter()
        row = await self._repo.create_notification(
            container_number, notification_type, severity, message, stakeholders)
        self._observe("notification.create", "success", t0, container=container_number)
        await self._emit(EVENT_PENDENCY_CREATED, container_number, {
            "notification_id": row.get("id"), "notification_type": notification_type,
            "severity": severity})
        return row

    async def list_notifications(self, **filters: Any) -> list[dict]:
        return await self._repo.list_notifications(**filters)

    # --------------------------------------------------------- workflow (0016)
    async def apply_workflow(self, container_number: str, action: str,
                             comment: Optional[str]) -> Optional[dict]:
        """Apply a workflow transition. Returns the stored workflow-event row, or
        ``None`` if the container is unknown (router -> 404). ``action`` is already
        validated (TRIGGER / APPROVE / REJECT) at the DTO layer."""
        t0 = perf_counter()
        new_status = WORKFLOW_TRANSITIONS[action]
        row = await self._repo.record_workflow(container_number, action, new_status, comment)
        self._observe("workflow", "success" if row else "not_found", t0,
                      container=container_number)
        return row

    async def list_workflow_history(self, container_number: str, *,
                                    limit: int = 100, offset: int = 0) -> list[dict]:
        return await self._repo.list_workflow_history(
            container_number, limit=limit, offset=offset)

    # ------------------------------------------------------- planning (0018)
    async def plan_yard(self, *, container_number: str, preferred_block: str,
                        priority: str) -> dict:
        """Allocate the next free slot in the preferred block (derived from live
        occupancy + prior plans) and record the plan. Emits ``cargo.queue_updated``."""
        t0 = perf_counter()
        slot = await self._repo.next_yard_slot(preferred_block)
        assigned_block = f"{preferred_block}-{slot:02d}"
        row = await self._repo.create_yard_plan(
            container_number, preferred_block, assigned_block, priority)
        self._observe("yard_plan", "success", t0, container=container_number)
        await self._emit(EVENT_QUEUE_UPDATED, container_number, {
            "assigned_block": assigned_block, "priority": priority})
        return row

    async def optimize_yard(self) -> dict:
        """Compute a yard congestion score + move recommendations from the live
        core.cargo yard occupancy. Deterministic: groups containers by block
        letter-zone; recommends relieving the busiest zone (keep one, move the rest).

        Capacity comes from ``core.yard_block`` (migration 0130) when the master is
        populated. Before this the denominator was a hardcoded nominal 10 with
        nothing in the response saying so — audit finding Y1, "a figure a JNPA
        evaluator could not trace". Every zone that falls back to the nominal value
        is now named in ``assumptions``, so the score is either sourced or declared.
        The response is a superset of the previous one: existing keys are unchanged."""
        rows = await self._repo.list_yarded_containers()
        capacity_getter = getattr(self._repo, "yard_block_capacity", None)
        capacities: dict[str, int] = await capacity_getter() if capacity_getter else {}
        zones: dict[str, list[str]] = {}
        for r in rows:
            yb = r.get("yard_block")
            if not yb:
                continue
            zone = str(yb).split("-", 1)[0]
            zones.setdefault(zone, []).append(r["container_number"])
        if not zones:
            return {"yard_congestion": 0.0, "recommendations": [],
                    "priority_containers": [], "capacity_source": "NONE",
                    "assumptions": []}

        # Per-zone capacity: the master's own row, else the sum of the block rows
        # that belong to the zone (block_code 'A-01' -> zone 'A'), else nominal.
        assumptions: list[dict[str, Any]] = []
        zone_capacity: dict[str, int] = {}
        for zone in zones:
            cap = capacities.get(zone)
            if cap is None:
                cap = sum(v for k, v in capacities.items()
                          if str(k).split("-", 1)[0] == zone) or None
            if cap is None:
                cap = _YARD_BLOCK_CAPACITY
                assumptions.append({
                    "field": f"yard_block_capacity[{zone}]",
                    "value": _YARD_BLOCK_CAPACITY,
                    "reason": ("no row in core.yard_block for this zone; nominal "
                               "per-block slot count assumed"),
                    "source": "ASSUMED (services.cargo.service._YARD_BLOCK_CAPACITY)",
                })
            zone_capacity[zone] = int(cap)

        total = sum(len(v) for v in zones.values())
        total_capacity = sum(zone_capacity.values()) or 1
        congestion = round(min(1.0, total / total_capacity), 2)
        # Per-zone utilisation, so the busiest zone is the most *saturated* one
        # rather than merely the most populated — a 9/10 block matters more than a
        # 12/500 one. Ties broken by zone name for determinism (unchanged contract).
        utilisation = {z: len(c) / zone_capacity[z] for z, c in zones.items()}
        busiest_zone = max(zones, key=lambda z: (utilisation[z], len(zones[z]), z))
        busiest = zones[busiest_zone]
        # Move only the overflow above capacity; when the zone is within capacity
        # keep the legacy behaviour (keep one, move the rest) so the endpoint still
        # returns a recommendation for the small demo dataset.
        over = len(busiest) - zone_capacity[busiest_zone]
        movers = busiest[-over:] if over > 0 else (busiest[1:] if len(busiest) >= 2 else [])
        reason = ("over block capacity" if over > 0 else "reduce congestion")
        recommendations = [
            {"container_number": cn, "action": "MOVE", "reason": reason}
            for cn in movers
        ]
        return {
            "yard_congestion": congestion,
            "recommendations": recommendations,
            "priority_containers": movers,
            "busiest_block": busiest_zone,
            "occupied": total,
            "capacity": total_capacity,
            "capacity_source": "core.yard_block" if capacities else "ASSUMED",
            "block_utilisation": {z: round(u, 2) for z, u in utilisation.items()},
            "assumptions": assumptions,
        }

    async def plan_rake(self, *, rake_id: str, containers: Any) -> dict:
        """Group containers onto a rail rake. Emits one ``cargo.queue_updated`` per
        container so the assignment is visible on the events poll."""
        t0 = perf_counter()
        items = list(containers or [])
        row = await self._repo.create_rake_plan(rake_id, items)
        self._observe("rake_plan", "success", t0)
        for cn in items:
            await self._emit(EVENT_QUEUE_UPDATED, cn, {"rake_id": rake_id})
            # Best-effort lifecycle advance to RAKE_ASSIGNED (optional state) +
            # the specific topic. Never fails planning if the box isn't yarded yet.
            await self._advance(cn, target=LC_RAKE_ASSIGNED, action="RAKE_ASSIGN",
                                strict=False)
            await self._emit(EVENT_RAKE_ASSIGNED, cn, {"rake_id": rake_id})
        return row

    async def list_rake_plans(self, **filters: Any) -> list[dict]:
        return await self._repo.list_rake_plans(**filters)

    async def plan_reefer(self, *, container_number: str, temperature: Any,
                          power_required: bool) -> dict:
        """Allocate the next powered reefer slot (REEFER-A<n>) for a container."""
        t0 = perf_counter()
        idx = await self._repo.next_reefer_index()
        slot = f"REEFER-A{idx:02d}"
        row = await self._repo.create_reefer_plan(
            container_number, temperature, power_required, slot)
        # Best-effort lifecycle advance to REEFER_PLANNED (optional state) + topic.
        await self._advance(container_number, target=LC_REEFER_PLANNED,
                            action="REEFER_PLAN", strict=False)
        await self._emit(EVENT_REEFER_PLANNED, container_number,
                         {"slot": slot, "temperature": temperature,
                          "power_required": power_required})
        self._observe("reefer_plan", "success", t0, container=container_number)
        return row
