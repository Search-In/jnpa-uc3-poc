"""/api/rail/sidings — corpus-backed rail siding view.  GAP-RAIL-02.

The finding this replaces
-------------------------
GAP-RAIL-02 recorded UC-2's Rail panel as un-backable, on three measured
grounds: no siding column existed in any core table, FOIS and CTO rake ids
shared zero rows, and the CTO terminal column held numeric codes. It was logged
as a product decision — redesign the panel or leave it on the simulator.

Two of the three were true only because the ICD daily reports were unparsed.
With GAP-ETL-04/07 done:

  * **The siding IS in the corpus.** `core.icd_rake_movement.track` carries
    exactly `T1` / `T2` — the same two values as `SIDING_IDS` in
    `@jnpa/schemas`. It was never missing; it was sitting in 14 PDFs nothing
    read.
  * **Rake identity now joins.** `icd_rake_movement.rake_id` matches
    `cto_manifest_entry.cto_code` for 5 distinct rakes, so placement time and
    container composition meet on the same rake for the first time. (FOIS
    remains a separate namespace — 0 rows overlap — so FOIS arrival still
    cannot be joined to either, and this endpoint does not pretend otherwise.)

What is still absent is stated per rake rather than filled in: removal and
departure times are in no supplied file, so rake TAT cannot be computed, and
`direction` is not recorded either.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from ..datewindow import DateWindow, date_window
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.rail_siding")

router = APIRouter(prefix="/api/rail", tags=["rail"])

#: The two sidings at the JNPT central rail yard, as the ICD report writes them.
SIDINGS = ("T1", "T2")

_SQL = """
SELECT i.rake_id,
       i.track                        AS siding_id,
       i.report_date,
       i.placed_at,
       i.placed_raw,
       i.discharge,
       i.source_file,
       t.terminal,
       t.containers,
       t.empties,
       t.wagons
FROM (
    -- One row per rake placement. The same rake is re-listed on consecutive
    -- daily reports while it is still on the siding, so collapse to the
    -- earliest report that mentions each placement.
    SELECT DISTINCT ON (rake_id, track, placed_raw)
           rake_id, track, report_date, placed_at, placed_raw, discharge, source_file
    FROM core.icd_rake_movement
    ORDER BY rake_id, track, placed_raw, report_date
) i
LEFT JOIN (
    SELECT upper(btrim(cto_code))                       AS cto_code,
           max(terminal)                                AS terminal,
           count(container_no)                          AS containers,
           count(*) FILTER (WHERE is_empty)             AS empties,
           count(DISTINCT wagon_no)                     AS wagons
    FROM core.cto_manifest_entry
    GROUP BY 1
) t ON t.cto_code = upper(btrim(i.rake_id))
{where}
ORDER BY i.placed_at DESC NULLS LAST, i.rake_id
LIMIT :limit
"""


@router.get("/sidings", summary="Rakes placed on a siding, from JNPA documents only")
async def rail_sidings(
    siding: Optional[str] = Query(
        None, description="T1 or T2. Omit for both."),
    window: DateWindow = Depends(date_window),
    limit: int = Query(200, ge=1, le=1000),
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    """Rake placements on the rail sidings, joined to their composition.

    Every field is read from a JNPA document. Where the corpus is silent the
    field is null and `not_in_corpus` says which — a rake with no composition is
    a rake whose CTO manifest was not supplied, and that is the answer, not a
    gap to be filled with a plausible number.
    """
    from jnpa_shared.db import fetch_all

    conds: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if siding:
        conds.append("upper(btrim(i.track)) = upper(btrim(:siding))")
        params["siding"] = siding
    if not window.is_open:
        frag, wparams = window.sql("i.placed_at")
        conds.append(frag.removeprefix(" AND ").strip())
        params.update(wparams)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = _SQL.format(where=where)

    try:
        rows = await fetch_all(sql, params, dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # noqa: BLE001 — absent table on an old volume
        log.warning("rail_sidings_unavailable", error=str(exc))
        REQUESTS.labels("rail", "error").inc()
        return {"siding": siding, "rakes": [], "count": 0,
                "error": "rail siding data is unavailable",
                "queries": [{"sql": sql, "params": _redact(params)}]}

    rakes = []
    for r in rows:
        d = dict(r)
        composed = d.get("containers") is not None and d["containers"] > 0
        rakes.append({
            "rake_id": d["rake_id"],
            "siding_id": d["siding_id"],
            "report_date": _iso(d.get("report_date")),
            "placement_ts": _iso(d.get("placed_at")),
            "placement_as_printed": d.get("placed_raw"),
            "terminal": d.get("terminal"),
            "container_count": d.get("containers") or 0,
            "empty_count": d.get("empties") or 0,
            "wagon_count": d.get("wagons") or 0,
            # Discharge composition by wagon class, as the report prints it.
            "discharge": d.get("discharge") or {},
            "source_file": d.get("source_file"),
            # Named absences, not silent nulls.
            "not_in_corpus": [
                *([] if composed else ["composition (no CTO manifest for this rake)"]),
                "removal_ts", "departure_ts", "direction",
                "fois_arrival (FOIS rake ids are a separate namespace — 0 rows join)",
            ],
        })

    REQUESTS.labels("rail", "ok").inc()
    return {
        "siding": siding,
        "rakes": rakes,
        "count": len(rakes),
        "note": ("Placement and siding come from the ICD daily reports; "
                 "composition from the CTO rake manifests. Rake TAT is NOT "
                 "computable — no supplied file records removal or departure."),
        "queries": [{"sql": sql, "params": _redact(params)}],
    }


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _redact(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (_iso(v) if hasattr(v, "isoformat") else v) for k, v in params.items()}
