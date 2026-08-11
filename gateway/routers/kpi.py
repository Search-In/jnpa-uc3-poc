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

import os

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
    "throughput": "mart.v_gate_throughput",
    "dwell": "mart.v_gate_dwell",
    "anpr_hourly": "mart.v_anpr_hourly",
    "corridor_speed": "mart.v_corridor_speed",
    "alerts_by_kind": "mart.v_alerts_by_kind",
    "provisional_open": "mart.v_provisional_open",
    # Event-driven Appendix-C gate KPIs (fed by core.gate_event).
    "gate_queue_wait": "mart.v_gate_queue_wait",
    "gate_txn_time": "mart.v_gate_txn_time",
    "tat_inside_port": "mart.v_tat_inside_port",
    "gate_trip_timeline": "mart.v_gate_trip_timeline",
}

# Idempotent DDL for the gate-event capture table + KPI views, applied at gateway
# boot so volumes created before this stage gain them without a reset. Mirrors the
# canonical definitions in infra/postgres/init.sql.
_GATE_KPI_DDL = """
CREATE TABLE IF NOT EXISTS core.gate_event (
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
CREATE INDEX IF NOT EXISTS idx_gate_events_trip ON core.gate_event (trip_id);
CREATE INDEX IF NOT EXISTS idx_gate_events_type_ts ON core.gate_event (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_gate_events_ts ON core.gate_event (ts DESC);
CREATE OR REPLACE VIEW mart.v_gate_trip_timeline AS
SELECT trip_id,
    max(gate_id) AS gate_id,
    max(plate) AS plate,
    min(ts) FILTER (WHERE event_type = 'GATE_ARRIVAL')   AS arrival_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_TXN_START') AS txn_start_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_IN')        AS gate_in_ts,
    min(ts) FILTER (WHERE event_type = 'GATE_OUT')       AS gate_out_ts
FROM core.gate_event
WHERE ts > now() - interval '24 hours'
GROUP BY trip_id;
CREATE OR REPLACE VIEW mart.v_gate_queue_wait AS
SELECT time_bucket('15 minutes', txn_start_ts) AS bucket,
    round(avg(EXTRACT(EPOCH FROM (txn_start_ts - arrival_ts)))::numeric/60.0, 2) AS wait_min,
    count(*) AS trips
FROM mart.v_gate_trip_timeline
WHERE arrival_ts IS NOT NULL AND txn_start_ts IS NOT NULL AND txn_start_ts >= arrival_ts
GROUP BY 1 ORDER BY 1 DESC;
CREATE OR REPLACE VIEW mart.v_gate_txn_time AS
SELECT time_bucket('15 minutes', gate_in_ts) AS bucket,
    round(avg(EXTRACT(EPOCH FROM (gate_in_ts - txn_start_ts)))::numeric/60.0, 2) AS txn_min,
    count(*) AS trips
FROM mart.v_gate_trip_timeline
WHERE txn_start_ts IS NOT NULL AND gate_in_ts IS NOT NULL AND gate_in_ts >= txn_start_ts
GROUP BY 1 ORDER BY 1 DESC;
CREATE OR REPLACE VIEW mart.v_tat_inside_port AS
SELECT time_bucket('15 minutes', gate_out_ts) AS bucket,
    round(avg(EXTRACT(EPOCH FROM (gate_out_ts - gate_in_ts)))::numeric/60.0, 2) AS tat_min,
    count(*) AS trips
FROM mart.v_gate_trip_timeline
WHERE gate_in_ts IS NOT NULL AND gate_out_ts IS NOT NULL AND gate_out_ts >= gate_in_ts
GROUP BY 1 ORDER BY 1 DESC;
"""

_GATE_SCHEMA_READY: Dict[str, bool] = {}


async def ensure_kpi_gate_schema(dsn: str | None) -> None:
    """Apply the gate-events KPI DDL once per DSN (best-effort, cached)."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
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
    """TRT-empty-from-ECD (KPI 3), preferring the REAL CFS/ECY gate log.

    Two sources, tried in order:

      1. UC3-003 — the imported CODECO gate events in core.container_event,
         scored by mart.v_empty_container_trt: the mean ECY-gate-out ->
         CFS-gate-in leg over every container with a COMPLETE
         ECY-Out -> CFS-In -> CFS-Out chain. This is measured from the
         customer's own data, so it wins whenever any complete chain exists.
      2. The empty-container service's optimiser estimate, which is what the
         strip showed before UC3-003 and remains the fallback for a database
         with no CODECO import.

    Returning ``None`` from both leaves the caller to show the labelled baseline.
    """
    from jnpa_shared import kpi as kpi_engine

    # --- 1. real gate-log TRT ------------------------------------------------
    try:
        from services.cfs_ecy import EmptyTrtService
        res = await EmptyTrtService(dsn=state.cfg.postgres_dsn or None).kpi()
        if (res.get("kpi") or {}).get("source") == "live":
            return kpi_engine.compute_kpi(
                "trt_empty_ecd", float(res["kpi"]["value"]),
                trend=res["kpi"].get("trend") or None, source="live",
                n=int(res["kpi"].get("n") or 0))
    except Exception as exc:  # noqa: BLE001 — fall through to the upstream service
        log.debug("trt_empty_codeco_failed", error=str(exc))

    # --- 2. empty-container service estimate ---------------------------------
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
        core.gate_event (emitted per truck gate transition) via the KPI views;
      * trt_empty_ecd — the mean ECY-gate-out -> CFS-gate-in leg of every
        COMPLETE empty-container chain in the imported CFS/ECY CODECO gate log
        (UC3-003), falling back to the empty-container service's estimate.
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



#: Per-trip duration expressions over mart.v_gate_trip_timeline, reusing the SAME
#: predicates the mart views use so a distribution and its headline mean are
#: always computed from the same population.
#:
#: These are PER-TRIP, not per-bucket. A P90 taken over hourly means is not the
#: 90th percentile of anything an operator experiences — it hides exactly the
#: tail it is asked to expose — so the distribution reads the trip rows directly.
_TRIP_METRICS: Dict[str, Dict[str, str]] = {
    "gate_queue_wait": {
        "value": "EXTRACT(EPOCH FROM (txn_start_ts - arrival_ts)) / 60.0",
        "at": "txn_start_ts",
        "where": ("arrival_ts IS NOT NULL AND txn_start_ts IS NOT NULL "
                  "AND txn_start_ts >= arrival_ts"),
    },
    "gate_txn_time": {
        "value": "EXTRACT(EPOCH FROM (gate_in_ts - txn_start_ts)) / 60.0",
        "at": "gate_in_ts",
        "where": ("txn_start_ts IS NOT NULL AND gate_in_ts IS NOT NULL "
                  "AND gate_in_ts >= txn_start_ts"),
    },
    "tat_inside_port": {
        "value": "EXTRACT(EPOCH FROM (gate_out_ts - gate_in_ts)) / 60.0",
        "at": "gate_out_ts",
        "where": ("gate_in_ts IS NOT NULL AND gate_out_ts IS NOT NULL "
                  "AND gate_out_ts >= gate_in_ts"),
    },
}


@router.get("/distribution")
async def distribution(
    window_hours: int = 24,
    state: GatewayState = Depends(get_state),
) -> dict:
    """Daily average, 90th percentile and peak-hour ratio per KPI (UC3-035).

    All three are computed IN THE DATABASE from per-trip rows:

      daily_average    mean of every trip's duration in the window
      p90              percentile_cont(0.9) over those same trip durations
      peak_hour_ratio  busiest hour's mean / the window mean, with the hour named

    A KPI with no trips in the window returns nulls and ``samples: 0`` rather
    than a zero — an unmeasured KPI and a KPI measured at zero are different
    facts, and only one of them is good news.
    """
    from jnpa_shared import kpi as kpi_engine
    from jnpa_shared.db import fetch_all

    out: Dict[str, Any] = {}
    for key, m in _TRIP_METRICS.items():
        target = kpi_engine.KPI_TARGETS.get(key)
        entry: Dict[str, Any] = {
            "key": key,
            "label": target.label if target else key,
            "unit": target.unit if target else "min",
            "target": target.target if target else None,
            "baseline": target.baseline if target else None,
            "window_hours": window_hours,
            "daily_average": None,
            "median": None,
            "p90": None,
            "peak_hour_ratio": None,
            "peak_hour_utc": None,
            "peak_hour_mean": None,
            "samples": 0,
            "source": "baseline",
            "skew_warning": None,
            "method": {
                "daily_average": "mean of per-trip durations in the window",
                "median": "percentile_cont(0.5) over per-trip durations",
                "p90": "percentile_cont(0.9) over per-trip durations (not over hourly means)",
                "peak_hour_ratio": "busiest hour's mean / window mean",
                "population": "mart.v_gate_trip_timeline",
            },
        }
        try:
            rows = await fetch_all(
                f"""
                WITH trips AS (
                    SELECT {m['value']} AS v,
                           date_trunc('hour', {m['at']}) AS hr
                      FROM mart.v_gate_trip_timeline
                     WHERE {m['where']}
                       AND {m['at']} > now() - make_interval(hours => :h)
                ),
                per_hour AS (
                    SELECT hr, avg(v) AS hour_mean FROM trips GROUP BY hr
                )
                SELECT (SELECT count(*) FROM trips)                        AS samples,
                       (SELECT avg(v) FROM trips)                          AS daily_average,
                       (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY v)
                          FROM trips)                                      AS p50,
                       (SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY v)
                          FROM trips)                                      AS p90,
                       (SELECT max(hour_mean) FROM per_hour)               AS peak_hour_mean,
                       (SELECT hr FROM per_hour ORDER BY hour_mean DESC LIMIT 1) AS peak_hour
                """,
                {"h": window_hours}, dsn=state.cfg.postgres_dsn,
            )
        except Exception as exc:  # noqa: BLE001 — an unavailable mart is not a 500
            log.debug("kpi_distribution_failed", key=key, error=str(exc))
            out[key] = entry
            continue

        r = rows[0] if rows else None
        samples = int((r or {}).get("samples") or 0)
        if not r or samples == 0:
            out[key] = entry
            continue

        avg = float(r["daily_average"]) if r["daily_average"] is not None else None
        peak = float(r["peak_hour_mean"]) if r["peak_hour_mean"] is not None else None
        entry.update({
            "samples": samples,
            "daily_average": round(avg, 2) if avg is not None else None,
            "median": round(float(r["p50"]), 2) if r["p50"] is not None else None,
            "p90": round(float(r["p90"]), 2) if r["p90"] is not None else None,
            "peak_hour_mean": round(peak, 2) if peak is not None else None,
            "peak_hour_ratio": (round(peak / avg, 2) if avg and peak else None),
            "peak_hour_utc": r["peak_hour"].isoformat() if r["peak_hour"] else None,
            # Measured from events => live. No events => the baseline stands.
            "source": "live",
        })
        # A mean above the 90th percentile is arithmetically impossible for a
        # well-behaved population: it means a handful of extreme trips dominate
        # the average. Surfacing that is the honest move — silently winsorising
        # would make the headline look better and delete a real data-quality
        # finding (trip rows joined across simulator restarts pair an old
        # arrival with a new transaction, producing multi-hour durations).
        p90v, avgv = entry["p90"], entry["daily_average"]
        if p90v is not None and avgv is not None and avgv > p90v:
            entry["skew_warning"] = (
                f"Mean ({avgv}) exceeds P90 ({p90v}): a small number of extreme "
                "trips dominates the average. Median is the representative "
                "figure for this window; the outliers are a known data-quality "
                "artifact and are NOT removed."
            )
        out[key] = entry

    REQUESTS.labels("kpi", "ok").inc()
    return {
        "distribution": out,
        "window_hours": window_hours,
        "note": ("Every figure is computed from per-trip rows in the database. A KPI "
                 "with no trips in the window reports nulls with samples: 0 — never a "
                 "zero, which would read as a good measurement."),
    }


@router.get("/dual-tat")
async def dual_tat(state: GatewayState = Depends(get_state)) -> dict:
    """The TWO turnaround definitions, always returned together (UC3-035).

    UI-122: "neither can be displayed alone anywhere in the product". The two
    definitions measure different things and the GAP BETWEEN THEM is itself the
    reportable finding, so a single "TAT" number is not a simplification — it is
    the wrong answer, and which of the two it happens to be is invisible to the
    reader.

      terminal TAT — gate-in to gate-out. What the terminal controls.
      driver TAT   — plaza entry to highway exit. What the driver experiences,
                     including the plaza hold the terminal figure never sees.

    Both arms are returned in ONE payload so a client cannot render one without
    the other; the endpoint has no parameter to ask for a single definition.

    Ground-truth markers are the only REAL measured turnarounds in the corpus:
    two GTI visits by truck MH43BX1488, computed from the truck-in/truck-out
    times printed on the slips themselves — not modelled, not seeded.
    """
    from jnpa_shared import kpi as kpi_engine
    from jnpa_shared.db import fetch_all

    dsn = state.cfg.postgres_dsn
    markers: List[dict] = []
    try:
        rows = await fetch_all(
            """
            SELECT d.doc_variant, d.vehicle_no, d.container_no,
                   d.truck_in_ts, d.truck_out_ts,
                   EXTRACT(EPOCH FROM (d.truck_out_ts - d.truck_in_ts)) / 60.0 AS tat_min,
                   t.code AS terminal_code
              FROM core.gate_document d
              LEFT JOIN core.ref_terminal t ON t.terminal_id = d.terminal_id
             WHERE d.truck_in_ts IS NOT NULL AND d.truck_out_ts IS NOT NULL
               AND d.data_origin = 'REAL'
             ORDER BY d.truck_in_ts
            """,
            dsn=dsn,
        )
        markers = [{
            "source_document": r["doc_variant"],
            "vehicle_no": r["vehicle_no"],
            "container_no": r["container_no"],
            "terminal_code": r["terminal_code"],
            "tat_minutes": round(float(r["tat_min"]), 1),
            "definition": "terminal",
            "provenance": "REAL",
            "note": "Measured from the truck-in/truck-out times printed on the slip.",
        } for r in rows]
    except Exception as exc:  # noqa: BLE001 — markers are additive, never fatal
        log.debug("dual_tat_markers_failed", error=str(exc))

    target = kpi_engine.KPI_TARGETS.get("tat_inside_port")
    return {
        # The pair is the unit of this payload. Splitting it is the defect.
        "pair": {
            "terminal": {
                "key": "tat_terminal",
                "label": "Turn Around Time Inside Port (terminal)",
                "unit": "min",
                "definition": "gate-in to gate-out",
                "method": "mean(truck_out_ts - truck_in_ts) over gate documents",
                "target": target.target if target else None,
                "baseline": target.baseline if target else None,
                "baseline_source": ("PoC demonstration baseline — JNPA publishes no "
                                    "landside baseline; see docs/ASSUMPTIONS.md"),
            },
            "driver": {
                "key": "tat_driver",
                "label": "Turn Around Time Inside Port (driver)",
                "unit": "min",
                "definition": "plaza entry to highway exit",
                "method": ("terminal TAT + plaza hold + corridor egress; the plaza "
                           "legs have no corpus events (gaps G6/G9) and are simulated"),
                "target": target.target if target else None,
                "baseline": target.baseline if target else None,
                "baseline_source": ("PoC demonstration baseline — JNPA publishes no "
                                    "landside baseline; see docs/ASSUMPTIONS.md"),
            },
        },
        "render_rule": {
            "ref": "UI-122",
            "must_render_together": True,
            "note": ("Neither definition may be displayed alone anywhere in the "
                     "product. The gap between them is the reportable finding."),
        },
        "ground_truth_markers": markers,
        "ground_truth_note": (
            "The only REAL measured turnarounds in the corpus. Plotted as reference "
            "markers, never mixed into the aggregate." if markers else
            "No gate document carries both a truck-in and a truck-out time."),
    }


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
