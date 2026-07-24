"""Cargo persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to ``core.cargo``. It performs no business logic
and no HTTP; it just runs parameterised statements through the cached
SQLAlchemy async engine (``jnpa_shared.db.get_engine``) exactly like
:mod:`services.fastag.service` — reads on a plain ``connect()``, writes inside a
single ``engine.begin()`` transaction (auto-commit / auto-rollback). No ORM.

Errors are surfaced as typed exceptions (:class:`CargoConflict`) so the service
layer can map them to HTTP status codes without importing SQLAlchemy.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.cargo.repository")


class CargoConflict(Exception):
    """Raised when an INSERT violates the container_number primary key."""


class CargoNotFound(Exception):
    """Raised when an update/delete targets a container_number that is absent."""


# Every column the API round-trips, in a stable order. `created_at`/`updated_at`
# are server-managed (DEFAULT now() + the BEFORE-UPDATE trigger) and never set by
# a client write. The e-Seal / pre-document / origin-stream columns were added by
# migration 0015 (all NULLable — backward compatible).
_COLUMNS = (
    "container_number", "vessel_name", "customs_status", "yard_block",
    "is_released", "vehicle_number", "gate", "camera_id", "eta",
    "eseal_status", "eseal_number", "pre_document_status", "origin_stream",
    # Unified lifecycle status (migration 0023). Server-managed via
    # transition_lifecycle() — NOT in _WRITABLE, so a client PUT can never set it
    # directly; the DB DEFAULT 'CREATED' populates it on INSERT.
    "lifecycle_status",
    "created_at", "updated_at",
)
_SELECT_COLS = ", ".join(_COLUMNS)

# Columns a client may set on create / patch on update (server-managed audit
# columns and the immutable PK are deliberately excluded from the update set).
_WRITABLE = (
    "vessel_name", "customs_status", "yard_block", "is_released",
    "vehicle_number", "gate", "camera_id", "eta",
    "eseal_status", "eseal_number", "pre_document_status", "origin_stream",
)

_INSERT = f"""
INSERT INTO core.cargo
    (container_number, vessel_name, customs_status, yard_block, is_released,
     vehicle_number, gate, camera_id, eta,
     eseal_status, eseal_number, pre_document_status, origin_stream)
VALUES
    (:container_number, :vessel_name, :customs_status, :yard_block, :is_released,
     :vehicle_number, :gate, :camera_id, :eta,
     :eseal_status, :eseal_number, :pre_document_status, :origin_stream)
RETURNING {_SELECT_COLS}
"""

_SELECT_ONE = f"SELECT {_SELECT_COLS} FROM core.cargo WHERE container_number = :container_number"

_DELETE = "DELETE FROM core.cargo WHERE container_number = :container_number"

# Cargo lifecycle event log (notifications contract, migration 0015).
_EVENT_COLS = ("id", "event", "container_number", "payload", "created_at")
_EVENT_SELECT = ", ".join(_EVENT_COLS)
_EVENT_INSERT = f"""
INSERT INTO core.cargo_event (event, container_number, payload)
VALUES (:event, :container_number, CAST(:payload AS jsonb))
RETURNING {_EVENT_SELECT}
"""

# Cargo workflow transition log (migration 0016). Append-only; the CURRENT status
# lives on core.cargo.workflow_status.
_WORKFLOW_COLS = ("id", "container_number", "action", "old_status", "new_status",
                  "comment", "created_at")
_WORKFLOW_SELECT = ", ".join(_WORKFLOW_COLS)

# Stakeholder notifications (migration 0017).
_NOTIF_COLS = ("id", "container_number", "notification_type", "severity",
               "message", "stakeholders", "status", "created_at")
_NOTIF_SELECT = ", ".join(_NOTIF_COLS)
# Whitelisted equality filters for the notifications list (keys are fixed
# identifiers, values always bound — injection-safe by construction).
_NOTIF_FILTER_COLS = ("container_number", "notification_type", "severity", "status")

# Planning tables (migration 0018).
_YARD_PLAN_COLS = ("id", "container_number", "preferred_block", "assigned_block",
                   "priority", "status", "created_at")
_YARD_PLAN_SELECT = ", ".join(_YARD_PLAN_COLS)
_RAKE_PLAN_COLS = ("id", "rake_id", "containers", "planned_containers", "status",
                   "created_at")
_RAKE_PLAN_SELECT = ", ".join(_RAKE_PLAN_COLS)
_REEFER_PLAN_COLS = ("id", "container_number", "temperature", "power_required",
                     "slot", "status", "created_at")
_REEFER_PLAN_SELECT = ", ".join(_REEFER_PLAN_COLS)

# Lifecycle audit log + scan verification records (migration 0023).
_LIFECYCLE_COLS = ("id", "container_number", "action", "old_status", "new_status",
                   "actor_role", "note", "created_at")
_LIFECYCLE_SELECT = ", ".join(_LIFECYCLE_COLS)
_SCAN_VERIF_COLS = ("id", "container_number", "verified", "remarks", "actor_role",
                    "created_at")
_SCAN_VERIF_SELECT = ", ".join(_SCAN_VERIF_COLS)


class CargoTransitionError(Exception):
    """Raised when a lifecycle transition is rejected: the container's current
    status is not a legal predecessor of the requested target (e.g. release before
    verify, or a duplicate release). Carries the current + attempted status so the
    router can render a precise 409."""

    def __init__(self, container_number: str, current: Optional[str], target: str) -> None:
        self.container_number = container_number
        self.current = current
        self.target = target
        super().__init__(
            f"illegal cargo transition for {container_number}: "
            f"{current or 'UNKNOWN'} -> {target}")


def _infer_lifecycle(row: Mapping[str, Any]) -> str:
    """Resolve a row's effective lifecycle when ``lifecycle_status`` is NULL — the
    case for rows written before migration 0023 backfilled them, or by a fake in a
    test. Derived from the legacy columns so the state machine behaves sensibly on
    historical data: released -> RELEASED, yarded -> YARD_ASSIGNED, else CREATED."""
    status = row.get("lifecycle_status")
    if status:
        return str(status)
    if row.get("is_released"):
        return "RELEASED"
    if row.get("yard_block"):
        return "YARD_ASSIGNED"
    return "CREATED"


class CargoRepository:
    """Raw-SQL CRUD for ``core.cargo``. Stateless apart from the DSN, so a single
    instance is safe to share across requests (the engine + pool are cached)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ create
    async def create(self, row: Mapping[str, Any]) -> dict:
        """INSERT one cargo row and return it. Raises :class:`CargoConflict` if
        the container_number already exists (PK violation)."""
        params = {c: row.get(c) for c in
                  ("container_number", *_WRITABLE)}
        try:
            async with get_engine(self._dsn).begin() as conn:
                result = await conn.execute(text(_INSERT), params)
                created = result.mappings().first()
        except IntegrityError as exc:  # unique_violation on the PK
            raise CargoConflict(str(getattr(exc, "orig", exc))) from exc
        return dict(created) if created else dict(params)

    # -------------------------------------------------------------------- read
    async def get(self, container_number: str) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(_SELECT_ONE),
                                        {"container_number": container_number})
            row = result.mappings().first()
        return dict(row) if row else None

    # Column names allowed as equality filters. Keys are fixed identifiers (NEVER
    # interpolated from client input); values are always bound parameters — so the
    # WHERE clause is injection-safe by construction.
    _FILTER_COLS = (
        "container_number", "customs_status", "yard_block", "is_released",
        "vehicle_number", "eseal_status", "pre_document_status", "origin_stream",
        "lifecycle_status",
    )

    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Build a parameterised ``WHERE`` clause from the whitelisted filters that
        are actually provided (non-None). Shared by list() and count()."""
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in self._FILTER_COLS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"{col} = :{col}")
                params[col] = val
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    async def list(
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
        """List cargo, newest ETA first. Every filter is an optional equality
        match, applied only when provided — so the no-arg call is unchanged
        (backward compatible)."""
        clause, params = self._where(locals())
        params.update({"limit": limit, "offset": offset})
        sql = (
            f"SELECT {_SELECT_COLS} FROM core.cargo {clause} "
            "ORDER BY eta DESC NULLS LAST, created_at DESC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    async def count(
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
        """Total rows matching the same filters as list() (ignores limit/offset).
        Powers the X-Total-Count header so a paginated UI knows the full size."""
        clause, params = self._where(locals())
        sql = f"SELECT count(*) AS n FROM core.cargo {clause}"
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ update
    async def update(self, container_number: str, fields: Mapping[str, Any]) -> dict:
        """Patch the writable columns supplied in ``fields`` (updated_at is set by
        the DB trigger). Returns the full updated row. Raises
        :class:`CargoNotFound` if no such container exists."""
        patch = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not patch:  # nothing to change — behave as a read (still 404 if absent)
            existing = await self.get(container_number)
            if existing is None:
                raise CargoNotFound(container_number)
            return existing
        set_clause = ", ".join(f"{k} = :{k}" for k in patch)
        params = {**patch, "container_number": container_number}
        sql = (
            f"UPDATE core.cargo SET {set_clause} "
            f"WHERE container_number = :container_number RETURNING {_SELECT_COLS}"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        if row is None:
            raise CargoNotFound(container_number)
        return dict(row)

    # ------------------------------------------------------------------ delete
    async def delete(self, container_number: str) -> bool:
        """DELETE one cargo row. Returns True if a row was removed, else False."""
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(_DELETE),
                                        {"container_number": container_number})
            return bool(result.rowcount)

    # --------------------------------------------------------------- events (log)
    async def record_event(self, event: str, container_number: str,
                           payload: Mapping[str, Any]) -> dict:
        """Append one cargo lifecycle event to ``core.cargo_event`` and return the
        stored row (with its monotonic id + server timestamp). Backs the UC-2
        notifications contract (GET /api/cargo/events)."""
        import json as _json
        params = {"event": event, "container_number": container_number,
                  "payload": _json.dumps(dict(payload or {}))}
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(_EVENT_INSERT), params)
            row = result.mappings().first()
        return dict(row) if row else dict(params)

    async def list_events(
        self,
        *,
        container_number: Optional[str] = None,
        event: Optional[str] = None,
        since_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List cargo events newest-first. ``since_id`` returns only events with a
        larger id (the poll cursor UC-2 advances each pull)."""
        conds: list[str] = []
        params: dict[str, Any] = {}
        if container_number is not None:
            conds.append("container_number = :container_number")
            params["container_number"] = container_number
        if event is not None:
            conds.append("event = :event")
            params["event"] = event
        if since_id is not None:
            conds.append("id > :since_id")
            params["since_id"] = since_id
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.update({"limit": limit, "offset": offset})
        sql = (
            f"SELECT {_EVENT_SELECT} FROM core.cargo_event {clause} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    # ----------------------------------------------------- notifications (0017)
    async def create_notification(self, container_number: str, notification_type: str,
                                  severity: str, message: Optional[str],
                                  stakeholders: Any) -> dict:
        """Insert one stakeholder notification and return the stored row (with its
        monotonic id + server timestamp). ``stakeholders`` is stored as jsonb."""
        import json as _json
        params = {
            "container_number": container_number,
            "notification_type": notification_type,
            "severity": severity,
            "message": message,
            "stakeholders": _json.dumps(list(stakeholders or [])),
        }
        sql = (
            "INSERT INTO core.cargo_notification "
            "(container_number, notification_type, severity, message, stakeholders) "
            "VALUES (:container_number, :notification_type, :severity, :message, "
            "CAST(:stakeholders AS jsonb)) "
            f"RETURNING {_NOTIF_SELECT}"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return dict(row) if row else dict(params)

    async def list_notifications(
        self,
        *,
        container_number: Optional[str] = None,
        notification_type: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List notifications newest-first with optional equality filters."""
        filters = locals()
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in _NOTIF_FILTER_COLS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"{col} = :{col}")
                params[col] = val
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.update({"limit": limit, "offset": offset})
        sql = (
            f"SELECT {_NOTIF_SELECT} FROM core.cargo_notification {clause} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    # --------------------------------------------------------- workflow (0016)
    async def record_workflow(self, container_number: str, action: str,
                              new_status: str, comment: Optional[str]) -> Optional[dict]:
        """Apply a workflow transition atomically: read the current status (locking
        the cargo row), set ``core.cargo.workflow_status`` to ``new_status``, and
        append the transition to the log. Returns the stored workflow-event row, or
        ``None`` if the container does not exist (so the service maps it to 404)."""
        async with get_engine(self._dsn).begin() as conn:
            cur = await conn.execute(
                text("SELECT workflow_status FROM core.cargo "
                     "WHERE container_number = :cn FOR UPDATE"),
                {"cn": container_number})
            existing = cur.mappings().first()
            if existing is None:
                return None
            old_status = existing["workflow_status"]
            await conn.execute(
                text("UPDATE core.cargo SET workflow_status = :ns "
                     "WHERE container_number = :cn"),
                {"ns": new_status, "cn": container_number})
            ev = await conn.execute(
                text("INSERT INTO core.cargo_workflow_event "
                     "(container_number, action, old_status, new_status, comment) "
                     "VALUES (:cn, :action, :old_status, :new_status, :comment) "
                     f"RETURNING {_WORKFLOW_SELECT}"),
                {"cn": container_number, "action": action, "old_status": old_status,
                 "new_status": new_status, "comment": comment})
            row = ev.mappings().first()
        return dict(row) if row else None

    async def list_workflow_history(self, container_number: str, *,
                                    limit: int = 100, offset: int = 0) -> list[dict]:
        """Append-only workflow transitions for one container, newest-first."""
        sql = (
            f"SELECT {_WORKFLOW_SELECT} FROM core.cargo_workflow_event "
            "WHERE container_number = :cn ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(
                text(sql), {"cn": container_number, "limit": limit, "offset": offset})
            return [dict(r) for r in result.mappings().all()]

    # ------------------------------------------------------- planning (0018)
    async def next_yard_slot(self, block: str) -> int:
        """Next free slot number in a yard block, derived from BOTH live cargo
        occupancy (core.cargo.yard_block LIKE 'B-%') and prior plans for the block —
        so repeated planning does not collide."""
        like = f"{block}-%"
        sql = (
            "SELECT (SELECT count(*) FROM core.cargo WHERE yard_block LIKE :like) + "
            "(SELECT count(*) FROM core.cargo_yard_plan WHERE assigned_block LIKE :like) AS n"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), {"like": like})
            row = result.mappings().first()
        return (int(row["n"]) if row else 0) + 1

    async def create_yard_plan(self, container_number: str, preferred_block: Optional[str],
                               assigned_block: str, priority: str) -> dict:
        params = {"container_number": container_number, "preferred_block": preferred_block,
                  "assigned_block": assigned_block, "priority": priority}
        sql = (
            "INSERT INTO core.cargo_yard_plan "
            "(container_number, preferred_block, assigned_block, priority) "
            "VALUES (:container_number, :preferred_block, :assigned_block, :priority) "
            f"RETURNING {_YARD_PLAN_SELECT}"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return dict(row) if row else dict(params)

    async def list_yarded_containers(self) -> list[dict]:
        """Every container with a live yard_block — the input to yard-optimization."""
        sql = ("SELECT container_number, yard_block FROM core.cargo "
               "WHERE yard_block IS NOT NULL ORDER BY yard_block")
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql))
            return [dict(r) for r in result.mappings().all()]

    async def create_rake_plan(self, rake_id: str, containers: Any) -> dict:
        import json as _json
        items = list(containers or [])
        params = {"rake_id": rake_id, "containers": _json.dumps(items),
                  "planned_containers": len(items)}
        sql = (
            "INSERT INTO core.cargo_rake_plan (rake_id, containers, planned_containers) "
            "VALUES (:rake_id, CAST(:containers AS jsonb), :planned_containers) "
            f"RETURNING {_RAKE_PLAN_SELECT}"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return dict(row) if row else dict(params)

    # -------------------------------------------------- lifecycle (0023)
    async def transition_lifecycle(
        self,
        container_number: str,
        *,
        target: str,
        allowed_from: set[str],
        action: str,
        actor_role: Optional[str] = None,
        note: Optional[str] = None,
        strict: bool = True,
    ) -> Optional[dict]:
        """Atomically advance ``core.cargo.lifecycle_status`` to ``target``.

        Locks the cargo row (``FOR UPDATE``), resolves the current status (falling
        back to :func:`_infer_lifecycle` when the column is NULL), and:

        * container absent            -> ``CargoNotFound`` if ``strict`` else ``None``
        * current not in ``allowed_from`` -> ``CargoTransitionError`` if ``strict``
                                             else ``None`` (no-op; the caller's
                                             primary write still stands)
        * otherwise                    -> UPDATE the status + append one row to the
                                          ``cargo_lifecycle_events`` audit log, and
                                          return the FULL updated cargo row plus the
                                          resolved ``old_status``/``new_status``.

        The update and the audit insert share one transaction, so the record's
        state and its audit trail can never diverge. The ``allowed_from`` policy is
        supplied by the service (the single source of the state machine)."""
        async with get_engine(self._dsn).begin() as conn:
            cur = await conn.execute(
                text(f"SELECT {_SELECT_COLS} FROM core.cargo "
                     "WHERE container_number = :cn FOR UPDATE"),
                {"cn": container_number})
            existing = cur.mappings().first()
            if existing is None:
                if strict:
                    raise CargoNotFound(container_number)
                return None
            current = _infer_lifecycle(existing)
            if current not in allowed_from:
                if strict:
                    raise CargoTransitionError(container_number, current, target)
                return None
            upd = await conn.execute(
                text("UPDATE core.cargo SET lifecycle_status = :ns "
                     f"WHERE container_number = :cn RETURNING {_SELECT_COLS}"),
                {"ns": target, "cn": container_number})
            row = upd.mappings().first()
            await conn.execute(
                text("INSERT INTO core.cargo_lifecycle_event "
                     "(container_number, action, old_status, new_status, actor_role, note) "
                     "VALUES (:cn, :action, :old, :new, :actor, :note)"),
                {"cn": container_number, "action": action, "old": current,
                 "new": target, "actor": actor_role, "note": note})
        out = dict(row) if row else dict(existing)
        out["_old_status"] = current
        out["_new_status"] = target
        return out

    async def list_lifecycle_events(self, container_number: str, *,
                                    limit: int = 100, offset: int = 0) -> list[dict]:
        """Append-only lifecycle transition history for one container, newest-first."""
        sql = (
            f"SELECT {_LIFECYCLE_SELECT} FROM core.cargo_lifecycle_event "
            "WHERE container_number = :cn ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(
                text(sql), {"cn": container_number, "limit": limit, "offset": offset})
            return [dict(r) for r in result.mappings().all()]

    async def list_scan_queue(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """Containers awaiting a customs scan: yard-assignment completed, NOT
        released, and NOT yet verified. Yard-assignment is detected from either the
        live ``yard_block`` or a lifecycle at/after YARD_ASSIGNED, so both the new
        lifecycle path and legacy yarded rows appear. Oldest-first (queue order)."""
        sql = (
            f"SELECT {_SELECT_COLS} FROM core.cargo "
            "WHERE is_released = false "
            "AND (yard_block IS NOT NULL OR lifecycle_status IN "
            "     ('YARD_ASSIGNED','YARD_POSITION_ALLOCATED','REEFER_PLANNED',"
            "      'RAKE_ASSIGNED','SCAN_PENDING')) "
            "AND COALESCE(lifecycle_status,'') NOT IN ('VERIFIED','RELEASED') "
            "ORDER BY created_at ASC, container_number ASC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), {"limit": limit, "offset": offset})
            return [dict(r) for r in result.mappings().all()]

    async def record_scan_verification(self, container_number: str, verified: bool,
                                       remarks: Optional[str],
                                       actor_role: Optional[str]) -> dict:
        """Insert one scan/customs verification record and return the stored row."""
        params = {"container_number": container_number, "verified": verified,
                  "remarks": remarks, "actor_role": actor_role}
        sql = (
            "INSERT INTO core.cargo_scan_verification "
            "(container_number, verified, remarks, actor_role) "
            "VALUES (:container_number, :verified, :remarks, :actor_role) "
            f"RETURNING {_SCAN_VERIF_SELECT}"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return dict(row) if row else dict(params)

    async def create_yard_position(self, container_number: str, *,
                                   assigned_block: str, yard_row: Optional[str],
                                   yard_slot: Optional[str], yard_position: Optional[str],
                                   priority: str) -> dict:
        """Record a physical yard position (block / row / slot / position) as a
        ``cargo_yard_plans`` row with the 0023 position columns populated."""
        params = {"container_number": container_number, "preferred_block": None,
                  "assigned_block": assigned_block, "priority": priority,
                  "yard_row": yard_row, "yard_slot": yard_slot,
                  "yard_position": yard_position}
        sql = (
            "INSERT INTO core.cargo_yard_plan "
            "(container_number, preferred_block, assigned_block, priority, "
            " yard_row, yard_slot, yard_position) "
            "VALUES (:container_number, :preferred_block, :assigned_block, :priority, "
            " :yard_row, :yard_slot, :yard_position) "
            f"RETURNING {_YARD_PLAN_SELECT}, yard_row, yard_slot, yard_position"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return dict(row) if row else dict(params)

    async def list_rake_plans(self, *, rake_id: Optional[str] = None,
                              limit: int = 100, offset: int = 0) -> list[dict]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        if rake_id is not None:
            conds.append("rake_id = :rake_id")
            params["rake_id"] = rake_id
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.update({"limit": limit, "offset": offset})
        sql = (
            f"SELECT {_RAKE_PLAN_SELECT} FROM core.cargo_rake_plan {clause} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    async def next_reefer_index(self) -> int:
        """Next reefer slot number, derived from prior reefer allocations."""
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) AS n FROM core.cargo_reefer_plan"))
            row = result.mappings().first()
        return (int(row["n"]) if row else 0) + 1

    async def create_reefer_plan(self, container_number: str, temperature: Any,
                                 power_required: bool, slot: str) -> dict:
        params = {"container_number": container_number, "temperature": temperature,
                  "power_required": power_required, "slot": slot}
        sql = (
            "INSERT INTO core.cargo_reefer_plan "
            "(container_number, temperature, power_required, slot) "
            "VALUES (:container_number, :temperature, :power_required, :slot) "
            f"RETURNING {_REEFER_PLAN_SELECT}"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return dict(row) if row else dict(params)
