"""Container Job persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL for the UC-III job spine (migration 0113):
assignment + status history, real gate crossings, yard movements and scan events.

Design notes:
  * Assignment validation reads the EXISTING masters (core.vehicle,
    core.driver_identity, core.driver + core.pdp, core.transporter_blacklist) —
    it creates no parallel registry.
  * Double-assignment is prevented by the partial unique indexes in 0113, so the
    guarantee holds even under concurrent writes; the pre-flight check exists to
    return a friendly error rather than a constraint violation.
  * Status transitions are applied under SELECT ... FOR UPDATE and written with
    their history row in ONE transaction (same discipline as services.cargo).
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.container_job.repository")

def _norm_py(raw: Optional[str]) -> str:
    """Python twin of :data:`ContainerJobRepository._NORM`, for bound parameters."""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


_JOB_COLS = ("container_number", "group_code", "transporter_id", "vehicle_id", "vehicle_no",
             "driver_id", "driver_licence", "move_type", "document_type",
             "document_reference", "terminal", "gate", "assigned_by", "notes")


class ContainerJobRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ helpers
    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def _one(self, sql: str, params: Mapping[str, Any] | None = None) -> Optional[dict]:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    async def _count(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(text(sql), dict(params or {}))).scalar() or 0)

    # -------------------------------------------------------------- validation
    async def vehicle_by_id(self, vehicle_id: str) -> Optional[dict]:
        return await self._one(
            "SELECT vehicle_id, vehicle_no, vehicle_type, status FROM core.vehicle "
            "WHERE vehicle_id = :v", {"v": vehicle_id})

    async def vehicle_by_plate(self, plate: str) -> Optional[dict]:
        return await self._one(
            "SELECT vehicle_id, vehicle_no, vehicle_type, status FROM core.vehicle "
            "WHERE upper(replace(coalesce(vehicle_no,''),' ','')) = :p LIMIT 1",
            {"p": plate})

    async def driver_identity(self, driver_id: str) -> Optional[dict]:
        return await self._one(
            "SELECT driver_id, name, license_no, status, vehicle_no_norm "
            "FROM core.driver_identity WHERE driver_id = :d", {"d": driver_id})

    async def driver_permit(self, licence_norm: str) -> Optional[dict]:
        """The driver's master row + CURRENT PDP permit (active + valid_until).

        This is the check the audit found missing everywhere: a permit — not the
        driving-licence date — decides whether a driver may take a job."""
        return await self._one(
            """SELECT dm.licence_number, dm.driver_name, dm.licence_valid_to,
                      dm.latest_pdp_number, dm.transporter_id,
                      ph.active AS pdp_active, ph.valid_until AS pdp_validity,
                      ph.pdp_number, ph.cancelled_by
               FROM core.driver dm
               LEFT JOIN LATERAL (
                   SELECT p.active, p.valid_until, p.pdp_number, p.cancelled_by
                   FROM core.pdp p
                   WHERE p.pdp_number = dm.latest_pdp_number
                   ORDER BY p.accepted_at DESC NULLS LAST LIMIT 1
               ) ph ON true
               WHERE dm.licence_no_norm = :ln LIMIT 1""",
            {"ln": licence_norm})

    async def transporter_blacklisted(self, *, transporter_id: Optional[int],
                                      vehicle_no: Optional[str]) -> Optional[dict]:
        """An ACTIVE blacklist record for the transporter or the vehicle's owner."""
        if transporter_id is not None:
            row = await self._one(
                "SELECT b.id, b.reason, b.severity, t.company_name AS transporter_name "
                "FROM core.transporter_blacklist b "
                "JOIN core.transporter t ON t.id = b.transporter_id "
                "WHERE b.transporter_id = :t AND b.status = 'ACTIVE' LIMIT 1",
                {"t": transporter_id})
            if row:
                return row
        if vehicle_no:
            return await self._one(
                "SELECT b.id, b.reason, b.severity, t.company_name AS transporter_name "
                "FROM core.transporter_vehicle tv "
                "JOIN core.transporter_blacklist b ON b.transporter_id = tv.transporter_id "
                "JOIN core.transporter t ON t.id = tv.transporter_id "
                "WHERE tv.vehicle_no_norm = :p AND b.status = 'ACTIVE' LIMIT 1",
                {"p": vehicle_no})
        return None

    # The human identifier normalised in SQL exactly as normalize_plate() /
    # normalize_licence() do in Python — see service.SQL_NORMALISE.
    _NORM = "upper(regexp_replace(coalesce({col}, ''), '[^A-Za-z0-9]', '', 'g'))"

    async def open_job_for_vehicle(self, vehicle_id: str,
                                   vehicle_no: Optional[str] = None) -> Optional[dict]:
        """The non-terminal job this TRUCK already holds, or None.

        Matched on the Vehicle ID or, when one is known, the registration — the
        same widened rule the availability list excludes on
        (services.container_job.service.open_job_not_exists). core.vehicle is
        unique on vehicle_id alone, so one truck can hold two master rows; a job
        raised against one of them still occupies the truck named by the other."""
        norm = self._NORM.format(col="vehicle_no")
        return await self._one(
            "SELECT id, container_number, vehicle_id, status "
            "FROM core.container_job_assignment "
            f"WHERE (vehicle_id = :v OR (:p <> '' AND {norm} = :p)) "
            "AND status NOT IN ('COMPLETED','CANCELLED') LIMIT 1",
            {"v": vehicle_id, "p": _norm_py(vehicle_no)})

    async def open_job_for_driver(self, driver_id: str,
                                  driver_licence: Optional[str] = None) -> Optional[dict]:
        """The non-terminal job this PERSON already holds, or None.

        Same two rules as :meth:`open_job_for_vehicle`: occupancy is the job's
        STATE (a COMPLETED or CANCELLED job keeps its driver_id and must free the
        person again), and the person is identified by their licence as well as
        their Driver ID, because core.driver_identity can carry several records
        for one driver."""
        norm = self._NORM.format(col="driver_licence")
        return await self._one(
            "SELECT id, container_number, vehicle_id, status "
            "FROM core.container_job_assignment "
            f"WHERE (driver_id = :d OR (:l <> '' AND {norm} = :l)) "
            "AND status NOT IN ('COMPLETED','CANCELLED') LIMIT 1",
            {"d": driver_id, "l": _norm_py(driver_licence)})

    async def open_job_for_container(self, container_number: str) -> Optional[dict]:
        return await self._one(
            "SELECT id, vehicle_id, status FROM core.container_job_assignment "
            "WHERE container_number = :c AND status NOT IN ('COMPLETED','CANCELLED') LIMIT 1",
            {"c": container_number})

    async def cargo_exists(self, container_number: str) -> Optional[dict]:
        return await self._one(
            "SELECT container_number, lifecycle_status, customs_status, is_released "
            "FROM core.cargo WHERE container_number = :c", {"c": container_number})

    async def customs_note(self, container_number: str) -> Optional[str]:
        """The remark customs recorded when it stopped this container, or None.

        Read only when an assignment is already being refused, from the three
        places the existing code writes such a remark:

          * the UC-III scanner — core.scan_event.remarks on a SCAN_HOLD, which is
            what flips the cargo row to UNDER_INSPECTION in service.record_scan;
          * the cargo module's verification — core.cargo_scan_verification.remarks
            with verified=false;
          * the CUSTOMS module itself — core.cargo_notification.message, which is
            where services.customs.reconcile_cargo writes its reason ("selected by
            RMS for customs scanning") when it moves a box to UNDER_INSPECTION.
            That path is how most real holds arise and it writes no scan remark,
            so a refusal used to report customs_note = null for precisely the
            containers customs had flagged itself.

        Newest wins. Nothing is synthesised: no remark on record returns None and
        the caller says only "Flagged by Customs". Resolved notifications are
        skipped — a closed alert is not the reason the box is still held."""
        row = await self._one(
            """
            SELECT remarks, ts FROM (
                SELECT remarks, scanned_at AS ts FROM core.scan_event
                 WHERE container_number = :cn AND result = 'SCAN_HOLD'
                UNION ALL
                SELECT remarks, created_at AS ts FROM core.cargo_scan_verification
                 WHERE container_number = :cn AND verified = false
                UNION ALL
                SELECT message AS remarks, created_at AS ts FROM core.cargo_notification
                 WHERE container_number = :cn AND notification_type LIKE 'CUSTOMS%'
                   AND status <> 'RESOLVED'
            ) r
            WHERE NULLIF(TRIM(COALESCE(remarks, '')), '') IS NOT NULL
            ORDER BY ts DESC LIMIT 1
            """, {"cn": container_number})
        note = (row or {}).get("remarks")
        return str(note).strip() if note else None

    async def document_counts(self, container_number: str) -> dict:
        """How many gate documents of each type reference this container.

        A truck may not be dispatched against a container with no paperwork, so
        the assignment gate needs a cheap existence probe rather than the full
        document payloads. Form-13 lives in the shared core.gate_capture store
        (both provenances count — a document exists regardless of whether it was
        uploaded or seeded)."""
        row = await self._one(
            "SELECT (SELECT count(*) FROM core.eir WHERE container_number = :c) AS eir, "
            "       (SELECT count(*) FROM core.pin_ticket WHERE container_number = :c) AS pin, "
            "       (SELECT count(*) FROM core.gate_capture "
            "         WHERE capture_type = 'FORM13' AND container_no = :c) AS form13",
            {"c": container_number})
        out = {k: int(v or 0) for k, v in (row or {}).items()}
        out["total"] = out.get("eir", 0) + out.get("pin", 0) + out.get("form13", 0)
        return out

    # ------------------------------------------------------------------ create
    async def create_job(self, rec: Mapping[str, Any], *,
                         blocked_customs: frozenset[str] = frozenset()) -> dict:
        """Insert the assignment + its ASSIGNED history row in one transaction.

        ``blocked_customs`` re-evaluates the customs disposition INSIDE that
        transaction, against the cargo row locked ``FOR SHARE``. The service has
        already checked it, but that read committed before this one opens: a
        customs hold landing in between (POST /api/scan/events writing SCAN_HOLD,
        or /api/customs/reconcile flipping the box to UNDER_INSPECTION) would
        otherwise be overtaken by an assignment that passed a stale check. The
        lock is the same discipline services.cargo already applies to release
        (CargoRepository.transition_lifecycle's ``blocked_customs``); FOR SHARE
        rather than FOR UPDATE because this transaction only reads the cargo row,
        and it is enough — every customs writer takes a stronger lock and so
        waits for this INSERT to commit or roll back.

        Raises :class:`CustomsFlagged` on a flagged container, before the INSERT
        runs, so nothing is persisted."""
        params = {c: rec.get(c) for c in _JOB_COLS}
        cols = ", ".join(_JOB_COLS)
        vals = ", ".join(f":{c}" for c in _JOB_COLS)
        cn = (rec.get("container_number") or "").strip().upper() or None
        try:
            async with get_engine(self._dsn).begin() as conn:
                if cn and blocked_customs:
                    cargo = (await conn.execute(text(
                        "SELECT customs_status FROM core.cargo "
                        "WHERE container_number = :c FOR SHARE"),
                        {"c": cn})).mappings().first()
                    cs = str((cargo or {}).get("customs_status") or "").upper()
                    if cs in blocked_customs:
                        raise CustomsFlagged(cn, cs)
                row = (await conn.execute(text(
                    f"INSERT INTO core.container_job_assignment ({cols}) "
                    f"VALUES ({vals}) RETURNING *"), params)).mappings().first()
                await conn.execute(text(_EVENT_INSERT), {
                    "job_id": row["id"], "event": "job.assigned", "old_status": None,
                    "new_status": "ASSIGNED", "actor": rec.get("assigned_by"),
                    "actor_role": rec.get("actor_role"),
                    "detail": json.dumps({"move_type": rec.get("move_type"),
                                          "document_reference": rec.get("document_reference")}),
                })
            return dict(row)
        except IntegrityError as exc:
            # The partial unique indexes are the real guard (concurrency-safe).
            msg = str(getattr(exc, "orig", exc))
            if "uq_job_open_vehicle" in msg:
                raise JobConflict("vehicle_already_assigned",
                                  "vehicle already holds an open job") from exc
            if "uq_job_open_container" in msg:
                raise JobConflict("container_already_assigned",
                                  "container already has an open job") from exc
            if "uq_job_open_driver" in msg:
                raise JobConflict("driver_already_assigned",
                                  "driver already holds an open job") from exc
            raise

    # -------------------------------------------------------------- transition
    async def transition(self, job_id: int, *, new_status: str, allowed_from: set[str],
                         event: str, actor: Optional[str] = None,
                         actor_role: Optional[str] = None,
                         detail: Optional[Mapping[str, Any]] = None,
                         stamp: Optional[str] = None) -> dict:
        """Apply a status change under a row lock, writing history atomically.

        Returns ``{ok, job, current_status}``. ``ok=False`` when the current status
        is not in ``allowed_from`` (the caller maps that to 409)."""
        async with get_engine(self._dsn).begin() as conn:
            cur = (await conn.execute(text(
                "SELECT * FROM core.container_job_assignment WHERE id = :id FOR UPDATE"),
                {"id": job_id})).mappings().first()
            if cur is None:
                return {"ok": False, "job": None, "current_status": None, "missing": True}
            if cur["status"] not in allowed_from:
                return {"ok": False, "job": dict(cur), "current_status": cur["status"]}
            sets = ["status = :st", "updated_at = now()"]
            if stamp:
                sets.append(f"{stamp} = now()")
            if new_status == "CANCELLED" and detail and detail.get("reason"):
                sets.append("cancelled_reason = :reason")
            params: dict[str, Any] = {"st": new_status, "id": job_id}
            if new_status == "CANCELLED" and detail and detail.get("reason"):
                params["reason"] = detail["reason"]
            row = (await conn.execute(text(
                f"UPDATE core.container_job_assignment SET {', '.join(sets)} "
                "WHERE id = :id RETURNING *"), params)).mappings().first()
            await conn.execute(text(_EVENT_INSERT), {
                "job_id": job_id, "event": event, "old_status": cur["status"],
                "new_status": new_status, "actor": actor, "actor_role": actor_role,
                "detail": json.dumps(dict(detail or {})),
            })
        return {"ok": True, "job": dict(row), "current_status": new_status}

    async def record_event(self, job_id: int, *, event: str, actor: Optional[str] = None,
                           actor_role: Optional[str] = None,
                           detail: Optional[Mapping[str, Any]] = None) -> None:
        """Append a non-transition job event (best-effort; never raises)."""
        try:
            async with get_engine(self._dsn).begin() as conn:
                await conn.execute(text(_EVENT_INSERT), {
                    "job_id": job_id, "event": event, "old_status": None,
                    "new_status": None, "actor": actor, "actor_role": actor_role,
                    "detail": json.dumps(dict(detail or {}))})
        except Exception as exc:  # noqa: BLE001
            log.warning("job.event_record_failed", extra={"job_id": job_id, "error": str(exc)})

    # ------------------------------------------------------------------- reads
    async def get_job(self, job_id: int) -> Optional[dict]:
        return await self._one("SELECT * FROM core.container_job_assignment WHERE id = :id",
                               {"id": job_id})

    async def job_events(self, job_id: int) -> list[dict]:
        return await self._rows(
            "SELECT id, event, old_status, new_status, actor, actor_role, detail, created_at "
            "FROM core.container_job_event WHERE job_id = :id ORDER BY id", {"id": job_id})

    @staticmethod
    def _job_where(f: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, p = [], {}
        if f.get("container_number"):
            clauses.append("container_number = :cn")
            p["cn"] = str(f["container_number"]).strip().upper()
        if f.get("vehicle_id"):
            # BUG-2: the driver LIST must scope exactly like the driver DETAIL
            # ownership check (driver_jobs._owns), which accepts either the
            # internal Vehicle ID or the normalised plate. Matching vehicle_id
            # alone made a driver paired with the registration (MH43SV7025)
            # instead of the TRK id see an empty list forever while still being
            # able to open the very same job by id. `vehicle_plate` is the
            # normalised form of the same binding; when supplied, either matches.
            if f.get("vehicle_plate"):
                clauses.append("(vehicle_id = :vid OR "
                               "regexp_replace(upper(coalesce(vehicle_no, '')), "
                               "'[^A-Z0-9]', '', 'g') = :vplate)")
                p["vplate"] = f["vehicle_plate"]
            else:
                clauses.append("vehicle_id = :vid")
            p["vid"] = f["vehicle_id"]
        if f.get("driver_id"):
            clauses.append("driver_id = :did")
            p["did"] = f["driver_id"]
        if f.get("status"):
            clauses.append("status = :st")
            p["st"] = str(f["status"]).upper()
        if f.get("open_only"):
            clauses.append("status NOT IN ('COMPLETED','CANCELLED')")
        if f.get("transporter_id") is not None:
            clauses.append("transporter_id = :tid")
            p["tid"] = f["transporter_id"]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), p

    async def list_jobs(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, p = self._job_where(filters)
        p.update(limit=limit, offset=offset)
        return await self._rows(
            f"SELECT * FROM core.container_job_assignment{where} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset", p)

    async def count_jobs(self, *, filters: Mapping[str, Any]) -> int:
        where, p = self._job_where(filters)
        return await self._count(f"SELECT count(*) FROM core.container_job_assignment{where}", p)

    # ------------------------------------------------- UC-II -> UC-III handover
    # A container that UC-II released is, until a truck is dispatched against it,
    # a job that does not exist yet: core.container_job_assignment.vehicle_id is
    # NOT NULL and the assignment gate demands a driver + gate document, so there
    # is nothing legitimate to insert at release time. The handover queue is
    # therefore READ from core.cargo (the single source of truth for the release)
    # rather than projected into a second table — no duplication, no fabricated
    # job row, and the row disappears from the queue the moment a real job opens
    # against it.
    _PENDING_FROM = (
        " FROM core.cargo c "
        " WHERE (c.lifecycle_status = 'RELEASED' OR c.is_released) "
        "   AND NOT EXISTS (SELECT 1 FROM core.container_job_assignment j "
        "                    WHERE j.container_number = c.container_number "
        "                      AND j.status NOT IN ('COMPLETED','CANCELLED'))")

    @staticmethod
    def _pending_where(f: Mapping[str, Any]) -> tuple[str, dict]:
        """Only the filters that can mean anything for a box with no job yet.

        A query scoped to a vehicle/driver/job-status is asking about dispatched
        work, so it must NOT be answered with un-dispatched boxes — the caller
        (service.list_pending_handover) skips the queue entirely for those."""
        clauses, p = "", {}
        if f.get("container_number"):
            clauses = " AND c.container_number = :pcn"
            p["pcn"] = str(f["container_number"]).strip().upper()
        return clauses, p

    async def list_pending_handover(self, *, filters: Mapping[str, Any],
                                    limit: int, offset: int) -> list[dict]:
        where, p = self._pending_where(filters)
        p.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT c.container_number, c.lifecycle_status, c.customs_status, "
            "       c.yard_block, c.vehicle_number, c.vessel_name, c.updated_at"
            + self._PENDING_FROM + where +
            " ORDER BY c.updated_at DESC, c.container_number LIMIT :limit OFFSET :offset", p)

    async def count_pending_handover(self, *, filters: Mapping[str, Any]) -> int:
        where, p = self._pending_where(filters)
        return await self._count("SELECT count(*)" + self._PENDING_FROM + where, p)

    async def vehicles_with_open_jobs(self) -> set[str]:
        """Vehicle IDs currently holding a job that is neither COMPLETED nor
        CANCELLED — i.e. genuinely busy.

        BUG-1: this is what makes a truck unavailable for a NEW assignment. It is
        NOT the same thing as "has a driver" (core.driver_identity), which is what
        /api/vehicles/available used to filter on and which excluded precisely the
        driver-bound fleet the PWA can sign in as."""
        rows = await self._rows(
            "SELECT DISTINCT vehicle_id, vehicle_no FROM core.container_job_assignment "
            "WHERE status NOT IN ('COMPLETED','CANCELLED') AND vehicle_id IS NOT NULL")
        # BOTH keys: this set is the in-memory backend's stand-in for the SQL
        # correlation, which matches a Vehicle ID or a registration. Returning
        # ids alone would let a truck's duplicate master row read as free.
        out = {r["vehicle_id"] for r in rows if r.get("vehicle_id")}
        out |= {_norm_py(r.get("vehicle_no")) for r in rows if _norm_py(r.get("vehicle_no"))}
        return out

    async def drivers_with_open_jobs(self) -> set[str]:
        """Driver IDs currently holding a job that is neither COMPLETED nor
        CANCELLED — i.e. genuinely busy.

        The driver-side twin of :meth:`vehicles_with_open_jobs`. Only the
        in-memory identity backend (which has no job table to join against) needs
        it; the Postgres path filters with the correlated NOT EXISTS in
        gateway.enrollment so the LIMIT applies to genuinely free drivers."""
        rows = await self._rows(
            "SELECT DISTINCT driver_id, driver_licence FROM core.container_job_assignment "
            "WHERE status NOT IN ('COMPLETED','CANCELLED') AND driver_id IS NOT NULL")
        out = {r["driver_id"] for r in rows if r.get("driver_id")}
        out |= {_norm_py(r.get("driver_licence")) for r in rows
                if _norm_py(r.get("driver_licence"))}
        return out

    async def latest_job_for_container(self, container_number: str) -> Optional[dict]:
        return await self._one(
            "SELECT * FROM core.container_job_assignment WHERE container_number = :c "
            "ORDER BY (status NOT IN ('COMPLETED','CANCELLED')) DESC, id DESC LIMIT 1",
            {"c": container_number})

    # ------------------------------------------------------------- gate events
    async def record_gate_event(self, rec: Mapping[str, Any]) -> dict:
        """Record a REAL gate crossing into the existing core.gate_event table.

        device_id/trip_id are NOT NULL in the base table (the simulator supplies
        them); an API-recorded crossing uses the plate as device_id and the job
        reference as trip_id so both producers coexist in one stream."""
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(text(
                """INSERT INTO core.gate_event
                       (ts, device_id, plate, gate_id, trip_id, event_type, lat, lon,
                        container_number, bat_lane, document_type, document_reference,
                        job_id, source, driver_id)
                   VALUES (coalesce(:ts, now()), :device_id, :plate, :gate_id, :trip_id,
                           :event_type, :lat, :lon, :container_number, :bat_lane,
                           :document_type, :document_reference, :job_id, :source, :driver_id)
                   RETURNING *"""), dict(rec))).mappings().first()
        return dict(row)

    async def gate_events_for(self, *, plate: Optional[str] = None,
                              container_number: Optional[str] = None,
                              job_id: Optional[int] = None, limit: int = 100) -> list[dict]:
        clauses, p = [], {"limit": limit}
        if plate:
            clauses.append("plate = :plate")
            p["plate"] = plate
        if container_number:
            clauses.append("container_number = :cn")
            p["cn"] = container_number
        if job_id is not None:
            clauses.append("job_id = :jid")
            p["jid"] = job_id
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return await self._rows(
            "SELECT id, ts, device_id, plate, gate_id, trip_id, event_type, container_number, "
            "bat_lane, document_type, document_reference, job_id, source, driver_id "
            f"FROM core.gate_event{where} ORDER BY ts DESC, id DESC LIMIT :limit", p)

    # --------------------------------------------------------- yard movements
    async def record_movement(self, rec: Mapping[str, Any]) -> dict:
        params = dict(rec)
        params["detail"] = json.dumps(dict(rec.get("detail") or {}))
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(text(
                """INSERT INTO core.cargo_movement_event
                       (job_id, container_number, movement_type, vehicle_id, vehicle_no,
                        driver_id, yard_location, from_location, terminal, occurred_at,
                        actor, detail)
                   VALUES (:job_id, :container_number, :movement_type, :vehicle_id, :vehicle_no,
                           :driver_id, :yard_location, :from_location, :terminal,
                           coalesce(:occurred_at, now()), :actor, CAST(:detail AS jsonb))
                   RETURNING *"""), params)).mappings().first()
        return dict(row)

    async def movements_for(self, *, container_number: Optional[str] = None,
                            job_id: Optional[int] = None, limit: int = 100) -> list[dict]:
        clauses, p = [], {"limit": limit}
        if container_number:
            clauses.append("container_number = :cn")
            p["cn"] = container_number
        if job_id is not None:
            clauses.append("job_id = :jid")
            p["jid"] = job_id
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return await self._rows(
            f"SELECT * FROM core.cargo_movement_event{where} "
            "ORDER BY occurred_at DESC, id DESC LIMIT :limit", p)

    # -------------------------------------------------------------- scanner
    async def list_scanners(self, *, active_only: bool = True) -> list[dict]:
        where = " WHERE active IS TRUE" if active_only else ""
        return await self._rows(
            f"SELECT * FROM core.scanner_machine{where} ORDER BY machine_code")

    async def scanner_by_code(self, machine_code: str) -> Optional[dict]:
        return await self._one("SELECT * FROM core.scanner_machine WHERE machine_code = :c",
                               {"c": machine_code})

    async def rms_selection_for(self, container_number: str) -> Optional[dict]:
        """The RMS scan selection for a container: which machine it was routed to.

        Reconstitutes the full machine code (D-INNSA1RSDT02) from the stored
        machine_type letter + scan_location — the join the audit found missing.

        ``igm_no`` is read from the PARENT scan report, not from the child row:
        the deployed RDS carries the base ``core.rms_scan_container`` shape
        (report_id, sl_no, container_no, machine_type, scan_location, cfs_name,
        goods_desc) without migration 0102's extension columns (id / igm_no /
        iso_valid), so selecting them here fails in production. Ordering likewise
        uses (report_id, sl_no), which exist in both schema variants."""
        return await self._one(
            """SELECT rc.container_no, r.igm_no, rc.machine_type, rc.scan_location,
                      rc.cfs_name,
                      (rc.machine_type || '-' || rc.scan_location) AS machine_code,
                      sm.machine_class, sm.terminal AS scanner_terminal, sm.active AS scanner_active
               FROM core.rms_scan_container rc
               LEFT JOIN core.rms_scan_report r ON r.report_id = rc.report_id
               LEFT JOIN core.scanner_machine sm
                      ON sm.machine_code = (rc.machine_type || '-' || rc.scan_location)
               WHERE rc.container_no = :cn
               ORDER BY rc.report_id DESC, rc.sl_no DESC LIMIT 1""",
            {"cn": container_number})

    async def record_scan(self, rec: Mapping[str, Any]) -> dict:
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(text(
                """INSERT INTO core.scan_event
                       (container_number, job_id, vehicle_id, vehicle_no, machine_code,
                        igm_no, result, remarks, scanned_at, actor)
                   VALUES (:container_number, :job_id, :vehicle_id, :vehicle_no, :machine_code,
                           :igm_no, :result, :remarks, coalesce(:scanned_at, now()), :actor)
                   RETURNING *"""), dict(rec))).mappings().first()
        return dict(row)

    async def latest_scan(self, container_number: str) -> Optional[dict]:
        return await self._one(
            "SELECT * FROM core.scan_event WHERE container_number = :cn "
            "ORDER BY scanned_at DESC, id DESC LIMIT 1", {"cn": container_number})

    async def scans_for(self, *, container_number: Optional[str] = None,
                        result: Optional[str] = None, limit: int = 100) -> list[dict]:
        clauses, p = [], {"limit": limit}
        if container_number:
            clauses.append("container_number = :cn")
            p["cn"] = container_number
        if result:
            clauses.append("result = :r")
            p["r"] = result
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return await self._rows(
            f"SELECT * FROM core.scan_event{where} ORDER BY scanned_at DESC, id DESC LIMIT :limit", p)

    async def set_cargo_customs_status(self, container_number: str, status_value: str) -> None:
        """Reflect a scan verdict on the EXISTING cargo row (best-effort, additive)."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE core.cargo SET customs_status = :s, updated_at = now() "
                "WHERE container_number = :c"),
                {"s": status_value, "c": container_number})


class JobConflict(Exception):
    """A uniqueness/precondition conflict the router maps to HTTP 409."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class CustomsFlagged(Exception):
    """core.cargo says customs has stopped this container — raised from inside the
    assignment transaction, under the cargo row lock.

    Carries the facts only. The service owns the note lookup and the operator
    wording, so the locked re-check and the pre-flight check answer the caller
    with one identical ``customs_flagged`` body.

    Distinct from :class:`JobConflict`, which the router answers 409 with no
    customs context: this is the same refusal as the pre-flight one and must
    reach the operator as such."""

    def __init__(self, container_number: str, customs_status: Optional[str]) -> None:
        self.container_number = container_number
        self.customs_status = (customs_status or "").upper()
        super().__init__(f"customs blocks assignment for {container_number}: "
                         f"customs_status={self.customs_status or 'UNKNOWN'}")


_EVENT_INSERT = """
INSERT INTO core.container_job_event
    (job_id, event, old_status, new_status, actor, actor_role, detail)
VALUES (:job_id, :event, :old_status, :new_status, :actor, :actor_role, CAST(:detail AS jsonb))
"""
