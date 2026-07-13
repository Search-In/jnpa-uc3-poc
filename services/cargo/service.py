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

from .repository import CargoConflict, CargoNotFound, CargoRepository

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
        if recorder is None:
            return
        try:
            await recorder(event, container_number, payload)
        except Exception as exc:  # noqa: BLE001 — never let notification I/O break CRUD
            log.warning("cargo.event.record_failed", event=event,
                        container_number=container_number, error=str(exc))

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
            events.append((EVENT_STATUS_CHANGED, {
                "customs_status": new.get("customs_status"),
                "previous_customs_status": old.get("customs_status")}))
        if old.get("yard_block") != new.get("yard_block") and new.get("yard_block"):
            events.append((EVENT_YARD_ASSIGNED, {"yard_block": new.get("yard_block")}))
        if old.get("gate") != new.get("gate") and new.get("gate"):
            events.append((EVENT_GATE_MOVEMENT, {
                "gate": new.get("gate"), "previous_gate": old.get("gate")}))
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
    async def create_cargo(self, row: Mapping[str, Any]) -> dict:
        t0 = perf_counter()
        try:
            out = await self._repo.create(row)
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
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        t0 = perf_counter()
        out = await self._repo.list(
            container_number=container_number, customs_status=customs_status,
            yard_block=yard_block, is_released=is_released,
            vehicle_number=vehicle_number, eseal_status=eseal_status,
            pre_document_status=pre_document_status, origin_stream=origin_stream,
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
    ) -> int:
        return await self._repo.count(
            container_number=container_number, customs_status=customs_status,
            yard_block=yard_block, is_released=is_released,
            vehicle_number=vehicle_number, eseal_status=eseal_status,
            pre_document_status=pre_document_status, origin_stream=origin_stream,
        )

    # ------------------------------------------------------------------ update
    async def update_cargo(self, container_number: str, fields: Mapping[str, Any]) -> dict:
        t0 = perf_counter()
        # Snapshot the pre-image so the diff can be turned into specific lifecycle
        # events (released / status_changed / yard_assigned / gate_movement).
        old = await self._repo.get(container_number) or {}
        try:
            out = await self._repo.update(container_number, fields)
        except CargoNotFound:
            self._observe("update", "not_found", t0, container=container_number)
            raise
        self._observe("update", "success", t0, container=container_number)
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
