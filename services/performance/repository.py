"""Performance & Daily Reports persistence — raw-SQL repository (Module 12).

The ONLY layer that speaks SQL to the jnpa.perf_* tables (populated from the
official JNPA Daily Status Report, monthly JN Port TEUs, and NLDS/LDB Analytics
PDFs by scripts/import_performance_reports.py). No ORM; parameterised reads on a
plain ``connect()`` off the shared cached async engine, exactly like
:mod:`services.cfs_ecy.repository`.

Read-only + additive: it reads only the perf_* tables it owns and writes nothing.
Injection-safe by construction — filter/sort COLUMN names are fixed identifiers
from whitelists; every VALUE is a bound parameter.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.performance.repository")

# Whitelisted trend metrics -> (sql expression, source query builder key).
# Daily metrics read one figure per report_date; monthly reads perf_monthly_teu.
_DAILY_TREND = {
    # metric            -> (table alias source, column)
    "total_teus":        ("traffic_jnport", "total_teus"),
    "yard_occupancy_pct":("status_total", "yard_occupancy_pct"),
    "gate_total_teus":   ("status_total", "gate_total_teus"),
    "gate_in_teus":      ("status_total", "gate_in_teus"),
    "gate_out_teus":     ("status_total", "gate_out_teus"),
    "icd_pendency_teus": ("status_total", "icd_pendency_teus"),
    "cfs_pendency_teus": ("status_total", "cfs_pendency_teus"),
    "tonnage":           ("tonnage_jnpa", "total_tonnes"),
}
_TRAFFIC_SORTS = {"terminal_code": "terminal_code", "period": "period",
                  "total_teus": "total_teus", "report_date": "report_date"}
_MONTHLY_SORTS = {"month_date": "month_date", "terminal_code": "terminal_code",
                  "total_teus": "total_teus"}


def _f(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


class PerformanceRepository:
    """Raw-SQL reads for the Performance & Daily Reports (perf_*) tables."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ---------------------------------------------------------------- dimension
    async def terminals(self) -> list[dict]:
        sql = ("SELECT code, full_name, operator, terminal_type, is_container, "
               "aliases, sort_order FROM jnpa.perf_terminals ORDER BY sort_order, code")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql))).mappings().all()
        return [dict(r) for r in rows]

    async def latest_report_date(self) -> Optional[date]:
        async with get_engine(self._dsn).connect() as conn:
            r = (await conn.execute(text(
                "SELECT max(report_date) AS d FROM jnpa.perf_daily_snapshot"))).mappings().first()
        return r["d"] if r and r["d"] else None

    async def prev_report_date(self, d: date) -> Optional[date]:
        async with get_engine(self._dsn).connect() as conn:
            r = (await conn.execute(text(
                "SELECT max(report_date) AS d FROM jnpa.perf_daily_snapshot "
                "WHERE report_date < :d"), {"d": d})).mappings().first()
        return r["d"] if r and r["d"] else None

    async def report_dates(self, limit: int = 60) -> list[str]:
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT report_date FROM jnpa.perf_daily_snapshot "
                "ORDER BY report_date DESC LIMIT :limit"), {"limit": limit})).mappings().all()
        return [str(r["report_date"]) for r in rows]

    # ---------------------------------------------------------------- KPI headline
    async def _day_headline(self, conn, d: date) -> dict:
        teus = (await conn.execute(text(
            "SELECT total_teus FROM jnpa.perf_daily_traffic "
            "WHERE report_date=:d AND terminal_code='JN_PORT' AND period='DAY'"),
            {"d": d})).scalar()
        tonnage = (await conn.execute(text(
            "SELECT total_tonnes, vessels FROM jnpa.perf_daily_tonnage "
            "WHERE report_date=:d AND category='JNPA_TOTAL' AND period='DAY'"),
            {"d": d})).mappings().first()
        st = (await conn.execute(text(
            "SELECT yard_occupancy_pct, gate_in_teus, gate_out_teus, gate_total_teus, "
            "  icd_pendency_teus, cfs_pendency_teus, reefer_total_slots, "
            "  reefer_occupied_slots, reefer_available_slots "
            "FROM jnpa.perf_daily_terminal_status "
            "WHERE report_date=:d AND terminal_code='TOTAL'"), {"d": d})).mappings().first()
        st = dict(st) if st else {}
        pend = None
        if st.get("icd_pendency_teus") is not None or st.get("cfs_pendency_teus") is not None:
            pend = (st.get("icd_pendency_teus") or 0) + (st.get("cfs_pendency_teus") or 0)
        return {
            "total_teus": _f(teus),
            "total_tonnes": _f(tonnage["total_tonnes"]) if tonnage else None,
            "vessel_calls": int(tonnage["vessels"]) if tonnage and tonnage["vessels"] is not None else None,
            "yard_occupancy_pct": _f(st.get("yard_occupancy_pct")),
            "gate_total_teus": _f(st.get("gate_total_teus")),
            "gate_in_teus": _f(st.get("gate_in_teus")),
            "gate_out_teus": _f(st.get("gate_out_teus")),
            "total_pendency_teus": _f(pend),
            "reefer_available_slots": st.get("reefer_available_slots"),
            "reefer_total_slots": st.get("reefer_total_slots"),
        }

    async def kpi(self, report_date: Optional[date]) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            d = report_date
            if d is None:
                d = (await conn.execute(text(
                    "SELECT max(report_date) AS d FROM jnpa.perf_daily_snapshot"))).scalar()
            if d is None:
                return None
            cur = await self._day_headline(conn, d)
            pd = (await conn.execute(text(
                "SELECT max(report_date) AS d FROM jnpa.perf_daily_snapshot "
                "WHERE report_date < :d"), {"d": d})).scalar()
            prev = await self._day_headline(conn, pd) if pd else {}
        deltas = {}
        for k, v in cur.items():
            pv = prev.get(k)
            if isinstance(v, (int, float)) and isinstance(pv, (int, float)):
                deltas[k] = round(v - pv, 2)
        return {"report_date": str(d), "prev_report_date": str(pd) if pd else None,
                "metrics": cur, "deltas": deltas}

    # ---------------------------------------------------------------- daily bundle
    async def daily_bundle(self, d: date) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            snap = (await conn.execute(text(
                "SELECT report_date, as_of_ts, source_file FROM jnpa.perf_daily_snapshot "
                "WHERE report_date=:d"), {"d": d})).mappings().first()
            if not snap:
                return None
            traffic = (await conn.execute(text(
                "SELECT terminal_code, period, vessels, imp_teus, exp_teus, total_teus, "
                "  rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus "
                "FROM jnpa.perf_daily_traffic WHERE report_date=:d "
                "ORDER BY period, terminal_code"), {"d": d})).mappings().all()
            tonnage = (await conn.execute(text(
                "SELECT category, period, vessels, liquid_tonnes, dry_bulk_tonnes, "
                "  break_bulk_tonnes, total_tonnes FROM jnpa.perf_daily_tonnage "
                "WHERE report_date=:d ORDER BY period, category"), {"d": d})).mappings().all()
            status = (await conn.execute(text(
                "SELECT * FROM jnpa.perf_daily_terminal_status WHERE report_date=:d "
                "ORDER BY terminal_code"), {"d": d})).mappings().all()
            vessels = (await conn.execute(text(
                "SELECT terminal_code, berth_no, via_no, vessel_name, cargo_commodity, "
                "  berthed_on, expected_completion FROM jnpa.perf_daily_vessels "
                "WHERE report_date=:d ORDER BY terminal_code, berth_no"), {"d": d})).mappings().all()
        return {
            "snapshot": dict(snap),
            "traffic": [dict(r) for r in traffic],
            "tonnage": [dict(r) for r in tonnage],
            "status": [dict(r) for r in status],
            "vessels": [dict(r) for r in vessels],
        }

    # ---------------------------------------------------------------- lists
    async def list_traffic(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                           limit: int, offset: int) -> tuple[list[dict], int]:
        conds, params = [], {}
        if filters.get("date_from"):
            conds.append("report_date >= :date_from"); params["date_from"] = filters["date_from"]
        if filters.get("date_to"):
            conds.append("report_date <= :date_to"); params["date_to"] = filters["date_to"]
        if filters.get("terminal"):
            conds.append("terminal_code = :terminal"); params["terminal"] = filters["terminal"]
        if filters.get("period"):
            conds.append("period = :period"); params["period"] = filters["period"]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        order_col = _TRAFFIC_SORTS.get(sort, "report_date")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(
                f"SELECT count(*) FROM jnpa.perf_daily_traffic {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT report_date, terminal_code, period, vessels, imp_teus, exp_teus, "
                "  total_teus, rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus "
                f"FROM jnpa.perf_daily_traffic {where} "
                f"ORDER BY {order_col} {order_dir}, terminal_code ASC "
                "LIMIT :limit OFFSET :offset"), params)).mappings().all()
        return [dict(r) for r in rows], int(total or 0)

    async def list_status(self, filters: Mapping[str, Any], *, limit: int,
                          offset: int) -> tuple[list[dict], int]:
        conds, params = [], {}
        if filters.get("date"):
            conds.append("report_date = :d"); params["d"] = filters["date"]
        if filters.get("terminal"):
            conds.append("terminal_code = :terminal"); params["terminal"] = filters["terminal"]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(
                f"SELECT count(*) FROM jnpa.perf_daily_terminal_status {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                f"SELECT * FROM jnpa.perf_daily_terminal_status {where} "
                "ORDER BY report_date DESC, terminal_code ASC LIMIT :limit OFFSET :offset"),
                params)).mappings().all()
        return [dict(r) for r in rows], int(total or 0)

    async def list_vessels(self, filters: Mapping[str, Any], *, limit: int,
                           offset: int) -> tuple[list[dict], int]:
        conds, params = [], {}
        if filters.get("date"):
            conds.append("report_date = :d"); params["d"] = filters["date"]
        if filters.get("terminal"):
            conds.append("terminal_code = :terminal"); params["terminal"] = filters["terminal"]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(
                f"SELECT count(*) FROM jnpa.perf_daily_vessels {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT report_date, terminal_code, berth_no, via_no, vessel_name, "
                "  cargo_commodity, berthed_on, expected_completion "
                f"FROM jnpa.perf_daily_vessels {where} "
                "ORDER BY report_date DESC, terminal_code, berth_no LIMIT :limit OFFSET :offset"),
                params)).mappings().all()
        return [dict(r) for r in rows], int(total or 0)

    async def list_monthly(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                           limit: int, offset: int) -> tuple[list[dict], int]:
        conds, params = [], {}
        if filters.get("fiscal_year"):
            conds.append("fiscal_year = :fy"); params["fy"] = filters["fiscal_year"]
        if filters.get("terminal"):
            conds.append("terminal_code = :terminal"); params["terminal"] = filters["terminal"]
        if filters.get("date_from"):
            conds.append("month_date >= :date_from"); params["date_from"] = filters["date_from"]
        if filters.get("date_to"):
            conds.append("month_date <= :date_to"); params["date_to"] = filters["date_to"]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        order_col = _MONTHLY_SORTS.get(sort, "month_date")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(
                f"SELECT count(*) FROM jnpa.perf_monthly_teu {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT fiscal_year, month_date, year_label, month_label, terminal_code, "
                "  vessel_calls, discharge_teus, load_teus, total_teus "
                f"FROM jnpa.perf_monthly_teu {where} "
                f"ORDER BY {order_col} {order_dir}, terminal_code ASC LIMIT :limit OFFSET :offset"),
                params)).mappings().all()
        return [dict(r) for r in rows], int(total or 0)

    # ---------------------------------------------------------------- trends
    async def trends(self, metric: str, *, grain: str, terminal: Optional[str],
                     date_from, date_to) -> list[dict]:
        if grain == "monthly":
            conds = []
            params: dict[str, Any] = {}
            if terminal:
                conds.append("terminal_code = :terminal"); params["terminal"] = terminal
            else:
                conds.append("terminal_code = 'JN_PORT'")
            where = "WHERE " + " AND ".join(conds)
            sql = ("SELECT month_date::text AS t, terminal_code, total_teus AS value "
                   f"FROM jnpa.perf_monthly_teu {where} ORDER BY month_date ASC")
            async with get_engine(self._dsn).connect() as conn:
                rows = (await conn.execute(text(sql), params)).mappings().all()
            return [{"t": r["t"], "terminal_code": r["terminal_code"], "value": _f(r["value"])}
                    for r in rows]
        # daily grain
        src, col = _DAILY_TREND.get(metric, (None, None))
        if src is None:
            return []
        params = {}
        drange = ""
        if date_from:
            drange += " AND report_date >= :date_from"; params["date_from"] = date_from
        if date_to:
            drange += " AND report_date <= :date_to"; params["date_to"] = date_to
        if src == "traffic_jnport":
            sql = (f"SELECT report_date::text AS t, {col} AS value FROM jnpa.perf_daily_traffic "
                   f"WHERE terminal_code='JN_PORT' AND period='DAY'{drange} ORDER BY report_date ASC")
        elif src == "tonnage_jnpa":
            sql = (f"SELECT report_date::text AS t, {col} AS value FROM jnpa.perf_daily_tonnage "
                   f"WHERE category='JNPA_TOTAL' AND period='DAY'{drange} ORDER BY report_date ASC")
        else:  # status_total
            sql = (f"SELECT report_date::text AS t, {col} AS value "
                   "FROM jnpa.perf_daily_terminal_status "
                   f"WHERE terminal_code='TOTAL'{drange} ORDER BY report_date ASC")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [{"t": r["t"], "terminal_code": "JN_PORT", "value": _f(r["value"])} for r in rows]

    async def daily_series(self, date_from, date_to) -> list[dict]:
        """Per-day headline series for the overview chart (JN Port TEUs + gate)."""
        params: dict[str, Any] = {}
        drange = ""
        if date_from:
            drange += " AND s.report_date >= :date_from"; params["date_from"] = date_from
        if date_to:
            drange += " AND s.report_date <= :date_to"; params["date_to"] = date_to
        sql = (
            "SELECT s.report_date::text AS day, "
            "  tr.total_teus AS total_teus, st.gate_in_teus, st.gate_out_teus, "
            "  st.yard_occupancy_pct "
            "FROM jnpa.perf_daily_snapshot s "
            "LEFT JOIN jnpa.perf_daily_traffic tr ON tr.report_date=s.report_date "
            "  AND tr.terminal_code='JN_PORT' AND tr.period='DAY' "
            "LEFT JOIN jnpa.perf_daily_terminal_status st ON st.report_date=s.report_date "
            "  AND st.terminal_code='TOTAL' "
            f"WHERE 1=1{drange} ORDER BY s.report_date ASC")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [{"day": r["day"], "total_teus": _f(r["total_teus"]),
                 "gate_in_teus": _f(r["gate_in_teus"]), "gate_out_teus": _f(r["gate_out_teus"]),
                 "yard_occupancy_pct": _f(r["yard_occupancy_pct"])} for r in rows]

    # ---------------------------------------------------------------- LDB reads
    async def ldb_port_dwell(self, filters: Mapping[str, Any]) -> list[dict]:
        conds, params = [], {}
        for col in ("report_month", "terminal_code", "cycle", "segment"):
            if filters.get(col):
                conds.append(f"{col} = :{col}"); params[col] = filters[col]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        sql = ("SELECT report_month::text AS report_month, terminal_code, cycle, segment, "
               "  dwell_hours, dwell_hours_prev FROM jnpa.perf_ldb_port_dwell "
               f"{where} ORDER BY cycle, segment, terminal_code")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def ldb_facility_dwell(self, filters: Mapping[str, Any], *, limit: int,
                                 offset: int) -> tuple[list[dict], int]:
        conds, params = [], {}
        for col in ("report_month", "facility_type"):
            if filters.get(col):
                conds.append(f"{col} = :{col}"); params[col] = filters[col]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(
                f"SELECT count(*) FROM jnpa.perf_ldb_facility_dwell {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT report_month::text AS report_month, facility_type, facility_name, "
                "  dwell_hours, dwell_hours_prev FROM jnpa.perf_ldb_facility_dwell "
                f"{where} ORDER BY dwell_hours DESC NULLS LAST, facility_name "
                "LIMIT :limit OFFSET :offset"), params)).mappings().all()
        return [dict(r) for r in rows], int(total or 0)

    async def ldb_congestion(self, filters: Mapping[str, Any]) -> list[dict]:
        conds, params = [], {}
        for col in ("report_month", "cycle"):
            if filters.get(col):
                conds.append(f"{col} = :{col}"); params[col] = filters[col]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        sql = ("SELECT report_month::text AS report_month, cycle, cluster_no, cluster_name, "
               "  cfs_count, pct_containers, congestion_level FROM jnpa.perf_ldb_congestion "
               f"{where} ORDER BY cycle, cluster_no")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def ldb_routes(self, filters: Mapping[str, Any]) -> list[dict]:
        conds, params = [], {}
        for col in ("report_month", "cycle", "transport_mode"):
            if filters.get(col):
                conds.append(f"{col} = :{col}"); params[col] = filters[col]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        sql = ("SELECT report_month::text AS report_month, cycle, transport_mode, route_name, "
               f"pct_share FROM jnpa.perf_ldb_route_movement {where} "
               "ORDER BY cycle, pct_share DESC")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def ldb_weather(self, filters: Mapping[str, Any]) -> list[dict]:
        conds, params = [], {}
        for col in ("report_month", "terminal_code", "cycle"):
            if filters.get(col):
                conds.append(f"{col} = :{col}"); params[col] = filters[col]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        sql = ("SELECT report_month::text AS report_month, terminal_code, cycle, weather, "
               f"dwell_hours FROM jnpa.perf_ldb_weather {where} "
               "ORDER BY cycle, terminal_code, weather")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def ldb_months(self) -> list[str]:
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT DISTINCT report_month::text AS m FROM jnpa.perf_ldb_port_dwell "
                "ORDER BY m DESC"))).mappings().all()
        return [r["m"] for r in rows]
