"""Container Job service — the UC-III lifecycle spine.

Owns the assignment validation chain, the job state machine, and the gate / yard /
scan actions that advance it. Thin over :class:`ContainerJobRepository` (all SQL
lives there); stateless apart from the DSN, mirroring services.cargo.

Job state machine (forward-only; CANCELLED reachable from any non-terminal state):

    ASSIGNED -> ACCEPTED -> AT_GATE -> IN_YARD -> PICKED_UP|DROPPED -> COMPLETED
                                                              \\-> CANCELLED

Assignment pre-conditions (all enforced, all previously missing):
    vehicle exists · vehicle ACTIVE · vehicle has no open job · driver exists ·
    driver PDP permit valid · transporter not blacklisted · container has no open job
"""
from __future__ import annotations

import re
from datetime import date
from time import perf_counter
from typing import Any, Dict, Mapping, Optional

from jnpa_shared.iso6346 import is_valid_container_no
from services.cargo.service import CUSTOMS_BLOCKS_RELEASE
from jnpa_shared.logging import get_logger

from .repository import ContainerJobRepository, CustomsFlagged, JobConflict

log = get_logger("services.container_job.service")

# ---------------------------------------------------------------- state machine
STATUSES = ("ASSIGNED", "ACCEPTED", "AT_GATE", "IN_YARD", "PICKED_UP", "DROPPED",
            "COMPLETED", "CANCELLED")
TERMINAL = frozenset({"COMPLETED", "CANCELLED"})
OPEN_STATES = frozenset(set(STATUSES) - TERMINAL)

# target status -> statuses it may be entered from
TRANSITIONS: Dict[str, frozenset[str]] = {
    "ACCEPTED": frozenset({"ASSIGNED"}),
    "AT_GATE": frozenset({"ASSIGNED", "ACCEPTED"}),
    "IN_YARD": frozenset({"AT_GATE"}),
    "PICKED_UP": frozenset({"IN_YARD"}),
    "DROPPED": frozenset({"IN_YARD", "PICKED_UP"}),
    "COMPLETED": frozenset({"PICKED_UP", "DROPPED", "AT_GATE", "IN_YARD"}),
    "CANCELLED": OPEN_STATES,
}

MOVE_TYPES = ("IMPORT_PICK", "EXPORT_DROP", "EMPTY_PICK", "EMPTY_DROP")


# SQL normalisation of a human identifier (registration plate / licence number),
# byte-for-byte the rule normalize_plate() and normalize_licence() apply in
# Python: strip everything that is not alphanumeric, upper-case the rest. Written
# once so the two can never drift.
SQL_NORMALISE = "upper(regexp_replace(coalesce({col}, ''), '[^A-Za-z0-9]', '', 'g'))"


def open_job_not_exists(master_column: str, *, job_column: str,
                        alias: str = "j",
                        master_identity: Optional[str] = None,
                        job_identity: Optional[str] = None) -> tuple[str, dict]:
    """``NOT EXISTS (open job on this resource)`` SQL fragment + bound params.

    "Occupied" is a DATABASE fact about core.container_job_assignment, and it is
    the SAME fact for a truck and for a driver: a row on this resource whose
    status is not terminal. Every availability query (gateway.fleet for
    core.vehicle, gateway.enrollment for core.driver_identity) and every
    assignment validation builds its exclusion from this ONE definition, derived
    from :data:`TERMINAL`, so a new job status can never make a busy resource
    look free in one place and not another.

    Terminal statuses (COMPLETED / CANCELLED) are excluded from the correlation,
    which is what frees the resource again when a job finishes. Note this is not
    ``driver_id IS NULL``: a completed job keeps its driver_id and must not go on
    occupying them.

    ``master_identity`` / ``job_identity`` widen the match from the master ROW to
    the PHYSICAL RESOURCE. Neither master is keyed on the human identifier —
    core.vehicle is unique on vehicle_id, core.driver_identity on driver_id — so
    the same truck can hold two rows under two Vehicle IDs, and the same person
    two driver records under two Driver IDs (the visible symptom being duplicate
    dropdown entries: one name, one licence, three options). Correlating on the
    surrogate id alone then reports a busy truck as free through its twin row,
    which is exactly what an operator sees as "MH04QA9911 is on a job and still
    in the list". A registration or a licence identifies the resource the yard
    actually dispatches, so an open job against EITHER key occupies it. Compared
    normalised, and only when non-blank on both sides — two rows with no plate on
    file are not thereby the same truck.

    ``master_column`` is the qualified column on the table being filtered (e.g.
    ``core.vehicle.vehicle_id``); ``job_column`` is the assignment column it must
    match. All identifiers are code-supplied, never client input.
    """
    names = sorted(TERMINAL)
    keys = [f"term{i}" for i in range(len(names))]
    placeholders = ", ".join(f":{k}" for k in keys)
    match = f"{alias}.{job_column} = {master_column}"
    if master_identity and job_identity:
        job_norm = SQL_NORMALISE.format(col=f"{alias}.{job_identity}")
        master_norm = SQL_NORMALISE.format(col=master_identity)
        match = (f"({match} OR (NULLIF({job_norm}, '') IS NOT NULL "
                 f"AND {job_norm} = {master_norm}))")
    sql = (f"NOT EXISTS (SELECT 1 FROM core.container_job_assignment {alias} "
           f"WHERE {match} "
           f"AND {alias}.status NOT IN ({placeholders}))")
    return sql, dict(zip(keys, names))

# Pseudo-status for the UC-II -> UC-III handover queue: a container UC-II has
# RELEASED that no truck has been dispatched against yet. It is NOT a job status
# (nothing with this value is ever written to core.container_job_assignment,
# whose CHECK constraint would reject it) — it exists only on the read surface so
# a released box is visible on the UC-III console before a job exists.
PENDING_ASSIGNMENT = "PENDING_ASSIGNMENT"

# "Flagged by Customs" is not a status of its own — core.cargo.customs_status is
# CHECKed to PENDING / CLEARED / HELD / UNDER_INSPECTION and nothing else. The
# dispositions that mean customs has stopped this box are already defined ONCE,
# in the cargo module, as the set that forbids a release; a truck may not be
# dispatched against a box customs is holding or examining for exactly the same
# reason, so this gate imports that definition rather than restating it.
#
# PENDING is deliberately NOT in it: it means customs has said nothing yet, which
# is the state of most of the corpus. Assignment stays open on PENDING.
CUSTOMS_FLAGGED = CUSTOMS_BLOCKS_RELEASE

# The reason line the operator sees, and the full sentence that explains it. Both
# are returned by the API rather than left for each client to restate, so the
# console, the driver PWA and a raw API caller all report the refusal identically.
CUSTOMS_FLAGGED_MESSAGE = "Flagged by Customs"
CUSTOMS_FLAGGED_DETAIL = ("Vehicle and driver assignment is blocked because this "
                          "container is flagged by Customs.")

# BUG-4: move types that may NOT be dispatched without an identified driver.
# An import pick-up leaves the terminal with a laden box against a PIN/Form-13,
# so the trip must be attributable to a person, not just a plate. Every job
# created before this rule carried driver_id = NULL, which left the driver PWA
# with nobody to notify and the audit trail with nobody to name.
DRIVER_REQUIRED_MOVE_TYPES = frozenset({"IMPORT_PICK"})

# Events emitted onto the job history (and, in Phase 6, onto the bus).
EVENT_ASSIGNED = "job.assigned"
EVENT_ACCEPTED = "job.accepted"
EVENT_GATE_IN = "job.gate_in"
EVENT_GATE_OUT = "job.gate_out"
EVENT_IN_YARD = "job.in_yard"
EVENT_YARD_PICKUP = "job.yard_pickup"
EVENT_YARD_DROP = "job.yard_drop"
EVENT_SCAN = "job.scan_recorded"
EVENT_COMPLETED = "job.completed"
EVENT_CANCELLED = "job.cancelled"

# Canonical UC-3 milestone codes. The `job.*` strings above are the wire/history
# names and stay as-is: they are already persisted in core.container_job_event
# and subscribed to on the bus, so renaming them would orphan existing rows.
# Every event instead carries its canonical code alongside, which is what the
# UC-3 lifecycle spec and the dashboards key on. Several wire events collapse
# onto one milestone (both yard legs are JOB_YARD until the box actually moves).
JOB_CREATED = "JOB_CREATED"
JOB_ACCEPTED = "JOB_ACCEPTED"
JOB_GATE_IN = "JOB_GATE_IN"
JOB_GATE_OUT = "JOB_GATE_OUT"
JOB_YARD = "JOB_YARD"
JOB_PICKUP = "JOB_PICKUP"
JOB_DROP = "JOB_DROP"
JOB_SCAN = "JOB_SCAN"
JOB_COMPLETE = "JOB_COMPLETE"
JOB_CANCELLED = "JOB_CANCELLED"

EVENT_CODES: Dict[str, str] = {
    EVENT_ASSIGNED: JOB_CREATED,
    EVENT_ACCEPTED: JOB_ACCEPTED,
    EVENT_GATE_IN: JOB_GATE_IN,
    EVENT_GATE_OUT: JOB_GATE_OUT,
    EVENT_IN_YARD: JOB_YARD,
    EVENT_YARD_PICKUP: JOB_PICKUP,
    EVENT_YARD_DROP: JOB_DROP,
    EVENT_SCAN: JOB_SCAN,
    EVENT_COMPLETED: JOB_COMPLETE,
    EVENT_CANCELLED: JOB_CANCELLED,
}

# Status -> milestone, for the arrival at a state that has no dedicated event
# (AT_GATE/IN_YARD are reached by the gate and movement recorders).
STATUS_CODES: Dict[str, str] = {
    "ASSIGNED": JOB_CREATED, "ACCEPTED": JOB_ACCEPTED, "AT_GATE": JOB_GATE_IN,
    "IN_YARD": JOB_YARD, "PICKED_UP": JOB_PICKUP, "DROPPED": JOB_DROP,
    "COMPLETED": JOB_COMPLETE, "CANCELLED": JOB_CANCELLED,
}


def event_code(event: Optional[str], new_status: Optional[str] = None) -> Optional[str]:
    """Canonical JOB_* milestone for a wire event, falling back to the state
    reached so a row written by an older build still resolves to a milestone."""
    return EVENT_CODES.get(event or "") or STATUS_CODES.get((new_status or "").upper())


def normalize_plate(raw: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def normalize_licence(raw: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


class ValidationFailed(Exception):
    """Assignment pre-condition failure -> HTTP 400 with a machine-readable code."""

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


class ContainerJobService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[ContainerJobRepository] = None) -> None:
        self._repo = repository or ContainerJobRepository(dsn=dsn)

    @staticmethod
    def _ms(t0: float) -> float:
        return round((perf_counter() - t0) * 1000, 1)

    # ================================================================== customs
    async def _customs_refusal(self, container_number: str,
                               customs_status: str) -> ValidationFailed:
        """The ONE customs_flagged refusal, raised from every assignment path.

        Looks the recorded remark up only here — on the refusal path, so the happy
        path costs nothing — and logs the block as a structured event. The note is
        reported when customs left one and omitted when it did not; the reason and
        the explanatory message never depend on it, so an absent remark can never
        produce a blank or confusing reason.
        """
        note = await self._repo.customs_note(container_number)
        log.warning("assignment_blocked_customs_flagged",
                    extra={"container_number": container_number,
                           "customs_status": customs_status,
                           "reason": CUSTOMS_FLAGGED_MESSAGE,
                           "customs_note_recorded": note is not None})
        return ValidationFailed(
            "customs_flagged",
            f"{CUSTOMS_FLAGGED_MESSAGE}: {note}" if note else CUSTOMS_FLAGGED_MESSAGE,
            reason=CUSTOMS_FLAGGED_MESSAGE,
            customs_status=customs_status, customs_note=note,
            container_number=container_number,
            message=CUSTOMS_FLAGGED_DETAIL)

    # ============================================================== validation
    async def validate_assignment(self, *, container_number: Optional[str],
                                  vehicle_id: Optional[str], vehicle_no: Optional[str],
                                  driver_id: Optional[str] = None,
                                  driver_licence: Optional[str] = None,
                                  move_type: str = "IMPORT_PICK",
                                  group_code: Optional[str] = None) -> Dict[str, Any]:
        """Run the full pre-condition chain WITHOUT writing. Returns the resolved
        vehicle/driver/transporter facts the caller then persists.

        Raises :class:`ValidationFailed` on the first failed check so the operator
        gets one precise reason rather than a generic refusal. Pure input checks
        run BEFORE resource lookups, so a malformed request never reports a
        resource conflict (e.g. "vehicle busy") as its reason.
        """
        checks: list[dict] = []

        if move_type not in MOVE_TYPES:
            raise ValidationFailed("invalid_move_type",
                                   f"move_type must be one of {list(MOVE_TYPES)}")
        # BUG-4: a driver is mandatory for the move types that carry a laden box
        # off the terminal. Checked here (a pure input rule) so it reports before
        # any resource lookup, and enforced in the service rather than the router
        # so BOTH /api/jobs/validate and /api/jobs are covered by one rule.
        if move_type in DRIVER_REQUIRED_MOVE_TYPES and not (driver_id or "").strip():
            raise ValidationFailed(
                "driver_required",
                f"{move_type} requires a driver; select the driver assigned to this vehicle",
                move_type=move_type)
        if not (container_number or "").strip() and not (group_code or "").strip():
            raise ValidationFailed("container_or_group_required",
                                   "supply a container_number or a group_code (empty-by-group job)")

        # --- container (optional: empty-by-group jobs carry a group code instead)
        cn = (container_number or "").strip().upper() or None
        if cn:
            if not is_valid_container_no(cn):
                raise ValidationFailed("invalid_container_number",
                                       f"{cn} fails the ISO-6346 check digit")
            open_job = await self._repo.open_job_for_container(cn)
            if open_job:
                raise ValidationFailed("container_already_assigned",
                                       f"{cn} already has open job #{open_job['id']}",
                                       job_id=open_job["id"])
            cargo = await self._repo.cargo_exists(cn)
            if not cargo:
                raise ValidationFailed("container_not_found",
                                       f"{cn} is not in the cargo registry")
            checks.append({"check": "container", "ok": True,
                           "detail": "known to cargo lifecycle",
                           "lifecycle_status": cargo.get("lifecycle_status")})

            # --- customs: a flagged box may not be dispatched at all.
            # This is the pre-flight read (it is also what /api/jobs/validate
            # answers with). assign() re-runs the same rule under the cargo row
            # lock, so a hold landing after this read still refuses the write.
            customs_status = str(cargo.get("customs_status") or "").upper()
            if customs_status in CUSTOMS_FLAGGED:
                raise await self._customs_refusal(cn, customs_status)
            checks.append({"check": "customs", "ok": True,
                           "detail": f"customs_status={customs_status or 'UNKNOWN'}",
                           "customs_status": customs_status})

            # A truck is not dispatched against a box with no paperwork: at least
            # one of the three client gate documents must already reference it.
            docs = await self._repo.document_counts(cn)
            if not docs.get("total"):
                raise ValidationFailed("no_gate_document",
                                       f"{cn} has no FORM13, PIN or EIR on record",
                                       documents=docs)
            checks.append({"check": "gate_document", "ok": True,
                           "detail": ", ".join(f"{k.upper()}={docs[k]}"
                                               for k in ("form13", "pin", "eir")),
                           "documents": docs})

        # --- vehicle: must exist and be ACTIVE
        vehicle = None
        if vehicle_id:
            vehicle = await self._repo.vehicle_by_id(vehicle_id)
        elif vehicle_no:
            vehicle = await self._repo.vehicle_by_plate(normalize_plate(vehicle_no))
        if vehicle is None:
            raise ValidationFailed("vehicle_not_found",
                                   f"vehicle {vehicle_id or vehicle_no} is not in the fleet registry")
        if (vehicle.get("status") or "").upper() != "ACTIVE":
            raise ValidationFailed("vehicle_not_active",
                                   f"vehicle {vehicle['vehicle_id']} is {vehicle.get('status')}",
                                   vehicle_status=vehicle.get("status"))
        checks.append({"check": "vehicle", "ok": True,
                       "detail": f"{vehicle['vehicle_id']} ACTIVE"})

        # --- vehicle must not already hold an open job
        open_v = await self._repo.open_job_for_vehicle(vehicle["vehicle_id"],
                                                       vehicle.get("vehicle_no"))
        if open_v:
            raise ValidationFailed("vehicle_already_assigned",
                                   f"vehicle {vehicle['vehicle_id']} already holds open job "
                                   f"#{open_v['id']} ({open_v['status']})",
                                   job_id=open_v["id"])
        checks.append({"check": "vehicle_availability", "ok": True, "detail": "no open job"})

        # --- driver: identity (enrollment) and/or master licence
        driver = None
        if driver_id:
            driver = await self._repo.driver_identity(driver_id)
            if driver is None:
                raise ValidationFailed("driver_not_found",
                                       f"driver {driver_id} is not enrolled")
            if (driver.get("status") or "").upper() != "ACTIVE":
                raise ValidationFailed("driver_not_active",
                                       f"driver {driver_id} is {driver.get('status')}")
            driver_licence = driver_licence or driver.get("license_no")
            checks.append({"check": "driver", "ok": True, "detail": f"{driver_id} ACTIVE"})

            # --- driver must not already hold an open job.
            # The symmetric half of the vehicle rule above, and the reason the
            # availability list can be trusted: a driver submitted directly to
            # the API — bypassing the dropdown entirely — is refused here, not
            # merely hidden from the console. One person cannot drive two trucks.
            open_d = await self._repo.open_job_for_driver(driver_id, driver_licence)
            if open_d:
                raise ValidationFailed("driver_already_assigned",
                                       f"driver {driver_id} already holds open job "
                                       f"#{open_d['id']} ({open_d['status']})",
                                       job_id=open_d["id"])
            checks.append({"check": "driver_availability", "ok": True, "detail": "no open job"})

        # --- PDP permit: the ACTUAL permit decides (never the licence date alone)
        permit = None
        if driver_licence:
            ln = normalize_licence(driver_licence)
            permit = await self._repo.driver_permit(ln)
            if permit is None:
                raise ValidationFailed("driver_not_in_master",
                                       f"licence {driver_licence} is not in the driver master")
            pdp_active = permit.get("pdp_active")
            pdp_validity = permit.get("pdp_validity")
            if pdp_active is False:
                raise ValidationFailed("pdp_inactive",
                                       f"PDP permit {permit.get('pdp_number')} is cancelled/inactive",
                                       pdp_number=permit.get("pdp_number"),
                                       cancelled_by=permit.get("cancelled_by"))
            if pdp_validity is not None and pdp_validity < date.today():
                raise ValidationFailed("pdp_expired",
                                       f"PDP permit {permit.get('pdp_number')} expired on {pdp_validity}",
                                       pdp_number=permit.get("pdp_number"),
                                       pdp_validity=str(pdp_validity))
            if pdp_active is None and permit.get("licence_valid_to") is not None \
                    and permit["licence_valid_to"] < date.today():
                # No permit row at all -> fall back to the licence date.
                raise ValidationFailed("licence_expired",
                                       f"driving licence expired on {permit['licence_valid_to']}")
            checks.append({"check": "pdp_permit", "ok": True,
                           "detail": (f"permit {permit.get('pdp_number')} valid"
                                      if pdp_active else "no permit row; licence in date"),
                           "pdp_number": permit.get("pdp_number"),
                           "pdp_validity": (str(pdp_validity) if pdp_validity else None)})

        # --- transporter blacklist
        transporter_id = (permit or {}).get("transporter_id")
        bl = await self._repo.transporter_blacklisted(
            transporter_id=transporter_id,
            vehicle_no=normalize_plate(vehicle.get("vehicle_no")))
        if bl:
            raise ValidationFailed("transporter_blacklisted",
                                   f"transporter {bl.get('transporter_name')} is blacklisted: "
                                   f"{bl.get('reason')}", severity=bl.get("severity"))
        checks.append({"check": "transporter", "ok": True, "detail": "not blacklisted"})

        return {"ok": True, "checks": checks, "vehicle": vehicle, "driver": driver,
                "permit": permit, "transporter_id": transporter_id,
                "container_number": cn}

    # ================================================================== assign
    async def assign(self, *, container_number: Optional[str], vehicle_id: Optional[str] = None,
                     vehicle_no: Optional[str] = None, driver_id: Optional[str] = None,
                     driver_licence: Optional[str] = None, move_type: str,
                     group_code: Optional[str] = None, document_type: Optional[str] = None,
                     document_reference: Optional[str] = None, terminal: Optional[str] = None,
                     gate: Optional[str] = None, notes: Optional[str] = None,
                     actor: Optional[str] = None, actor_role: Optional[str] = None) -> Dict[str, Any]:
        """Validate then create the assignment. The only supported way to bind a
        truck+driver to a container job."""
        t0 = perf_counter()
        v = await self.validate_assignment(
            container_number=container_number, vehicle_id=vehicle_id, vehicle_no=vehicle_no,
            driver_id=driver_id, driver_licence=driver_licence, move_type=move_type,
            group_code=group_code)
        rec = {
            "container_number": v["container_number"], "group_code": group_code,
            "transporter_id": v["transporter_id"],
            "vehicle_id": v["vehicle"]["vehicle_id"], "vehicle_no": v["vehicle"].get("vehicle_no"),
            "driver_id": driver_id,
            "driver_licence": driver_licence or (v["driver"] or {}).get("license_no"),
            "move_type": move_type, "document_type": document_type,
            "document_reference": document_reference, "terminal": terminal, "gate": gate,
            "assigned_by": actor, "actor_role": actor_role, "notes": notes,
        }
        try:
            job = await self._repo.create_job(rec, blocked_customs=CUSTOMS_FLAGGED)
        except CustomsFlagged as exc:
            # Customs stopped the box between the pre-flight read above and the
            # INSERT. The transaction rolled back, so no job and no history row
            # exist; the operator gets the same refusal the pre-flight raises.
            raise await self._customs_refusal(exc.container_number, exc.customs_status) from exc
        await self._publish(EVENT_ASSIGNED, job)
        log.info("job.assigned", extra={"job_id": job["id"], "container": job["container_number"],
                                        "vehicle": job["vehicle_id"], "ms": self._ms(t0)})
        return {"job": job, "checks": v["checks"]}

    # ============================================================ transitions
    async def _advance(self, job_id: int, *, new_status: str, event: str,
                       actor: Optional[str], actor_role: Optional[str],
                       detail: Optional[Mapping[str, Any]] = None,
                       stamp: Optional[str] = None) -> Dict[str, Any]:
        res = await self._repo.transition(
            job_id, new_status=new_status, allowed_from=set(TRANSITIONS[new_status]),
            event=event, actor=actor, actor_role=actor_role, detail=detail, stamp=stamp)
        if res.get("missing"):
            raise JobConflict("job_not_found", f"job #{job_id} does not exist")
        if not res["ok"]:
            raise JobConflict("illegal_transition",
                              f"job #{job_id} is {res['current_status']}; "
                              f"{new_status} requires one of {sorted(TRANSITIONS[new_status])}")
        await self._publish(event, res["job"], detail)
        return res["job"]

    async def _publish(self, event: str, job: Mapping[str, Any],
                       detail: Optional[Mapping[str, Any]] = None) -> None:
        """Distribute a job milestone (Kafka + WS). Best-effort; never raises."""
        try:
            from services.lifecycle_bus import publish
            await publish(event, {
                "event_code": event_code(event, job.get("status")),
                "job_id": job.get("id"), "container_number": job.get("container_number"),
                "vehicle_id": job.get("vehicle_id"), "vehicle_no": job.get("vehicle_no"),
                "driver_id": job.get("driver_id"), "status": job.get("status"),
                "move_type": job.get("move_type"), "terminal": job.get("terminal"),
                **({"detail": dict(detail)} if detail else {}),
            })
        except Exception as exc:  # noqa: BLE001 — a bus failure must not fail the job
            log.warning("job.publish_failed", extra={"event": event, "error": str(exc)})

    async def accept(self, job_id: int, *, actor=None, actor_role=None) -> Dict[str, Any]:
        return await self._advance(job_id, new_status="ACCEPTED", event=EVENT_ACCEPTED,
                                   actor=actor, actor_role=actor_role, stamp="accepted_at")

    async def complete(self, job_id: int, *, actor=None, actor_role=None,
                       notes: Optional[str] = None) -> Dict[str, Any]:
        return await self._advance(job_id, new_status="COMPLETED", event=EVENT_COMPLETED,
                                   actor=actor, actor_role=actor_role,
                                   detail={"notes": notes} if notes else None,
                                   stamp="completed_at")

    async def cancel(self, job_id: int, *, reason: str, actor=None, actor_role=None) -> Dict[str, Any]:
        return await self._advance(job_id, new_status="CANCELLED", event=EVENT_CANCELLED,
                                   actor=actor, actor_role=actor_role,
                                   detail={"reason": reason})

    # ============================================================ gate events
    async def record_gate_event(self, *, event_type: str, plate: str,
                                gate_id: Optional[str] = None, job_id: Optional[int] = None,
                                container_number: Optional[str] = None,
                                bat_lane: Optional[str] = None,
                                document_type: Optional[str] = None,
                                document_reference: Optional[str] = None,
                                driver_id: Optional[str] = None,
                                device_id: Optional[str] = None, ts=None,
                                lat: Optional[float] = None, lon: Optional[float] = None,
                                actor: Optional[str] = None,
                                actor_role: Optional[str] = None) -> Dict[str, Any]:
        """Record a REAL gate crossing (the API path that did not exist before) and
        advance the linked job: GATE_IN -> AT_GATE, GATE_OUT -> COMPLETED."""
        plate_n = normalize_plate(plate)
        job = await self._repo.get_job(job_id) if job_id else None
        if job_id and job is None:
            raise JobConflict("job_not_found", f"job #{job_id} does not exist")
        cn = (container_number or (job or {}).get("container_number") or None)
        rec = {
            "ts": ts, "device_id": device_id or plate_n, "plate": plate_n,
            "gate_id": gate_id, "trip_id": (f"JOB-{job_id}" if job_id else plate_n),
            "event_type": event_type, "lat": lat, "lon": lon,
            "container_number": (cn.strip().upper() if cn else None),
            "bat_lane": bat_lane, "document_type": document_type,
            "document_reference": document_reference or (job or {}).get("document_reference"),
            "job_id": job_id, "source": "API", "driver_id": driver_id or (job or {}).get("driver_id"),
        }
        row = await self._repo.record_gate_event(rec)

        advanced = None
        if job_id:
            try:
                if event_type == "GATE_IN":
                    advanced = await self._advance(
                        job_id, new_status="AT_GATE", event=EVENT_GATE_IN, actor=actor,
                        actor_role=actor_role,
                        detail={"gate_id": gate_id, "bat_lane": bat_lane})
                elif event_type == "GATE_OUT":
                    advanced = await self._advance(
                        job_id, new_status="COMPLETED", event=EVENT_GATE_OUT, actor=actor,
                        actor_role=actor_role, detail={"gate_id": gate_id},
                        stamp="completed_at")
                else:
                    await self._repo.record_event(job_id, event=f"job.{event_type.lower()}",
                                                  actor=actor, actor_role=actor_role,
                                                  detail={"gate_id": gate_id})
            except JobConflict as exc:
                # The crossing is a FACT and stays recorded; the job simply did not
                # advance (e.g. GATE_OUT arrived while the job was still ASSIGNED).
                log.info("gate_event.job_not_advanced",
                         extra={"job_id": job_id, "reason": exc.code})
        log.info("gate_event.recorded", extra={"event_type": event_type, "plate": plate_n,
                                               "job_id": job_id, "gate_id": gate_id})
        return {"gate_event": row, "job": advanced}

    async def gate_events(self, **kw) -> list[dict]:
        return await self._repo.gate_events_for(**kw)

    # ========================================================= yard movements
    async def record_movement(self, *, movement_type: str, job_id: Optional[int] = None,
                              container_number: Optional[str] = None,
                              yard_location: Optional[str] = None,
                              from_location: Optional[str] = None,
                              vehicle_id: Optional[str] = None, vehicle_no: Optional[str] = None,
                              driver_id: Optional[str] = None, terminal: Optional[str] = None,
                              occurred_at=None, actor: Optional[str] = None,
                              actor_role: Optional[str] = None,
                              detail: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Yard pickup / drop / move — first-class events (previously unmodelled).

        ``yard_location`` is free-format on purpose: the real PIN-ticket format
        ('2P08D.1') cannot fit the cargo yard_block regex."""
        if movement_type not in ("YARD_PICKUP", "YARD_DROP", "YARD_MOVE"):
            raise ValidationFailed("invalid_movement_type",
                                   "movement_type must be YARD_PICKUP, YARD_DROP or YARD_MOVE")
        job = await self._repo.get_job(job_id) if job_id else None
        if job_id and job is None:
            raise JobConflict("job_not_found", f"job #{job_id} does not exist")
        cn = (container_number or (job or {}).get("container_number") or None)
        rec = {
            "job_id": job_id, "container_number": (cn.strip().upper() if cn else None),
            "movement_type": movement_type,
            "vehicle_id": vehicle_id or (job or {}).get("vehicle_id"),
            "vehicle_no": vehicle_no or (job or {}).get("vehicle_no"),
            "driver_id": driver_id or (job or {}).get("driver_id"),
            "yard_location": yard_location, "from_location": from_location,
            "terminal": terminal or (job or {}).get("terminal"),
            "occurred_at": occurred_at, "actor": actor, "detail": detail or {},
        }
        row = await self._repo.record_movement(rec)

        advanced = None
        if job_id:
            try:
                if movement_type == "YARD_PICKUP":
                    # entering the yard is implied by the first pickup
                    if (job or {}).get("status") == "AT_GATE":
                        await self._advance(job_id, new_status="IN_YARD", event=EVENT_IN_YARD,
                                            actor=actor, actor_role=actor_role,
                                            detail={"yard_location": yard_location})
                    advanced = await self._advance(
                        job_id, new_status="PICKED_UP", event=EVENT_YARD_PICKUP, actor=actor,
                        actor_role=actor_role, detail={"yard_location": yard_location})
                elif movement_type == "YARD_DROP":
                    if (job or {}).get("status") == "AT_GATE":
                        await self._advance(job_id, new_status="IN_YARD", event=EVENT_IN_YARD,
                                            actor=actor, actor_role=actor_role,
                                            detail={"yard_location": yard_location})
                    advanced = await self._advance(
                        job_id, new_status="DROPPED", event=EVENT_YARD_DROP, actor=actor,
                        actor_role=actor_role, detail={"yard_location": yard_location})
                else:
                    await self._repo.record_event(job_id, event="job.yard_move", actor=actor,
                                                  actor_role=actor_role,
                                                  detail={"yard_location": yard_location})
            except JobConflict as exc:
                log.info("movement.job_not_advanced", extra={"job_id": job_id, "reason": exc.code})
        return {"movement": row, "job": advanced}

    async def movements(self, **kw) -> list[dict]:
        return await self._repo.movements_for(**kw)

    # =============================================================== scanner
    async def scanners(self, *, active_only: bool = True) -> list[dict]:
        return await self._repo.list_scanners(active_only=active_only)

    async def scan_status(self, container_number: str) -> Dict[str, Any]:
        """Does this box need scanning, at which machine, and was it scanned?

        This is the truck→scanner routing answer the audit found missing entirely:
        the RMS selection names a machine (D-INNSA1RSDT02); this resolves it to the
        scanner master and reports the latest scan verdict."""
        cn = container_number.strip().upper()
        selection = await self._repo.rms_selection_for(cn)
        latest = await self._repo.latest_scan(cn)
        job = await self._repo.latest_job_for_container(cn)
        required = selection is not None
        return {
            "container_number": cn,
            "scan_required": required,
            "rms_selection": selection,
            "machine_code": (selection or {}).get("machine_code"),
            "machine_class": (selection or {}).get("machine_class"),
            "latest_scan": latest,
            "result": (latest or {}).get("result") or ("SCAN_PENDING" if required else None),
            "cleared": (latest or {}).get("result") == "SCANNED_CLEAN" or not required,
            "job_id": (job or {}).get("id"),
        }

    async def record_scan(self, *, container_number: str, result: str,
                          machine_code: Optional[str] = None, job_id: Optional[int] = None,
                          vehicle_id: Optional[str] = None, vehicle_no: Optional[str] = None,
                          igm_no: Optional[int] = None, remarks: Optional[str] = None,
                          scanned_at=None, actor: Optional[str] = None,
                          actor_role: Optional[str] = None) -> Dict[str, Any]:
        """Record a scan outcome and reflect it on the cargo row.

        SCANNED_CLEAN releases the customs hold back to PENDING (OOC still decides
        final clearance); SCAN_HOLD keeps/sets UNDER_INSPECTION."""
        valid = ("SCAN_PENDING", "SCANNED_CLEAN", "SCAN_HOLD", "SCAN_SKIPPED")
        if result not in valid:
            raise ValidationFailed("invalid_scan_result", f"result must be one of {list(valid)}")
        cn = container_number.strip().upper()
        if machine_code:
            machine = await self._repo.scanner_by_code(machine_code)
            if machine is None:
                raise ValidationFailed("scanner_not_found",
                                       f"scanner {machine_code} is not in the machine master")
        row = await self._repo.record_scan({
            "container_number": cn, "job_id": job_id, "vehicle_id": vehicle_id,
            "vehicle_no": vehicle_no, "machine_code": machine_code, "igm_no": igm_no,
            "result": result, "remarks": remarks, "scanned_at": scanned_at, "actor": actor,
        })
        if result == "SCANNED_CLEAN":
            await self._repo.set_cargo_customs_status(cn, "PENDING")
        elif result == "SCAN_HOLD":
            await self._repo.set_cargo_customs_status(cn, "UNDER_INSPECTION")
        if job_id:
            await self._repo.record_event(job_id, event=EVENT_SCAN, actor=actor,
                                          actor_role=actor_role,
                                          detail={"result": result, "machine_code": machine_code})
        await self._publish(EVENT_SCAN, {"id": job_id, "container_number": cn},
                            {"result": result, "machine_code": machine_code})
        log.info("scan.recorded", extra={"container": cn, "result": result,
                                         "machine_code": machine_code})
        return {"scan": row}

    async def scans(self, **kw) -> list[dict]:
        return await self._repo.scans_for(**kw)

    # ================================================================== reads
    async def _events(self, job_id: int) -> list[dict]:
        """Job history with each row tagged with its canonical JOB_* milestone,
        including rows written before the codes existed."""
        rows = await self._repo.job_events(job_id)
        for row in rows:
            row["event_code"] = event_code(row.get("event"), row.get("new_status"))
        return rows

    async def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        job = await self._repo.get_job(job_id)
        if job is None:
            return None
        job["events"] = await self._events(job_id)
        return job

    async def list_jobs(self, *, filters, limit: int, offset: int,
                        include_pending: bool = False) -> Dict[str, Any]:
        """List job assignments, optionally preceded by the UC-II -> UC-III
        handover queue.

        ``include_pending`` is opt-in and defaults off, so every existing consumer
        (driver PWA, availability checks, reports) keeps seeing dispatched jobs
        only. The UC-III Container Operations console turns it on: a container
        that UC-II RELEASED but which no truck has been dispatched against had no
        representation anywhere on the UC-III read surface, so a released box was
        invisible until an operator happened to type its number into the assign
        panel. The queue entries are derived from core.cargo, carry
        ``pending_handover: true`` and ``id: null``, and are ordered first because
        they are the actionable half of the list.
        """
        pending_total = 0
        pending: list[dict] = []
        if include_pending and self._pending_applies(filters):
            pending_total = await self._repo.count_pending_handover(filters=filters)
            if offset < pending_total:
                pending = [self._as_pending_item(r) for r in
                           await self._repo.list_pending_handover(
                               filters=filters, limit=limit, offset=offset)]

        job_offset = max(0, offset - pending_total)
        job_limit = max(0, limit - len(pending))
        rows = (await self._repo.list_jobs(filters=filters, limit=job_limit, offset=job_offset)
                if job_limit else [])
        total = await self._repo.count_jobs(filters=filters) + pending_total
        items = pending + list(rows)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    @staticmethod
    def _pending_applies(filters) -> bool:
        """The handover queue answers "which released box still needs a truck?".
        A query already scoped to a truck, a driver, a job status or open jobs is
        asking about dispatched work, so the queue must stay out of it."""
        return not any(filters.get(k) for k in
                       ("vehicle_id", "vehicle_plate", "driver_id", "status", "open_only"))

    @staticmethod
    def _as_pending_item(row: Mapping[str, Any]) -> Dict[str, Any]:
        """Shape a released-cargo row like a job row so one list can render both.
        ``id`` is null and ``status`` is PENDING_ASSIGNMENT precisely because no
        job exists yet — the consumer must route these into the assignment flow
        (POST /api/jobs), never treat them as an open job."""
        return {
            "id": None,
            "pending_handover": True,
            "container_number": row.get("container_number"),
            "group_code": None,
            "transporter_id": None,
            "vehicle_id": None,
            # The truck UC-II recorded on the cargo row, as a dispatch HINT for
            # the operator — the binding is only real once a job is assigned.
            "vehicle_no": row.get("vehicle_number"),
            "driver_id": None,
            "driver_licence": None,
            "move_type": "IMPORT_PICK",
            "document_type": None,
            "document_reference": None,
            "terminal": None,
            "gate": None,
            "status": PENDING_ASSIGNMENT,
            "lifecycle_status": row.get("lifecycle_status"),
            "customs_status": row.get("customs_status"),
            "yard_block": row.get("yard_block"),
            "vessel_name": row.get("vessel_name"),
            "released_at": row.get("updated_at"),
            "assigned_by": None,
            "assigned_at": None,
            "accepted_at": None,
            "completed_at": None,
            "cancelled_reason": None,
            "notes": None,
        }

    async def pending_handover(self, *, container_number: Optional[str] = None,
                               limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """The handover queue on its own (released, no open job) — the same rows
        GET /api/jobs?include_pending=true prefixes its page with."""
        filters = {"container_number": container_number}
        rows = await self._repo.list_pending_handover(filters=filters, limit=limit, offset=offset)
        total = await self._repo.count_pending_handover(filters=filters)
        items = [self._as_pending_item(r) for r in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def vehicles_with_open_jobs(self) -> set:
        """Vehicle IDs holding a non-terminal job — the real "truck is busy" set
        used by the Control-Room availability dropdown (BUG-1)."""
        return await self._repo.vehicles_with_open_jobs()

    async def drivers_with_open_jobs(self) -> set:
        """Driver IDs holding a non-terminal job — the "driver is busy" set, the
        exact counterpart of :meth:`vehicles_with_open_jobs`, used by the
        Control-Room driver availability list."""
        return await self._repo.drivers_with_open_jobs()

    async def assignment_for_container(self, container_number: str) -> Optional[Dict[str, Any]]:
        job = await self._repo.latest_job_for_container(container_number.strip().upper())
        if job is None:
            return None
        job["events"] = await self._events(job["id"])
        return job
