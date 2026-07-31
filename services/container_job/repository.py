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

    async def open_job_for_vehicle(self, vehicle_id: str) -> Optional[dict]:
        return await self._one(
            "SELECT id, container_number, status FROM core.container_job_assignment "
            "WHERE vehicle_id = :v AND status NOT IN ('COMPLETED','CANCELLED') LIMIT 1",
            {"v": vehicle_id})

    async def open_job_for_container(self, container_number: str) -> Optional[dict]:
        return await self._one(
            "SELECT id, vehicle_id, status FROM core.container_job_assignment "
            "WHERE container_number = :c AND status NOT IN ('COMPLETED','CANCELLED') LIMIT 1",
            {"c": container_number})

    async def cargo_exists(self, container_number: str) -> Optional[dict]:
        return await self._one(
            "SELECT container_number, lifecycle_status, customs_status, is_released "
            "FROM core.cargo WHERE container_number = :c", {"c": container_number})

    # ------------------------------------------------------------------ create
    async def create_job(self, rec: Mapping[str, Any]) -> dict:
        """Insert the assignment + its ASSIGNED history row in one transaction."""
        params = {c: rec.get(c) for c in _JOB_COLS}
        cols = ", ".join(_JOB_COLS)
        vals = ", ".join(f":{c}" for c in _JOB_COLS)
        try:
            async with get_engine(self._dsn).begin() as conn:
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


_EVENT_INSERT = """
INSERT INTO core.container_job_event
    (job_id, event, old_status, new_status, actor, actor_role, detail)
VALUES (:job_id, :event, :old_status, :new_status, :actor, :actor_role, CAST(:detail AS jsonb))
"""
