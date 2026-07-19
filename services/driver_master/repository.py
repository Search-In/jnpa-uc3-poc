"""Driver Master persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL for the Driver Master module. It reads
jnpa.driver_master + jnpa.driver_pdp_history (Phase-1 tables) and LEFT-JOINs the
existing driver tables (jnpa.drivers / driver_enrollments / verification_logs)
read-only to derive enrollment + verification status. No writes, no ORM, no HTTP.

Reads run on a plain engine.connect() (same as services.cargo). All statements
are parameterised. The driver↔registry link is by NORMALISED licence number
(UPPER + alnum-only) — the same normalisation the Phase-1 importer used.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.driver_master.repository")


def normalize_licence(raw: Optional[str]) -> str:
    """UPPER + alnum-only — matches the Phase-1 import key (licence_no_norm)."""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


# Sortable columns (whitelist → real SQL expression). Prevents SQL injection via
# an arbitrary ORDER BY.
# Sort expressions chosen to MATCH the migration-0026 indexes so ORDER BY + LIMIT
# is an index scan (no full sort of the 31k-row registry):
#   lower(name)          -> idx_driver_master_name
#   lower(company_name)  -> idx_driver_master_company
#   licence_valid_to     -> idx_driver_master_valid
#   licence_no_norm      -> uq_driver_master_licence
_SORTS: Dict[str, str] = {
    "name": "lower(dm.name)",
    "licence": "dm.licence_no_norm",
    "company": "lower(dm.company_name)",
    "validity": "dm.licence_valid_to",
    "created": "dm.id",
}

# PDP/validity status derived from licence_valid_to. Partitions cleanly into
# ACTIVE / EXPIRING / EXPIRED / UNKNOWN.
_STATUS_EXPR = """
CASE
  WHEN dm.licence_valid_to IS NULL THEN 'UNKNOWN'
  WHEN dm.licence_valid_to < current_date THEN 'EXPIRED'
  WHEN dm.licence_valid_to <= current_date + INTERVAL '30 days' THEN 'EXPIRING'
  ELSE 'ACTIVE'
END
"""

# Enrollment status derived from the existing login tables (READ ONLY).
_ENROLL_EXPR = """
CASE
  WHEN e.driver_id IS NOT NULL THEN 'ENROLLED'
  WHEN en.enroll_status IN ('PENDING','REENROLL') THEN 'PENDING'
  WHEN en.enroll_status = 'REJECTED' THEN 'REJECTED'
  ELSE 'NOT_ENROLLED'
END
"""

# Lateral joins that resolve enrollment (jnpa.drivers), pending enrollment
# (jnpa.driver_enrollments), latest verification (jnpa.verification_logs) and the
# current PDP record (jnpa.driver_pdp_history) for each registry row.
_JOINS = """
FROM jnpa.driver_master dm
LEFT JOIN jnpa.transporters t ON t.id = dm.transporter_id
LEFT JOIN LATERAL (
    SELECT dr.driver_id, dr.status AS driver_status, dr.photo_url AS enrolled_photo_url,
           dr.vehicle_no, dr.vehicle_no_norm
    FROM jnpa.drivers dr
    WHERE coalesce(dr.license_no,'') <> ''
      AND regexp_replace(upper(dr.license_no), '[^A-Z0-9]', '', 'g') = dm.licence_no_norm
    ORDER BY (dr.status = 'ACTIVE') DESC
    LIMIT 1
) e ON true
LEFT JOIN LATERAL (
    SELECT en2.status AS enroll_status
    FROM jnpa.driver_enrollments en2
    WHERE coalesce(en2.license_no,'') <> ''
      AND regexp_replace(upper(en2.license_no), '[^A-Z0-9]', '', 'g') = dm.licence_no_norm
    ORDER BY en2.submitted_at DESC
    LIMIT 1
) en ON true
LEFT JOIN LATERAL (
    SELECT vl.decision AS verification, vl.score, vl.ts AS verified_at
    FROM jnpa.verification_logs vl
    WHERE e.driver_id IS NOT NULL AND vl.driver_id = e.driver_id
    ORDER BY vl.ts DESC
    LIMIT 1
) v ON true
LEFT JOIN LATERAL (
    SELECT ph.active AS pdp_active, ph.appl_number, ph.validity AS pdp_validity,
           ph.acceptance_time_stamp AS pdp_accepted_at
    FROM jnpa.driver_pdp_history ph
    WHERE ph.pdp_number = dm.latest_pdp_number
    ORDER BY ph.acceptance_time_stamp DESC
    LIMIT 1
) pdp ON true
"""

_SELECT_FIELDS = f"""
    dm.id, dm.licence_no, dm.licence_no_norm, dm.name, dm.company_name,
    dm.transporter_id, t.name AS transporter_name, t.code AS transporter_code,
    t.status AS transporter_status,
    dm.photo_file, dm.photo_url, dm.licence_type, dm.licence_valid_to,
    dm.latest_pdp_number, dm.dob, dm.status AS master_status,
    ({_STATUS_EXPR}) AS pdp_status,
    pdp.pdp_active, pdp.appl_number, pdp.pdp_validity,
    ({_ENROLL_EXPR}) AS enrollment_status,
    e.driver_id AS enrolled_driver_id, e.driver_status, e.vehicle_no,
    e.enrolled_photo_url,
    v.verification, v.score AS verification_score, v.verified_at
"""


# Normalised-licence sets from the (tiny) login tables — used as scalar subqueries
# so the LIST / COUNT queries filter by enrollment/verification WITHOUT the
# per-row LATERAL joins (which made count(*) over 31k rows take ~15s).
_ENROLLED_NORMS = ("SELECT regexp_replace(upper(license_no),'[^A-Z0-9]','','g') "
                   "FROM jnpa.drivers WHERE coalesce(license_no,'') <> ''")
_VERIFIED_NORMS = ("SELECT regexp_replace(upper(dr.license_no),'[^A-Z0-9]','','g') "
                   "FROM jnpa.drivers dr JOIN jnpa.verification_logs vl ON vl.driver_id = dr.driver_id "
                   "WHERE vl.decision = :verification AND coalesce(dr.license_no,'') <> ''")


def _build_where(f: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Parameterised WHERE clause over driver_master (+ transporters) only — no
    LATERAL joins, so it stays index-friendly for list AND count."""
    where: List[str] = []
    p: Dict[str, Any] = {}
    if f.get("search"):
        where.append(
            "(dm.name ILIKE :s OR dm.licence_no ILIKE :s OR dm.licence_no_norm ILIKE :s "
            "OR coalesce(dm.company_name,'') ILIKE :s "
            "OR coalesce(dm.latest_pdp_number,'') ILIKE :s "
            "OR coalesce(t.name,'') ILIKE :s)"
        )
        p["s"] = f"%{f['search']}%"
    if f.get("company"):
        where.append("(dm.company_name ILIKE :company OR coalesce(t.name,'') ILIKE :company)")
        p["company"] = f"%{f['company']}%"
    if f.get("transporter_id") is not None:
        where.append("dm.transporter_id = :transporter_id")
        p["transporter_id"] = f["transporter_id"]
    status = (f.get("status") or "").upper()
    if status == "EXPIRED":
        where.append("dm.licence_valid_to < current_date")
    elif status == "EXPIRING":
        where.append("dm.licence_valid_to >= current_date AND dm.licence_valid_to <= current_date + INTERVAL '30 days'")
    elif status == "ACTIVE":
        where.append("dm.licence_valid_to > current_date + INTERVAL '30 days'")
    elif status == "UNKNOWN":
        where.append("dm.licence_valid_to IS NULL")
    enrolled = f.get("enrolled")
    if enrolled is True:
        where.append(f"dm.licence_no_norm IN ({_ENROLLED_NORMS})")
    elif enrolled is False:
        where.append(f"dm.licence_no_norm NOT IN ({_ENROLLED_NORMS})")
    if f.get("verification"):
        where.append(f"dm.licence_no_norm IN ({_VERIFIED_NORMS})")
        p["verification"] = f["verification"].upper()
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, p


class DriverMasterRepository:
    """Raw-SQL reads for the Driver Master module. Stateless apart from the DSN."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _fetch_all(self, sql: str, params: Mapping[str, Any]) -> List[Dict[str, Any]]:
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            res = await conn.execute(text(sql), dict(params))
            return [dict(r) for r in res.mappings().all()]

    async def _fetch_one(self, sql: str, params: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        rows = await self._fetch_all(sql, params)
        return rows[0] if rows else None

    async def list_drivers(
        self, f: Mapping[str, Any], *, sort: str, direction: str, limit: int, offset: int
    ) -> Tuple[List[Dict[str, Any]], int]:
        clause, p = _build_where(f)
        sort_col = _SORTS.get(sort, "dm.name")
        dir_sql = "DESC" if str(direction).lower() == "desc" else "ASC"
        base_from = "FROM jnpa.driver_master dm LEFT JOIN jnpa.transporters t ON t.id = dm.transporter_id"
        # Page + count use ONLY driver_master + transporters (index-friendly).
        rows = await self._fetch_all(
            f"""SELECT dm.id, dm.licence_no, dm.licence_no_norm, dm.name, dm.company_name,
                       dm.transporter_id, t.name AS transporter_name, t.code AS transporter_code,
                       t.status AS transporter_status, dm.photo_file, dm.photo_url,
                       dm.licence_type, dm.licence_valid_to, dm.latest_pdp_number, dm.dob,
                       dm.status AS master_status, ({_STATUS_EXPR}) AS pdp_status
                {base_from} {clause}
                ORDER BY {sort_col} {dir_sql} NULLS LAST, dm.id ASC
                LIMIT :limit OFFSET :offset""",
            {**p, "limit": limit, "offset": offset},
        )
        total_row = await self._fetch_one(f"SELECT count(*) AS n {base_from} {clause}", p)
        total = int(total_row["n"]) if total_row else 0
        # Enrich ONLY the page rows (≤ limit) with enrollment / verification / pdp
        # derived from the login tables — cheap because it runs the LATERAL joins
        # for a handful of licences, not the whole registry.
        norms = [r["licence_no_norm"] for r in rows]
        if norms:
            enrich = await self._fetch_all(
                f"""SELECT dm.licence_no_norm,
                           ({_ENROLL_EXPR}) AS enrollment_status,
                           e.driver_id AS enrolled_driver_id, e.driver_status, e.vehicle_no,
                           v.verification, v.score AS verification_score, v.verified_at,
                           pdp.pdp_active
                    {_JOINS}
                    WHERE dm.licence_no_norm = ANY(:norms)""",
                {"norms": norms},
            )
            by_norm = {e["licence_no_norm"]: e for e in enrich}
            for r in rows:
                r.update({k: v for k, v in (by_norm.get(r["licence_no_norm"], {})).items()
                          if k != "licence_no_norm"})
        return rows, total

    async def get_driver(self, licence_norm: str) -> Optional[Dict[str, Any]]:
        return await self._fetch_one(
            f"SELECT {_SELECT_FIELDS} {_JOINS} WHERE dm.licence_no_norm = :ln",
            {"ln": licence_norm},
        )

    async def get_pdp_history(
        self, licence_norm: str, *, limit: int, offset: int
    ) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
        """Full PDP lineage for a driver: all permits sharing the appl_number of the
        driver's current (latest) PDP; falls back to the single current permit."""
        head = await self._fetch_one(
            """SELECT dm.latest_pdp_number, ph.appl_number
               FROM jnpa.driver_master dm
               LEFT JOIN jnpa.driver_pdp_history ph ON ph.pdp_number = dm.latest_pdp_number
               WHERE dm.licence_no_norm = :ln LIMIT 1""",
            {"ln": licence_norm},
        )
        if not head:
            return [], 0, None
        appl = head.get("appl_number")
        latest = head.get("latest_pdp_number")
        if appl:
            where, key = "ph.appl_number = :key", {"key": appl}
        else:
            where, key = "ph.pdp_number = :key", {"key": latest}
        rows = await self._fetch_all(
            f"""SELECT ph.pdp_id, ph.pdp_number, ph.appl_number, ph.active,
                       ph.acceptance_time_stamp, ph.validity, ph.remarks,
                       ph.pdp_cancelled_by, ph.cancellation_time
                FROM jnpa.driver_pdp_history ph
                WHERE {where}
                ORDER BY ph.acceptance_time_stamp DESC NULLS LAST
                LIMIT :limit OFFSET :offset""",
            {**key, "limit": limit, "offset": offset},
        )
        total_row = await self._fetch_one(
            f"SELECT count(*) AS n FROM jnpa.driver_pdp_history ph WHERE {where}", key
        )
        return rows, int(total_row["n"]) if total_row else 0, appl or latest

    async def stats(self) -> Dict[str, Any]:
        row = await self._fetch_one(
            """
            WITH enrolled_norms AS (
                SELECT DISTINCT regexp_replace(upper(license_no),'[^A-Z0-9]','','g') AS n
                FROM jnpa.drivers WHERE coalesce(license_no,'') <> ''
            ),
            pending_norms AS (
                SELECT DISTINCT regexp_replace(upper(license_no),'[^A-Z0-9]','','g') AS n
                FROM jnpa.driver_enrollments
                WHERE coalesce(license_no,'') <> '' AND status IN ('PENDING','REENROLL')
            )
            SELECT
              count(*) AS total_drivers,
              count(*) FILTER (WHERE dm.licence_valid_to > current_date + INTERVAL '30 days') AS active_pdp,
              count(*) FILTER (WHERE dm.licence_valid_to >= current_date
                               AND dm.licence_valid_to <= current_date + INTERVAL '30 days') AS expiring_soon,
              count(*) FILTER (WHERE dm.licence_valid_to < current_date) AS expired_pdp,
              count(DISTINCT lower(coalesce(dm.company_name,''))) FILTER (WHERE coalesce(dm.company_name,'') <> '') AS companies,
              count(*) FILTER (WHERE dm.licence_no_norm IN (SELECT n FROM enrolled_norms)) AS enrolled,
              count(*) FILTER (WHERE dm.licence_no_norm NOT IN (SELECT n FROM enrolled_norms)
                               AND dm.licence_no_norm IN (SELECT n FROM pending_norms)) AS pending_enrollment
            FROM jnpa.driver_master dm
            """,
            {},
        )
        row = row or {}
        total = int(row.get("total_drivers") or 0)
        enrolled = int(row.get("enrolled") or 0)
        return {
            "total_drivers": total,
            "active_pdp": int(row.get("active_pdp") or 0),
            "expiring_soon": int(row.get("expiring_soon") or 0),
            "expired_pdp": int(row.get("expired_pdp") or 0),
            "companies": int(row.get("companies") or 0),
            "enrolled": enrolled,
            "pending_enrollment": int(row.get("pending_enrollment") or 0),
            "not_enrolled": max(0, total - enrolled),
        }
