"""/api/query — S-08 Ad-hoc Query, as a CONSTRAINED surface.  GAP-SCR-02.

The decision this encodes
-------------------------
S-08 asks for "a read-only query surface over the canonical model". The obvious
reading is a SQL box. We are not building that, and the reason is not caution
about typos:

  * `assert_read_only` blocks writes, not harm. `SELECT * FROM core.driver` is a
    read, and it is a DPDP-sensitive dump of 31,846 licence records.
  * RBAC in this gateway is enforced per PATH PREFIX. A general SQL endpoint has
    one path, so a CUSTOMS token and a TRANSPORTER token reach identical data
    through it — silently undoing every scoping rule in `auth.py`.
  * A read can still take the database down. `jnpa_schema_v3` is shared with
    five other engineers; one unbounded join across 1.19M gate events is enough.

So the surface is a *query builder*, not a SQL box: the caller names a dataset
from a whitelist, the columns and filters it exposes, a date window and a limit.
The SERVER composes the SQL, and returns it in the response so the working is
still fully traceable — Notice §1(d) is satisfied by showing the query, not by
letting the user write it.

Adding a dataset is a deliberate act: one entry below, with the columns and
filters that are safe to expose and the role that may see it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query

from ..datewindow import DateWindow, date_window
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.adhoc_query")

router = APIRouter(prefix="/api/query", tags=["query"])

MAX_ROWS = 500


@dataclass(frozen=True)
class Dataset:
    """One queryable view of the canonical model."""
    key: str
    label: str
    table: str
    #: Columns returned, in order. Nothing outside this list is ever selected —
    #: `SELECT *` would leak whatever a later migration adds.
    columns: Sequence[str]
    #: Columns a caller may filter on, by exact (case-insensitive) match.
    filters: Sequence[str] = ()
    #: Timestamp a date window applies to, if the dataset has one.
    date_col: Optional[str] = None
    #: Default ordering. Fixed server-side: an ORDER BY the client controls is
    #: another way to make an unbounded scan.
    order_by: str = ""
    #: What this dataset is, and where it came from.
    note: str = ""


#: The whitelist. Deliberately excludes every table carrying personal data
#: (`core.driver` licences, `core.app_user`, identity/biometrics) — those are
#: reachable only through their own RBAC-scoped endpoints, and an ad-hoc surface
#: must not become a side door around that.
DATASETS: tuple[Dataset, ...] = (
    Dataset("cargo", "Containers (shared cargo record)", "core.cargo",
            ("container_number", "vessel_name", "direction", "lifecycle_status",
             "customs_status", "yard_block", "is_released", "eta", "source_igm_no"),
            ("container_number", "vessel_name", "direction", "lifecycle_status",
             "customs_status"),
            "eta", "eta DESC NULLS LAST",
            "One row per container known to the twin (11,957)."),
    Dataset("igm", "Import manifests (IGM headers)", "core.igm",
            ("igm_no", "igm_date", "imo_no", "vessel_code", "voyage_no",
             "line_code", "terminal_code", "declared_lines", "eta"),
            ("igm_no", "imo_no", "vessel_code", "line_code", "terminal_code"),
            "igm_date", "igm_date DESC NULLS LAST",
            "CHPOI03 manifests. Note: these carry NO vessel name — only an IMO "
            "and a call sign (defect B7)."),
    Dataset("igm_container", "Manifested containers", "core.igm_line_container",
            ("igm_no", "line_no", "container_no", "seal_no", "iso_code", "status"),
            ("igm_no", "container_no", "iso_code", "status"),
            None, "igm_no, line_no",
            "The container lines of the IGMs (11,914)."),
    Dataset("berthing", "Vessel calls (berthing reports)",
            "core.berthing_report_vessel",
            ("vessel_name", "via_no", "berth_id", "service", "line_code",
             "eta", "ata", "atd", "total_moves", "import_teu", "export_teu"),
            ("vessel_name", "via_no", "berth_id", "line_code"),
            "eta", "COALESCE(eta, ata) DESC NULLS LAST",
            "Per-terminal berthing reports (June + the 20-26 July week)."),
    Dataset("gate_document", "Gate documents", "core.gate_document",
            ("doc_category", "doc_ref", "container_no", "vehicle_no",
             "vessel_name", "voyage", "gate_no", "doc_ts"),
            ("doc_category", "doc_ref", "container_no", "vehicle_no", "vessel_name"),
            "doc_ts", "doc_ts DESC NULLS LAST",
            "Parsed Form 13 / EIR / PIN documents. Driver names are NOT exposed "
            "here — use /api/gate-docs, which is role-scoped."),
    Dataset("codeco", "CODECO gate movements", "core.codeco_movement",
            ("container_no", "vehicle_no", "vcn", "gate_pass_no", "gate_no",
             "delivery_mode", "iso_code", "gate_pass_ts"),
            ("container_no", "vehicle_no", "vcn", "gate_pass_no"),
            "gate_pass_ts", "gate_pass_ts DESC NULLS LAST",
            "EDI gate-out messages."),
    Dataset("cfs_ecy", "CFS / empty-yard movements", "core.cfs_ecy_movement",
            ("container_no", "facility", "mode", "event_ts"),
            ("container_no", "facility", "mode"),
            "event_ts", "event_ts DESC NULLS LAST",
            "Off-dock container logistics (CODECO from CFS and ECY)."),
    Dataset("icd_pendency", "ICD destination-wise pendency",
            "core.icd_fpd_pendency",
            ("report_date", "terminal", "series", "fpd_code", "teu"),
            ("terminal", "series", "fpd_code"),
            "report_date", "report_date DESC, terminal, fpd_code",
            "Daily rail pendency in TEUs. 20 of 2,940 cells do not reconcile — "
            "a defect in the source report (D14), stored as printed."),
    Dataset("icd_rake", "ICD rake placements", "core.icd_rake_movement",
            ("report_date", "rake_id", "track", "placed_at", "placed_raw", "discharge"),
            ("rake_id", "track"),
            "placed_at", "placed_at DESC NULLS LAST",
            "Rake placement and discharge composition."),
    Dataset("fois", "FOIS train intimations", "core.fois_train_intimation",
            ("rake_id", "rake_name", "units", "station_from", "station_to",
             "loaded_empty_flag", "eda", "edd"),
            ("rake_id", "station_from", "station_to", "loaded_empty_flag"),
            "eda", "eda DESC NULLS LAST",
            "Rail arrivals. FOIS rake ids do NOT join to CTO or ICD rake ids "
            "(defect A9) — 0 rows overlap."),
    Dataset("cto", "CTO rake manifests", "core.cto_manifest_entry",
            ("cto_code", "wagon_no", "container_no", "is_empty", "box_size",
             "line_code", "pol", "pod", "terminal", "event_ts"),
            ("cto_code", "container_no", "terminal", "line_code"),
            "event_ts", "event_ts DESC NULLS LAST",
            "Container composition per rake."),
    Dataset("free_time", "Container free-day allowance", "core.container_free_time",
            ("container_no", "igm_no", "line_no", "free_days", "commencement_basis",
             "commenced_at", "extracted_from", "provenance"),
            ("container_no", "igm_no", "free_days"),
            "commenced_at", "commenced_at DESC NULLS LAST, container_no",
            "Free-day allowance read out of the IGM goods description — no "
            "structured field exists. Covers 627 of 12,235 containers (5.1%); "
            "the rest state no term. NO TARIFF exists in the corpus, so no "
            "charge is derivable from this."),
    Dataset("berth_decision", "Berth allocation decisions",
            "core.berth_allocation_decision",
            ("decision_id", "call_id", "vessel_name", "berth_code", "from_position",
             "to_position", "reason_code", "reason_note", "actor", "actor_role",
             "decided_at"),
            ("call_id", "reason_code", "actor"),
            "decided_at", "decided_at DESC",
            "Append-only log of berth queue re-ordering (F-15): which call moved, "
            "why, on whose authority and when."),
    Dataset("form11", "Form 11 export pre-advice", "core.form11_entry",
            ("terminal", "container_no", "via", "icd_location", "booking_number",
             "pod", "line_code", "status", "gross_weight"),
            ("terminal", "container_no", "via", "icd_location"),
            None, "terminal, container_no",
            "Rail export pre-advice (3 rows)."),
)

_BY_KEY = {d.key: d for d in DATASETS}


@router.get("/datasets", summary="What can be queried, and how")
async def list_datasets() -> Dict[str, Any]:
    """The whitelist, so a client can build a form rather than guess."""
    return {
        "datasets": [{
            "key": d.key, "label": d.label, "table": d.table,
            "columns": list(d.columns), "filters": list(d.filters),
            "date_column": d.date_col, "note": d.note,
        } for d in DATASETS],
        "max_rows": MAX_ROWS,
        "note": ("This is a query BUILDER, not a SQL box. The server composes "
                 "every statement and returns it with the results, so the "
                 "working is traceable without the client choosing the SQL."),
    }


@router.get("/{dataset}", summary="Run one whitelisted query and show its SQL")
async def run_query(
    dataset: str,
    request_filters: Optional[str] = Query(
        None, alias="filters",
        description="Comma-separated col=value pairs, e.g. "
                    "'vessel_name=XIN HANG ZHOU,direction=IMPORT'. Only the "
                    "dataset's declared filter columns are accepted."),
    window: DateWindow = Depends(date_window),
    limit: int = Query(100, ge=1, le=MAX_ROWS),
    offset: int = Query(0, ge=0),
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    ds = _BY_KEY.get(dataset)
    if ds is None:
        raise HTTPException(status_code=404, detail={
            "error": "unknown_dataset", "dataset": dataset,
            "known": sorted(_BY_KEY)})

    conds: List[str] = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    rejected: List[str] = []

    for i, pair in enumerate((request_filters or "").split(",")):
        pair = pair.strip()
        if not pair:
            continue
        col, _, value = pair.partition("=")
        col, value = col.strip(), value.strip()
        if col not in ds.filters:
            # Named, not ignored: a filter that silently does nothing returns a
            # wider result set than the caller believes they asked for.
            rejected.append(col)
            continue
        # The column is a fixed identifier from the dataset; the value is bound.
        key = f"f{i}"
        conds.append(f"upper(btrim({col}::text)) = upper(btrim(:{key}))")
        params[key] = value

    if rejected:
        raise HTTPException(status_code=400, detail={
            "error": "unfilterable_column", "columns": rejected,
            "allowed": list(ds.filters)})

    window_applied = False
    if not window.is_open:
        if not ds.date_col:
            raise HTTPException(status_code=400, detail={
                "error": "dataset_has_no_date_column", "dataset": ds.key,
                "detail": f"{ds.label} carries no timestamp to window on."})
        frag, wparams = window.sql(ds.date_col)
        conds.append(frag.removeprefix(" AND ").strip())
        params.update(wparams)
        window_applied = True

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    order = f"ORDER BY {ds.order_by}" if ds.order_by else ""
    sql = (f"SELECT {', '.join(ds.columns)} FROM {ds.table} "
           f"{where} {order} LIMIT :limit OFFSET :offset").replace("  ", " ").strip()

    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(sql, params, dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # noqa: BLE001 — a missing table must not 500 opaquely
        log.warning("adhoc_query_failed", dataset=ds.key, error=str(exc))
        REQUESTS.labels("query", "error").inc()
        return {"dataset": ds.key, "rows": [], "count": 0,
                "error": str(exc).splitlines()[0],
                "sql": sql, "params": _serialise(params)}

    REQUESTS.labels("query", "ok").inc()
    return {
        "dataset": ds.key,
        "label": ds.label,
        "table": ds.table,
        "rows": [_serialise(dict(r)) for r in rows],
        "count": len(rows),
        "truncated": len(rows) == limit,
        "window_applied": window_applied,
        "note": ds.note,
        # Notice §1(d): the working, verbatim.
        "sql": sql,
        "params": _serialise(params),
    }


def _serialise(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in d.items()}
