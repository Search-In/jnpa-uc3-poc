"""/api/facilities — T-09 Facilities & Utilities Directory.  GAP-SCR-03.

There is no facilities master table in `jnpa_schema_v3`, and inventing one would
mean typing a list of places into the codebase. So this composes the directory
from the five places the corpus actually names a facility, and tags every row
with where it came from:

  * `core.ref_terminal` (8) + `core.ref_berth` (42) — the container terminals,
    with operator and the INNSA1 site code the EDI messages use.
  * `core.perf_ldb_facility_dwell` — 29 CFS and 42 ICD named in the LDB monthly
    performance reports, each with its measured dwell.
  * `core.icd_rake_movement` — the two rail sidings, T1 and T2. These were
    invisible until the ICD daily reports were parsed (GAP-ETL-07); the twin
    previously believed no siding was recorded anywhere.
  * `core.parking_facility` — the truck lots and the Common Parking Plaza, with
    coordinates and capacity.

WEIGHBRIDGES ARE ABSENT, and that is the finding rather than a hole to fill.
`core.weighbridge_reroute` references `WB-BMCT-01` and `WB-BMCT-02`, and no
supplied file lists a weighbridge, its location or its capacity. The directory
reports that explicitly (`/api/facilities/absent`) so the driver-side locator
(D-10) can say "not supplied" rather than render an empty map that reads as a
loading failure.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.facilities")

router = APIRouter(prefix="/api/facilities", tags=["facilities"])

#: One SELECT per source. Kept separate rather than UNIONed in one statement so
#: a source that is absent on an older volume degrades to "no rows from this
#: source" instead of failing the whole directory — and so each row can carry
#: the table it came from without a CASE ladder.
_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("TERMINAL", "core.ref_terminal", """
        SELECT t.code                       AS facility_id,
               'TERMINAL'                   AS type,
               t.name                       AS name,
               t.operator                   AS operator,
               t.pcs_code                   AS site_code,
               NULL::double precision       AS lat,
               NULL::double precision       AS lon,
               NULL::integer                AS capacity,
               count(b.berth_id)            AS berth_count,
               NULL::numeric                AS dwell_hours,
               'core.ref_terminal'          AS source_table,
               '7-Berthing Reports · core.ref_berth' AS source_files
        FROM core.ref_terminal t
        LEFT JOIN core.ref_berth b ON b.terminal_id = t.terminal_id
        GROUP BY t.code, t.name, t.operator, t.pcs_code
    """),
    ("CFS_ICD", "core.perf_ldb_facility_dwell", """
        SELECT DISTINCT ON (facility_name)
               facility_name_norm           AS facility_id,
               facility_type                AS type,
               facility_name                AS name,
               NULL::text                   AS operator,
               NULL::text                   AS site_code,
               NULL::double precision       AS lat,
               NULL::double precision       AS lon,
               NULL::integer                AS capacity,
               NULL::bigint                 AS berth_count,
               dwell_hours                  AS dwell_hours,
               'core.perf_ldb_facility_dwell' AS source_table,
               '12-Performance Reports (LDB monthly dwell)' AS source_files
        FROM core.perf_ldb_facility_dwell
        WHERE facility_name IS NOT NULL
        ORDER BY facility_name, report_month DESC
    """),
    ("RAIL_SIDING", "core.icd_rake_movement", """
        SELECT DISTINCT
               track                        AS facility_id,
               'RAIL_SIDING'                AS type,
               'JNPA central rail yard — siding ' || track AS name,
               'JNPA'                       AS operator,
               NULL::text                   AS site_code,
               NULL::double precision       AS lat,
               NULL::double precision       AS lon,
               NULL::integer                AS capacity,
               NULL::bigint                 AS berth_count,
               NULL::numeric                AS dwell_hours,
               'core.icd_rake_movement'     AS source_table,
               '10-Form 11_ICD Rail/ICD_REPORTS' AS source_files
        FROM core.icd_rake_movement
        WHERE track IS NOT NULL
    """),
    ("PARKING", "core.parking_facility", """
        SELECT facility_name                AS facility_id,
               'CPP'                        AS type,
               facility_name                AS name,
               'JNPA'                       AS operator,
               NULL::text                   AS site_code,
               (location->>'lat')::double precision AS lat,
               (location->>'lon')::double precision AS lon,
               capacity                     AS capacity,
               NULL::bigint                 AS berth_count,
               NULL::numeric                AS dwell_hours,
               'core.parking_facility'      AS source_table,
               'UC-III parking model'       AS source_files
        FROM core.parking_facility
    """),
)

#: Facility classes the corpus names nowhere. Reported rather than omitted: a
#: directory that simply lacks weighbridges is indistinguishable from one that
#: failed to load them.
ABSENT: tuple[Dict[str, str], ...] = (
    {
        "type": "WEIGHBRIDGE",
        "why": ("No supplied file lists a weighbridge, its location or its "
                "capacity. `core.weighbridge_reroute` references two ids "
                "(WB-BMCT-01, WB-BMCT-02) and nothing defines them."),
        "would_need": ("A weighbridge master — id, terminal, coordinates, "
                       "lane count, operating hours."),
    },
    {
        "type": "FUEL / REST / AMENITY",
        "why": ("Driver-facing amenities are not in the corpus at all. The "
                "Gati Shakti road points (core.gs_road_point, 497 rows) are "
                "road geometry, not facilities."),
        "would_need": "An amenities layer, or permission to use a public POI source.",
    },
)


@router.get("", summary="Every facility the corpus names, with its source")
@router.get("/")
async def list_facilities(
    type: Optional[str] = Query(None, description="TERMINAL | CFS | ICD | RAIL_SIDING | CPP"),
    q: Optional[str] = Query(None, description="name contains"),
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    from jnpa_shared.db import fetch_all

    rows: List[Dict[str, Any]] = []
    unavailable: List[str] = []
    queries: List[Dict[str, Any]] = []

    for label, table, sql in _SOURCES:
        try:
            got = await fetch_all(sql, {}, dsn=state.cfg.postgres_dsn)
        except Exception as exc:  # noqa: BLE001 — one absent source must not empty the directory
            log.debug("facility_source_unavailable", source=table, error=str(exc))
            unavailable.append(table)
            queries.append({"source": table, "sql": sql.strip(),
                            "error": str(exc).splitlines()[0]})
            continue
        queries.append({"source": table, "sql": sql.strip(), "row_count": len(got)})
        rows.extend(dict(r) for r in got)

    if type:
        want = type.strip().upper()
        rows = [r for r in rows if str(r.get("type", "")).upper() == want]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in str(r.get("name", "")).lower()]

    rows.sort(key=lambda r: (str(r.get("type")), str(r.get("name"))))
    by_type: Dict[str, int] = {}
    for r in rows:
        by_type[str(r.get("type"))] = by_type.get(str(r.get("type")), 0) + 1

    REQUESTS.labels("facilities", "ok").inc()
    return {
        "facilities": rows,
        "count": len(rows),
        "by_type": by_type,
        "absent": list(ABSENT),
        "sources_unavailable": unavailable,
        "note": ("Composed from the five places the corpus names a facility; "
                 "there is no facilities master table. Every row carries the "
                 "table it came from. Classes the corpus does not name are "
                 "listed under `absent` rather than omitted."),
        "queries": queries,
    }


@router.get("/absent", summary="Facility classes the corpus does not name")
async def absent_facilities() -> Dict[str, Any]:
    """What a driver-side locator must say instead of rendering an empty map."""
    REQUESTS.labels("facilities", "ok").inc()
    return {"absent": list(ABSENT), "count": len(ABSENT)}


@router.get("/weighing", summary="Where container weighing is actually evidenced")
async def weighing_points(
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    """What D-10 (Weighbridge Locator) can honestly show.

    No weighbridge exists in the corpus — the word appears in none of the 449
    files, and the single `core.weighbridge_reroute` row is flagged
    `simulated = true`. A locator drawing weighbridge pins would therefore be
    drawing pins we invented.

    What IS evidenced is the WEIGHING: gate documents carry a VGM against a
    terminal. So this returns the terminals that recorded a verified gross mass
    and how many, which lets the driver screen answer the question actually
    behind "where do I weigh" — where weighing is recorded — without inventing
    a facility.
    """
    from jnpa_shared.db import fetch_all

    sql = """
        SELECT COALESCE(NULLIF(btrim(t.code), ''),
                        NULLIF(btrim(g.terminal_id::text), ''),
                        'unstated')                     AS terminal,
               max(t.name)                              AS terminal_name,
               count(*) FILTER (WHERE g.gross_weight_kg IS NOT NULL) AS vgm_documents,
               min(g.gross_weight_kg)  AS min_kg,
               max(g.gross_weight_kg)  AS max_kg,
               max(g.doc_ts)           AS latest_doc_ts
        FROM core.gate_document g
        -- `gate_document.terminal_id` is the numeric key into ref_terminal,
        -- not the terminal code. Joining on the code silently yields NULL names
        -- and a screen full of bare numbers.
        LEFT JOIN core.ref_terminal t ON t.terminal_id = g.terminal_id
        WHERE g.gross_weight_kg IS NOT NULL
        GROUP BY 1
        ORDER BY 3 DESC
    """
    try:
        rows = [dict(r) for r in await fetch_all(sql, {}, dsn=state.cfg.postgres_dsn)]
    except Exception as exc:  # noqa: BLE001
        log.warning("weighing_points_unavailable", error=str(exc))
        rows = []

    for r in rows:
        ts = r.get("latest_doc_ts")
        if hasattr(ts, "isoformat"):
            r["latest_doc_ts"] = ts.isoformat()

    REQUESTS.labels("facilities", "ok").inc()
    return {
        "weighing_points": rows,
        "count": len(rows),
        "weighbridges_in_corpus": 0,
        "absent": [a for a in ABSENT if a["type"] == "WEIGHBRIDGE"],
        "note": ("No weighbridge is named in any supplied file. These are the "
                 "terminals whose gate documents record a verified gross mass — "
                 "where weighing is EVIDENCED, not where a weighbridge stands."),
        "queries": [{"source": "core.gate_document", "sql": sql.strip()}],
    }
