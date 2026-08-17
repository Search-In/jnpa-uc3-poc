"""/api/fleet — D-13 Fleet View.  GAP-SCR-08.

A transporter's own vehicles, with the PROVENANCE of each plate→company link.

Why the provenance is the point
-------------------------------
`11-Transport Data` contains no vehicle-registration column anywhere (defect
B1), so no plate can be resolved to a company through JNPA's own masters. The
11 links we have came from two different places and are not equally strong:

  * 3 are DOCUMENT_EVIDENCED — read off a Form 13 or a PIN ticket that names
    both the plate and the transporter.
  * 8 are SYNTHETIC under assumption A-G6 — generated so the flow could be
    demonstrated end to end.

A fleet list that showed eleven rows identically would present eight assumptions
as records. Each row therefore carries how it was established, and the count of
each is shown above the list rather than left to be inferred by scrolling.

Scoping
-------
A TRANSPORTER token sees ONLY the fleet of the company its account is linked to,
via `core.transporter.user_id`. If that link does not resolve the response is
empty with a stated reason — never the whole fleet. Falling back to "show
everything" when identity is unclear is how a transport partner ends up reading
a competitor's vehicle list.

Control-room roles may pass `?transporter_id=` to inspect any company, which is
their existing audience for `/api/transporters`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request

from ..datewindow import DateWindow, date_window, window_cond
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.fleet")

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

CONTROL_ROOM_ROLES = {"JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS", "CUSTOMS"}


async def _resolve_transporter(request: Request, state: GatewayState,
                               explicit: Optional[int]) -> tuple[Optional[int], Optional[str]]:
    """Which company's fleet may this caller see? Returns (id, refusal reason)."""
    from jnpa_shared.db import fetch_all

    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    username = getattr(principal, "sub", None)

    if explicit is not None:
        if role is None or role in CONTROL_ROOM_ROLES:
            return explicit, None
        # A transporter asking for someone else's id is refused outright rather
        # than silently redirected to their own — the answer should not look
        # like it worked.
        return None, ("transporter_id may only be supplied by a control-room "
                      "role; your account sees its own fleet only")

    if not username:
        # Open demo profile with no auth: no identity, so no scoped fleet.
        return None, ("no authenticated account, so no fleet can be scoped; "
                      "a control-room role may pass ?transporter_id=")

    rows = await fetch_all(
        """
        SELECT t.id
        FROM core.transporter t
        JOIN core.app_user u ON u.user_id = t.user_id
        WHERE lower(u.username) = lower(:username)
        LIMIT 1
        """, {"username": username}, dsn=state.cfg.postgres_dsn)
    if rows:
        return int(dict(rows[0])["id"]), None
    return None, ("this account is not linked to a transporter company "
                  "(core.transporter.user_id)")


@router.get("", summary="My fleet, with how each plate→company link was established")
@router.get("/")
async def my_fleet(
    request: Request,
    transporter_id: Optional[int] = Query(
        None, description="Control-room roles only: inspect another company."),
    window: DateWindow = Depends(date_window),
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    from jnpa_shared.db import fetch_all

    tid, refused = await _resolve_transporter(request, state, transporter_id)
    if tid is None:
        REQUESTS.labels("fleet", "ok").inc()
        return {"transporter_id": None, "company": None, "vehicles": [],
                "count": 0, "by_provenance": {}, "reason": refused}

    params: Dict[str, Any] = {"tid": tid}
    cond = window_cond(window, "tv.created_at", params)
    where = f" AND {cond}" if cond else ""

    sql = f"""
        SELECT tv.vehicle_no,
               tv.vehicle_no_norm,
               tv.provenance,
               tv.assumption_ref,
               tv.source_ref,
               tv.created_at,
               t.company_name,
               -- Blacklist is a fact about the COMPANY, not the vehicle, but a
               -- fleet screen is where it matters operationally.
               (SELECT count(*) FROM core.transporter_blacklist bl
                 WHERE bl.transporter_id = t.id) AS company_blacklist_entries,
               (SELECT count(*) FROM core.container_job_assignment j
                 WHERE upper(regexp_replace(btrim(j.vehicle_no), '[^A-Za-z0-9]', '', 'g'))
                     = upper(tv.vehicle_no_norm)) AS jobs,
               (SELECT max(g.doc_ts) FROM core.gate_document g
                 WHERE upper(regexp_replace(btrim(g.vehicle_no), '[^A-Za-z0-9]', '', 'g'))
                     = upper(tv.vehicle_no_norm)) AS last_gate_document_ts
        FROM core.transporter_vehicle tv
        JOIN core.transporter t ON t.id = tv.transporter_id
        WHERE tv.transporter_id = :tid{where}
        ORDER BY tv.provenance, tv.vehicle_no
    """
    try:
        rows = [dict(r) for r in await fetch_all(sql, params, dsn=state.cfg.postgres_dsn)]
    except Exception as exc:  # noqa: BLE001
        log.warning("fleet_query_failed", transporter_id=tid, error=str(exc))
        REQUESTS.labels("fleet", "error").inc()
        return {"transporter_id": tid, "company": None, "vehicles": [], "count": 0,
                "by_provenance": {}, "error": str(exc).splitlines()[0],
                "sql": sql.strip()}

    for r in rows:
        for k in ("created_at", "last_gate_document_ts"):
            if hasattr(r.get(k), "isoformat"):
                r[k] = r[k].isoformat()

    by_prov: Dict[str, int] = {}
    for r in rows:
        key = str(r.get("provenance") or "UNSTATED")
        by_prov[key] = by_prov.get(key, 0) + 1

    REQUESTS.labels("fleet", "ok").inc()
    return {
        "transporter_id": tid,
        "company": rows[0]["company_name"] if rows else None,
        "vehicles": rows,
        "count": len(rows),
        "by_provenance": by_prov,
        "note": ("`provenance` describes how the plate was linked to this "
                 "company, not the plate itself. DOCUMENT_EVIDENCED means a "
                 "gate document names both; SYNTHETIC means the link was "
                 "generated under the stated assumption, because "
                 "`11-Transport Data` carries no vehicle-registration column "
                 "to resolve one (defect B1)."),
        "sql": sql.strip(),
    }
