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

    # ------------------------------------------------------------------ create
    async def create_cargo(self, row: Mapping[str, Any]) -> dict:
        t0 = perf_counter()
        try:
            out = await self._repo.create(row)
        except CargoConflict:
            self._observe("create", "conflict", t0, container=row.get("container_number"))
            raise
        self._observe("create", "success", t0, container=out.get("container_number"))
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
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        t0 = perf_counter()
        out = await self._repo.list(
            container_number=container_number, customs_status=customs_status,
            yard_block=yard_block, is_released=is_released,
            vehicle_number=vehicle_number, limit=limit, offset=offset,
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
    ) -> int:
        return await self._repo.count(
            container_number=container_number, customs_status=customs_status,
            yard_block=yard_block, is_released=is_released,
            vehicle_number=vehicle_number,
        )

    # ------------------------------------------------------------------ update
    async def update_cargo(self, container_number: str, fields: Mapping[str, Any]) -> dict:
        t0 = perf_counter()
        try:
            out = await self._repo.update(container_number, fields)
        except CargoNotFound:
            self._observe("update", "not_found", t0, container=container_number)
            raise
        self._observe("update", "success", t0, container=container_number)
        return out

    # ------------------------------------------------------------------ delete
    async def delete_cargo(self, container_number: str) -> bool:
        t0 = perf_counter()
        removed = await self._repo.delete(container_number)
        self._observe("delete", "success" if removed else "not_found", t0, container=container_number)
        return removed
