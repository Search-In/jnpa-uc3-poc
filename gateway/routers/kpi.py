"""/api/kpi — materialised KPI views from Timescale + degradation telemetry.

    GET /api/kpi               -> summary KPIs (reads the materialised views)
    GET /api/kpi/{view}        -> one named KPI view's rows
    GET /api/kpi/sources       -> {source, state, last_ok, latency_p95} table
                                  (the dashboard "System Health" panel)
    GET /api/kpi/cameras       -> per-camera ANPR degradation level

The KPI views are created in infra/postgres/init.sql (continuous aggregates /
plain views named jnpa.kpi_*). The endpoint reads whichever exist and degrades
to an empty list for any that don't (so the route is robust across volumes
created before this PoC stage).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
from .anpr import KNOWN_CAMERAS, camera_state

log = get_logger("gateway.kpi")

router = APIRouter(prefix="/api/kpi", tags=["kpi"])

# Whitelisted KPI views (schema-qualified). The name segment in /api/kpi/{view}
# is validated against these keys so the path can never inject arbitrary SQL.
KPI_VIEWS: Dict[str, str] = {
    "throughput": "jnpa.kpi_gate_throughput",
    "dwell": "jnpa.kpi_gate_dwell",
    "anpr_hourly": "jnpa.kpi_anpr_hourly",
    "corridor_speed": "jnpa.kpi_corridor_speed",
    "alerts_by_kind": "jnpa.kpi_alerts_by_kind",
    "provisional_open": "jnpa.kpi_provisional_open",
    # Event-driven Appendix-C gate KPIs (fed by jnpa.gate_events).
    "gate_queue_wait": "jnpa.kpi_gate_queue_wait",
    "gate_txn_time": "jnpa.kpi_gate_txn_time",
    "tat_inside_port": "jnpa.kpi_tat_inside_port",
    "gate_trip_timeline": "jnpa.kpi_gate_trip_timeline",
}

# Idempotent DDL for the gate-event capture table + KPI views, applied at gateway
# boot so volumes created before this stage gain them without a reset. Mirrors the
# canonical definitions in infra/postgres/init.sql.
_GATE_KPI_DDL = """
CREATE TABLE IF NOT EXISTS jnpa.gate_events (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    device_id  text NOT NULL,
    plate      text,
    gate_id    text,
    trip_id    text NOT NULL,
    event_type text NOT NULL
               CHECK (event_type IN ('GATE_ARRIVAL','GATE_TXN_START','GATE_IN','GATE_OUT')),
    lat        double precision,
    lon        double precision
);
CREATE INDEX IF NOT EXISTS idx_gate_events_trip ON jnpa.gate_events (trip_id);
CREATE INDEX IF NOT EXISTS idx_gate_events_type_ts ON jnpa.gate_events (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_gate_events_ts ON jnpa.gate_events (ts DESC);
CREATE OR REPLACE VIEW jnpa.kpi_gate_trip_timeline AS
SELECT trip_id,
    max(gate_id) AS gate_id,
    max(plate) AS plate,
    min(ts) FILTER (WHERE event_type = 'GATE_ARRIVAL')   AS arrival_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_TXN_START') AS txn_start_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_IN')        AS gate_in_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_OUT')       AS gate_out_ts
FROM jnpa.gate_events
WHERE ts > now() - interval '24 hours'
GROUP BY trip_id;
CREATE OR REPLACE VIEW jnpa.kpi_gate_queue_wait AS
SELECT time_bucket('15 minutes', txn_start_ts) AS bucket,
    round(avg(EXTRACT(EPOCH FROM (txn_start_ts - arrival_ts)))::numeric/60.0, 2) AS wait_min,
    count(*) AS trips
FROM jnpa.kpi_gate_trip_timeline
WHERE arrival_ts IS NOT NULL AND txn_start_ts IS NOT NULL AND txn_start_ts >= arrival_ts
GROUP BY 1 ORDER BY 1 DESC;
CREATE OR REPLACE VIEW jnpa.kpi_gate_txn_time AS
SELECT time_bucket('15 minutes', gate_in_ts) AS bucket,
    round(avg(EXTRACT(EPOCH FROM (gate_in_ts - txn_start_ts)))::numeric/60.0, 2) AS txn_min,
    count(*) AS trips
FROM jnpa.kpi_gate_trip_timeline
WHERE txn_start_ts IS NOT NULL AND gate_in_ts IS NOT NULL AND gate_in_ts >= txn_start_ts
GROUP BY 1 ORDER BY 1 DESC;
CREATE OR REPLACE VIEW jnpa.kpi_tat_inside_port AS
SELECT time_bucket('15 minutes', gate_out_ts) AS bucket,
    round(avg(EXTRACT(EPOCH FROM (gate_out_ts - gate_in_ts)))::numeric/60.0, 2) AS tat_min,
    count(*) AS trips
FROM jnpa.kpi_gate_trip_timeline
WHERE gate_in_ts IS NOT NULL AND gate_out_ts IS NOT NULL AND gate_out_ts >= gate_in_ts
GROUP BY 1 ORDER BY 1 DESC;
"""

_GATE_SCHEMA_READY: Dict[str, bool] = {}


async def ensure_kpi_gate_schema(dsn: str | None) -> None:
    """Apply the gate-events KPI DDL once per DSN (best-effort, cached)."""
    if not dsn or _GATE_SCHEMA_READY.get(dsn):
        return
    from jnpa_shared.db import execute
    for stmt in (s.strip() for s in _GATE_KPI_DDL.split(";")):
        if stmt:
            try:
                await execute(stmt, dsn=dsn)
            except Exception as exc:  # noqa: BLE001 — one bad DDL must not abort boot
                log.warning("kpi_gate_ddl_skipped", error=str(exc), stmt=stmt[:60])
    _GATE_SCHEMA_READY[dsn] = True
    log.info("kpi_gate_schema_ready")


async def _read_view(state: GatewayState, view_sql: str, limit: int = 500) -> List[dict]:
    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(f"SELECT * FROM {view_sql} LIMIT {int(limit)}",
                               dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # view may not exist on an old volume
        log.debug("kpi_view_unavailable", view=view_sql, error=str(exc))
        return []
    out = []
    for r in rows:
        d: Dict[str, Any] = dict(r)
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        out.append(d)
    return out


@router.get("")
@router.get("/")
async def kpi_summary(state: GatewayState = Depends(get_state)) -> dict:
    """All KPI views in one payload for the dashboard summary."""
    out: Dict[str, Any] = {}
    for name, view_sql in KPI_VIEWS.items():
        out[name] = await _read_view(state, view_sql)
    REQUESTS.labels("kpi", "ok").inc()
    return {"views": out}


def _kpi_from_buckets(key: str, rows: List[dict], value_field: str):
    """Build a live KpiResult from a bucketed KPI view (rows are newest-first).

    The headline value is the trips-weighted mean across the window (stable when
    buckets are sparse); the sparkline is the per-bucket series oldest->newest.
    Returns ``None`` when the view has no usable rows (caller shows baseline).
    """
    from jnpa_shared import kpi as kpi_engine

    usable = [
        r for r in rows
        if r.get(value_field) is not None and int(r.get("trips") or 0) > 0
    ]
    if not usable:
        return None
    total_trips = sum(int(r["trips"]) for r in usable)
    if total_trips <= 0:
        return None
    value = sum(float(r[value_field]) * int(r["trips"]) for r in usable) / total_trips
    # Trend: up to the last 8 buckets, chronological (oldest -> newest).
    series = [float(r[value_field]) for r in reversed(usable[:8])]
    return kpi_engine.compute_kpi(key, round(value, 2), trend=series,
                                  source="live", n=total_trips)


async def _trt_empty_kpi(state: GatewayState):
    """TRT-empty-from-ECD from the empty-container service's computed KPI."""
    from jnpa_shared import kpi as kpi_engine
    try:
        url = state.cfg.empty_container_url.rstrip("/") + "/kpi/trt_empty"
        resp = await state.http.get(url)
        if resp.status_code == 200:
            d = resp.json()
            val = d.get("value")
            if val is not None:
                return kpi_engine.compute_kpi(
                    "trt_empty_ecd", float(val),
                    trend=d.get("trend") or None, source="live",
                    n=int(d.get("n") or 0))
    except Exception as exc:  # noqa: BLE001 — fall back to baseline
        log.debug("trt_empty_upstream_failed", error=str(exc))
    return None


@router.get("/strip")
async def kpi_strip(state: GatewayState = Depends(get_state)) -> dict:
    """The dashboard KPI strip — each KPI as {value,target,deltaPct,trend,source}.

    The four Appendix-C acceptance KPIs are computed from **real event data**:
      * gate_queue_wait / gate_txn_time / tat_inside_port — aggregated from
        jnpa.gate_events (emitted per truck gate transition) via the KPI views;
      * trt_empty_ecd — the empty-container service's computed TRT.
    Each KPI carries ``source: "live"`` when it came from event data or
    ``"baseline"`` when no data exists yet — so a placeholder is never mistaken
    for a measured value. Operational roll-ups still derive from their views.
    """
    from jnpa_shared import kpi as kpi_engine

    targets = kpi_engine.KPI_TARGETS
    results: Dict[str, dict] = {}

    # --- Appendix-C KPIs from event data ----------------------------------
    qw_rows = await _read_view(state, KPI_VIEWS["gate_queue_wait"])
    tx_rows = await _read_view(state, KPI_VIEWS["gate_txn_time"])
    tat_rows = await _read_view(state, KPI_VIEWS["tat_inside_port"])
    live = {
        "gate_queue_wait": _kpi_from_buckets("gate_queue_wait", qw_rows, "wait_min"),
        "gate_txn_time": _kpi_from_buckets("gate_txn_time", tx_rows, "txn_min"),
        "tat_inside_port": _kpi_from_buckets("tat_inside_port", tat_rows, "tat_min"),
        "trt_empty_ecd": await _trt_empty_kpi(state),
    }
    for key, res in live.items():
        if res is not None:
            results[key] = res.to_dict()

    # --- Operational roll-ups (best-effort live, else baseline) ------------
    throughput_rows = await _read_view(state, KPI_VIEWS["throughput"])
    tp_vals = [float(r["reads"]) for r in throughput_rows if r.get("reads") is not None]
    if tp_vals:
        results["gate_throughput"] = kpi_engine.compute_kpi(
            "gate_throughput", round(sum(tp_vals) / len(tp_vals), 2),
            source="live", n=len(tp_vals)).to_dict()

    # --- Fill any KPI still absent with an explicitly-labelled baseline ----
    for key, t in targets.items():
        if key not in results:
            results[key] = kpi_engine.compute_kpi(
                key, t.baseline, source="baseline", n=0).to_dict()

    # Preserve the canonical KPI order.
    strip = [results[key] for key in targets if key in results]
    live_count = sum(1 for s in strip if s.get("source") == "live")
    REQUESTS.labels("kpi", "ok").inc()
    return {"strip": strip, "count": len(strip), "live_count": live_count}


@router.get("/sources")
async def kpi_sources(state: GatewayState = Depends(get_state)) -> dict:
    """System-Health table: {source, state, last_ok, latency_p95} per source."""
    table = []
    for h in state.sources.table():
        table.append({
            "source": h.source,
            "state": h.state.value,
            "last_ok": h.last_ok.isoformat() if h.last_ok else None,
            "latency_p95_ms": h.latency_p95_ms,
            "last_decision_path": h.last_decision_path,
        })
    REQUESTS.labels("kpi", "ok").inc()
    return {"sources": table, "count": len(table)}


@router.get("/cameras")
async def kpi_cameras(state: GatewayState = Depends(get_state)) -> dict:
    """Per-camera degradation level (LIVE / CACHED / SYNTHETIC)."""
    rows = []
    for cam in KNOWN_CAMERAS:
        cs = camera_state(state, cam)
        rows.append({k: v for k, v in cs.items() if not k.startswith("_")})
    REQUESTS.labels("kpi", "ok").inc()
    return {"cameras": rows, "count": len(rows)}


@router.get("/{view}")
async def kpi_view(view: str, state: GatewayState = Depends(get_state)) -> dict:
    if view not in KPI_VIEWS:
        raise HTTPException(status_code=404,
                            detail={"error": "unknown_view", "view": view,
                                    "known": list(KPI_VIEWS)})
    rows = await _read_view(state, KPI_VIEWS[view])
    REQUESTS.labels("kpi", "ok").inc()
    return {"view": view, "rows": rows, "count": len(rows)}
