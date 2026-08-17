"""/api/cargo/free-time — the free-day clock.  GAP-FLOW-05 / flow F-05.

F-05 asked for a CHARGE clock. This is not one, deliberately.

Not one file in the 449 carries a demurrage or detention RATE, and no supplied
document states when free time commences (discharge? entry inwards?
out-of-charge? each gives a different answer). A screen showing rupees would be
showing a number we invented and JNPA could not check.

What the corpus does carry is the ALLOWANCE, typed by the shipper into the IGM
goods description — "14 FREE DAYS AT POD". 627 of 12,235 containers (5.1%) state
one. So the clock reports DAYS USED AGAINST THE ALLOWANCE, names the commencement
basis it used, and says plainly for the other 94.9% that no term was stated.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from ..datewindow import DateWindow, date_window, window_cond
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.free_time")

router = APIRouter(prefix="/api/cargo/free-time", tags=["cargo"])


@router.get("", summary="Free-day allowance vs days elapsed, per container")
@router.get("/")
async def free_time(
    container_no: Optional[str] = Query(None),
    expiring_within_days: Optional[int] = Query(
        None, ge=0, le=60,
        description="Only containers whose remaining free days are at or below this."),
    expired_only: bool = Query(False),
    window: DateWindow = Depends(date_window),
    limit: int = Query(200, ge=1, le=1000),
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    from jnpa_shared.db import fetch_all

    conds: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if container_no:
        conds.append("upper(btrim(f.container_no)) = upper(btrim(:container_no))")
        params["container_no"] = container_no
    cond = window_cond(window, "f.commenced_at", params)
    if cond:
        conds.append(cond)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    # `days_elapsed` is measured to now(); a container that has since left is
    # still counted from commencement, because the corpus records no departure
    # for most boxes and stopping the clock at a gate-out we do not have would
    # understate every figure.
    sql = f"""
        SELECT f.container_no, f.igm_no, f.line_no,
               f.free_days, f.extracted_from,
               f.commencement_basis, f.commenced_at,
               f.provenance, f.source_file,
               floor(EXTRACT(EPOCH FROM (now() - f.commenced_at)) / 86400)::int AS days_elapsed,
               (f.free_days - floor(EXTRACT(EPOCH FROM (now() - f.commenced_at)) / 86400))::int
                   AS days_remaining
        FROM core.container_free_time f
        {where}
        ORDER BY days_remaining ASC, f.container_no
        LIMIT :limit
    """
    try:
        rows = [dict(r) for r in await fetch_all(sql, params, dsn=state.cfg.postgres_dsn)]
    except Exception as exc:  # noqa: BLE001
        log.warning("free_time_unavailable", error=str(exc))
        REQUESTS.labels("cargo", "error").inc()
        return {"containers": [], "count": 0, "error": str(exc).splitlines()[0]}

    for r in rows:
        if hasattr(r.get("commenced_at"), "isoformat"):
            r["commenced_at"] = r["commenced_at"].isoformat()
        r["expired"] = (r.get("days_remaining") is not None and r["days_remaining"] < 0)

    if expired_only:
        rows = [r for r in rows if r["expired"]]
    elif expiring_within_days is not None:
        rows = [r for r in rows
                if r.get("days_remaining") is not None
                and r["days_remaining"] <= expiring_within_days]

    try:
        total = await fetch_all(
            "SELECT (SELECT count(*) FROM core.igm_line_container) AS manifested, "
            "       (SELECT count(*) FROM core.container_free_time) AS with_term",
            {}, dsn=state.cfg.postgres_dsn)
        counts = dict(total[0]) if total else {}
    except Exception:  # noqa: BLE001
        counts = {}

    REQUESTS.labels("cargo", "ok").inc()
    return {
        "containers": rows,
        "count": len(rows),
        "expired": sum(1 for r in rows if r["expired"]),
        "coverage": counts,
        "charge_computed": False,
        "note": ("Days against the stated allowance. NO CHARGE is computed: the "
                 "corpus carries no demurrage or detention tariff, and no "
                 "document states when free time commences — this clock runs "
                 "from IGM entry inwards, which is recorded per row so it can "
                 "be recomputed if JNPA states a different rule. Containers "
                 "with no stated term are absent from this list by definition."),
        "sql": sql.strip(),
    }
