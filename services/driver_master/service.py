"""Driver Master service orchestration — the single read entry point.

Thin over :class:`DriverMasterRepository`: owns observability (one structured log
line per op) and shapes the nested profile / validation envelopes, keeping the
router free of SQL. Stateless apart from the DSN (one shared instance is safe),
mirroring services.cargo. The repository is dependency-injected so tests can pass
a fake.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Mapping, Optional

from jnpa_shared.logging import get_logger

from .repository import DriverMasterRepository, normalize_licence

log = get_logger("services.driver_master.service")


class DriverMasterService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[DriverMasterRepository] = None) -> None:
        self._repo = repository or DriverMasterRepository(dsn=dsn)

    async def list_drivers(
        self, filters: Mapping[str, Any], *, sort: str, direction: str, limit: int, offset: int
    ) -> Dict[str, Any]:
        t0 = perf_counter()
        rows, total = await self._repo.list_drivers(
            filters, sort=sort, direction=direction, limit=limit, offset=offset)
        log.info("driver_master.list", extra={"total": total, "returned": len(rows),
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def get_profile(self, licence: str) -> Optional[Dict[str, Any]]:
        ln = normalize_licence(licence)
        row = await self._repo.get_driver(ln)
        if not row:
            return None
        return self._shape_profile(row)

    async def get_pdp_history(self, licence: str, *, limit: int, offset: int) -> Optional[Dict[str, Any]]:
        ln = normalize_licence(licence)
        # Confirm the driver exists so we can 404 distinctly from an empty history.
        exists = await self._repo.get_driver(ln)
        if not exists:
            return None
        rows, total, lineage = await self._repo.get_pdp_history(ln, limit=limit, offset=offset)
        return {"licence": exists.get("licence_no"), "appl_number": lineage,
                "items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def stats(self) -> Dict[str, Any]:
        return await self._repo.stats()

    async def validate(self, licence: str) -> Optional[Dict[str, Any]]:
        ln = normalize_licence(licence)
        row = await self._repo.get_driver(ln)
        if not row:
            return None
        pdp_status = row.get("pdp_status")
        return {
            "licence": row.get("licence_no"),
            "driver_name": row.get("name"),
            "licence_valid": pdp_status in ("ACTIVE", "EXPIRING"),
            "expired": pdp_status == "EXPIRED",
            "pdp_status": pdp_status,
            "licence_valid_to": row.get("licence_valid_to"),
            "transporter": {
                "id": row.get("transporter_id"),
                "name": row.get("transporter_name"),
                "code": row.get("transporter_code"),
                "status": row.get("transporter_status"),
                "blacklisted": (row.get("transporter_status") == "BLACKLISTED"),
            } if row.get("transporter_id") else None,
            "enrollment_status": row.get("enrollment_status"),
            "enrolled": row.get("enrollment_status") == "ENROLLED",
            "verification": row.get("verification"),
            "verified": row.get("verification") == "VERIFIED",
            "decision": "ALLOW" if pdp_status in ("ACTIVE", "EXPIRING") else "REVIEW",
        }

    # --------------------------------------------------------------- shaping
    @staticmethod
    def _shape_profile(r: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "driver": {
                "id": r.get("id"),
                "name": r.get("name"),
                "dob": r.get("dob"),
                "photo_file": r.get("photo_file"),
                "photo_url": r.get("photo_url") or r.get("enrolled_photo_url"),
                "master_status": r.get("master_status"),
            },
            "licence": {
                "licence_no": r.get("licence_no"),
                "licence_no_norm": r.get("licence_no_norm"),
                "licence_type": r.get("licence_type"),
                "valid_to": r.get("licence_valid_to"),
                "pdp_status": r.get("pdp_status"),
            },
            "transport_company": {
                "name": r.get("company_name"),
                "transporter_id": r.get("transporter_id"),
                "transporter_name": r.get("transporter_name"),
                "transporter_code": r.get("transporter_code"),
                "transporter_status": r.get("transporter_status"),
            },
            "pdp": {
                "latest_pdp_number": r.get("latest_pdp_number"),
                "appl_number": r.get("appl_number"),
                "active": r.get("pdp_active"),
                "validity": r.get("pdp_validity"),
                "status": r.get("pdp_status"),
            },
            "enrollment": {
                "status": r.get("enrollment_status"),
                "linked_driver_id": r.get("enrolled_driver_id"),
                "driver_status": r.get("driver_status"),
                "vehicle_no": r.get("vehicle_no"),
            },
            "verification": {
                "decision": r.get("verification"),
                "score": r.get("verification_score"),
                "verified_at": r.get("verified_at"),
            },
        }
