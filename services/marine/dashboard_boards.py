"""UC-I dashboard board reads for the UC-1 PoC (M-01 / M-09 / UI-020 / UC1-011).

Adds the ``/api/marine/vessel-states``, ``/api/marine/berths``, ``/api/marine/kpis``
family the frontend ``Uc3Adapter`` expects. Every value is derived from the existing
Marine Projection + factual call aggregates — no new lifecycle rules here.

Read-only. Touches only ``core`` schema tables already used by the projection layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .berthing_plan import BerthingPlanService, resolve_window
from .projection import CallProjection, MarineProjection
from .service import VesselCallService
from .state_service import MarineStateService

log = get_logger("services.marine.dashboard_boards")

CORPUS_SOURCE = "JNPA marine corpus (PCS + berthing reports)"
_DATA_MODE = "CACHED"

# Calls whose traffic picture should appear at the anchor instant.
_CALLS_FOR_TRAFFIC = """
SELECT c.call_id, c.vcn, c.via_no, c.imo_no, c.vessel_name, c.voyage_no,
       c.status, c.eta, c.etb, c.etd, c.ata, c.atd,
       (SELECT t.code FROM core.ref_terminal t WHERE t.terminal_id = c.terminal_id)
         AS terminal_code,
       (SELECT b.code FROM core.ref_berth b WHERE b.berth_id = c.berth_id)
         AS berth_code
  FROM core.vessel_call c
 WHERE (
       (c.ata IS NOT NULL AND c.ata <= :at AND (c.atd IS NULL OR c.atd > :at))
       OR (c.atd IS NOT NULL AND c.atd <= :at
           AND c.atd > :at - interval '12 hours')
       OR (COALESCE(c.etb, c.eta) IS NOT NULL
           AND COALESCE(c.etb, c.eta) >= :lo
           AND COALESCE(c.etb, c.eta) <= :hi
           AND (c.atd IS NULL OR c.atd > :at))
       )
 ORDER BY c.call_id
"""

_BERTHS_REGISTER = """
SELECT b.berth_id, b.code, b.terminal_id,
       t.code AS terminal_code,
       COALESCE(t.name, t.code, '') AS terminal_name,
       COALESCE(t.operator, '') AS operator
  FROM core.ref_berth b
  LEFT JOIN core.ref_terminal t ON t.terminal_id = b.terminal_id
 ORDER BY b.code
"""

# Berthing report row occupying a berth at the anchor (best-effort match on berth code).
_BERTH_OCCUPANT = """
SELECT b.terminal, b.vessel_name, b.voyage_number AS voyage_no,
       b.imo_number AS imo_no, b.shipping_line, b.status AS record_status,
       b.berthing_time, b.cargo_operation_start, b.cargo_operation_end,
       b.departure_time
  FROM core.berthing_record b
 WHERE upper(replace(replace(trim(b.berth_number), ' ', ''), '-', ''))
       = upper(replace(replace(trim(:code), ' ', ''), '-', ''))
   AND COALESCE(b.berthing_time, b.ata, b.eta) <= :at
   AND (b.departure_time IS NULL OR b.departure_time > :at)
 ORDER BY COALESCE(b.berthing_time, b.ata, b.eta) DESC, b.id DESC
 LIMIT 1
"""


def _aware(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    return None


def _envelope(*, anchor: datetime, observed: Optional[datetime] = None) -> dict[str, Any]:
    obs = observed or anchor
    return {
        "data_mode": _DATA_MODE,
        "source": CORPUS_SOURCE,
        "observed_at": obs,
        "as_of": anchor,
    }


def traffic_state(p: CallProjection, at: datetime) -> str:
    """Map a projection to the UC1-011 traffic-map state enum."""
    atd = _aware(p.atd)
    if atd is not None and atd <= at and (at - atd) <= timedelta(hours=12):
        return "departed"
    if p.is_at_berth:
        return "alongside"
    if p.pilot_state in ("Active", "Onboard", "Assigned") and not p.is_at_berth:
        return "under_pilotage"
    if p.anchored_at is not None or (p.arrival_state or "").strip().lower() == "anchored":
        return "at_anchorage"
    if p.is_in_port and not p.is_at_berth:
        return "at_anchorage"
    eta = _aware(p.eta) or _aware(p.etb)
    if eta is not None and eta > at:
        return "inbound" if _aware(p.ata) else "expected"
    return "expected"


def _berth_ui_state(occ_state: str, record_status: str) -> str:
    """Map engine berth occupancy + report status → UI-022 sub-state."""
    if occ_state.lower() == "free":
        return "free"
    st = (record_status or "").strip().upper()
    if st in ("CARGO_OPERATION", "BERTHING_STARTED", "WORKING"):
        return "occupied-working"
    return "occupied-idle" if occ_state.lower() == "occupied" else "free"


def _kpi_card(
    *,
    key: str,
    name: str,
    value: Optional[float],
    unit: str,
    n: int,
    definition: str,
    basis: str,
    baseline_source: str = "no published baseline for this KPI",
    note: str = "",
    series: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "value": value,
        "median": None,
        "unit": unit,
        "n": n,
        "definition": definition,
        "basis": basis,
        "baseline_source": baseline_source,
        "baseline": None,
        "vs_baseline_pct": None,
        "note": note or "",
        "series": series or [],
    }


class DashboardBoardsService:
    """Orchestration for ``GET /api/marine/{vessel-states,berths,kpis}``."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._projection = MarineProjection(dsn)
        self._state = MarineStateService(dsn, projection=self._projection)
        self._calls = VesselCallService(dsn)
        self._plan = BerthingPlanService(dsn)

    async def _anchor(self, at: Optional[datetime]) -> datetime:
        latest = await self._plan.latest_actual()
        _, _, anchor = resolve_window(at=at, days=5, latest_actual=latest)
        return anchor

    async def vessel_states(self, *, at: Optional[datetime] = None) -> dict[str, Any]:
        anchor = await self._anchor(at)
        lo = anchor - timedelta(days=7)
        hi = anchor + timedelta(days=7)
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(
                text(_CALLS_FOR_TRAFFIC),
                {"at": anchor, "lo": lo, "hi": hi},
            )).mappings().all()
        ids = [int(r["call_id"]) for r in rows]
        states = await self._projection.by_call_ids(ids)

        items: list[dict[str, Any]] = []
        for r in rows:
            cid = int(r["call_id"])
            p = states.get(cid)
            if p is None:
                continue
            name = str(r.get("vessel_name") or p.vessel_name or "").strip()
            imo = str(r.get("imo_no") or p.imo_no or "").strip()
            if not name and not imo:
                continue
            items.append({
                "call_id": cid,
                "vcn": r.get("vcn") or p.vcn,
                "via_no": r.get("via_no") or p.via_no,
                "imo_no": imo,
                "vessel_name": name,
                "voyage_no": r.get("voyage_no") or p.voyage_no,
                "status": r.get("status") or p.status,
                "state": traffic_state(p, anchor),
                "berth_code": r.get("berth_code") or "",
                "terminal": str(r.get("terminal_code") or "").strip(),
                "eta": r.get("eta") or p.eta,
                "etb": r.get("etb") or p.etb,
                "etd": r.get("etd") or p.etd,
                "ata": r.get("ata") or p.ata,
                "atd": r.get("atd") or p.atd,
                "anchor_down_at": p.anchored_at,
                "pilot_boarded_at": p.pilot_boarded_at,
                "first_line_at": p.berthed_at,
                "movement_type": "INBOUND" if traffic_state(p, anchor) in (
                    "inbound", "expected") else "PORT",
            })
        return {**_envelope(anchor=anchor), "items": items}

    async def berths(self, *, at: Optional[datetime] = None) -> dict[str, Any]:
        anchor = await self._anchor(at)
        occ = await self._state.berth_occupancy()
        occ_by_code = {str(b.get("code") or ""): b for b in occ.get("berths", [])}

        async with get_engine(self._dsn).connect() as conn:
            reg = (await conn.execute(text(_BERTHS_REGISTER))).mappings().all()

        items: list[dict[str, Any]] = []
        occupied = 0
        for b in reg:
            code = str(b.get("code") or "").strip()
            ob = occ_by_code.get(code, {})
            eng_state = str(ob.get("state") or "Free")
            occ_by = ob.get("occupied_by") or {}
            report: Mapping[str, Any] = {}
            if code and eng_state.lower() != "free":
                async with get_engine(self._dsn).connect() as conn:
                    row = (await conn.execute(
                        text(_BERTH_OCCUPANT), {"code": code, "at": anchor},
                    )).mappings().first()
                if row:
                    report = dict(row)
            ui_state = _berth_ui_state(
                eng_state,
                str(report.get("record_status") or ""),
            )
            if ui_state != "free":
                occupied += 1
            items.append({
                "berth_id": int(b["berth_id"]),
                "code": code,
                "terminal": str(b.get("terminal_code") or "").strip(),
                "terminal_name": str(b.get("terminal_name") or "").strip(),
                "operator": str(b.get("operator") or "").strip(),
                "length_m": None,
                "design_depth_m": None,
                "dimensions_assumed": True,
                "state": ui_state,
                "vessel_name": str(
                    report.get("vessel_name") or occ_by.get("vessel_name") or "",
                ).strip(),
                "voyage_no": str(report.get("voyage_no") or "").strip(),
                "imo_no": str(report.get("imo_no") or "").strip(),
                "shipping_line": str(report.get("shipping_line") or "").strip(),
                "alongside_since": report.get("berthing_time"),
                "ops_start": report.get("cargo_operation_start"),
                "ops_end": report.get("cargo_operation_end"),
                "record_status": str(report.get("record_status") or "").strip(),
            })
        return {**_envelope(anchor=anchor), "items": items, "occupied": occupied}

    async def kpis(
        self, *, at: Optional[datetime] = None, window_days: int = 30,
    ) -> dict[str, Any]:
        anchor = await self._anchor(at)
        window_days = max(1, min(int(window_days), 90))
        lo = anchor - timedelta(days=window_days)
        hi = anchor + timedelta(days=1)

        stats = await self._calls.stats({
            "eta_from": lo, "eta_to": hi,
        })
        occ = await self._state.berth_occupancy()
        vs = await self.vessel_states(at=anchor)
        states = vs.get("items") or []

        total = int(stats.get("total") or 0)
        tat = stats.get("avg_turnaround_hours")
        pre_berth = stats.get("avg_pre_berth_delay_hours")
        occ_total = int(occ.get("total") or 0)
        occ_n = int(occ.get("occupied") or 0)
        occ_pct = round(100.0 * occ_n / occ_total, 1) if occ_total else None

        anchored = sum(1 for s in states if s.get("state") == "at_anchorage")
        approaching = sum(
            1 for s in states if s.get("state") in ("inbound", "expected"))

        kpis: list[dict[str, Any]] = [
            _kpi_card(
                key="PRE_BERTH_DELAY",
                name="Pre-Berthing Delay",
                value=pre_berth,
                unit="h",
                n=total,
                definition="Mean hours between declared ETA and actual arrival (ATA − ETA)",
                basis="core.vessel_call factual timestamps",
                note="" if pre_berth is not None else (
                    "not measurable — no calls with both ETA and ATA in window"),
            ),
            _kpi_card(
                key="PRE_SAIL_DELAY",
                name="Pre-Sailing Delay",
                value=None,
                unit="h",
                n=0,
                definition="Mean hours between planned and actual sailing",
                basis="requires ATC/ATD pairing — not in corpus at anchor",
                note="not measurable — ATC not populated for this corpus slice",
            ),
            _kpi_card(
                key="AVG_TAT",
                name="Average Vessel Turnaround",
                value=tat,
                unit="h",
                n=int(stats.get("departed") or 0),
                definition="Mean hours alongside (ATD − ATA) for completed calls",
                basis="core.vessel_call factual timestamps",
                note="" if tat is not None else (
                    "not measurable — no completed calls with both ATA and ATD"),
            ),
            _kpi_card(
                key="JIT_PCT",
                name="Just-In-Time Arrivals",
                value=None,
                unit="%",
                n=0,
                definition="Share of arrivals within ±60 min of the recommended slot",
                basis="requires berthing-plan slot comparison — not computed here",
                note="not measurable — JIT needs plan-slot linkage at anchor",
            ),
            _kpi_card(
                key="FORECAST_ACC",
                name="Forecast Accuracy",
                value=None,
                unit="%",
                n=0,
                definition="Share of arrivals within ±4 h of declared ETA",
                basis="berthing-report ETA vs ATA",
                note="see Prediction vs Actual panel for rolling MAE",
            ),
            _kpi_card(
                key="BERTH_OCC",
                name="Berth Occupancy",
                value=occ_pct,
                unit="%",
                n=occ_total,
                definition="Occupied share of container-terminal berths at the anchor instant",
                basis="Marine Projection berth occupancy",
                note="" if occ_pct is not None else "no berths in register",
            ),
        ]
        return {
            **_envelope(anchor=anchor),
            "window": {"days": window_days, "anchor": anchor},
            "kpis": kpis,
            "anchored_count": anchored,
            "approaching_count": approaching,
        }

    async def arrivals_departures(
        self,
        *,
        at: Optional[datetime] = None,
        hours: int = 48,
        bucket_hours: int = 4,
    ) -> dict[str, Any]:
        anchor = await self._anchor(at)
        hours = max(4, min(int(hours), 336))
        bucket_hours = max(1, min(int(bucket_hours), 24))
        start = anchor - timedelta(hours=hours // 2)
        end = anchor + timedelta(hours=hours // 2)

        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT ata, atd FROM core.vessel_call "
                "WHERE (ata IS NOT NULL AND ata >= :lo AND ata < :hi) "
                "   OR (atd IS NOT NULL AND atd >= :lo AND atd < :hi)"),
                {"lo": start, "hi": end},
            )).mappings().all()

        buckets: dict[datetime, dict[str, int]] = {}
        span = timedelta(hours=bucket_hours)
        t = start
        while t < end:
            buckets[t] = {"arrivals": 0, "departures": 0}
            t += span

        def _bucket(ts: datetime) -> Optional[datetime]:
            ts = _aware(ts)
            if ts is None:
                return None
            idx = int((ts - start).total_seconds() // span.total_seconds())
            key = start + idx * span
            return key if key in buckets else None

        for r in rows:
            b = _bucket(r.get("ata"))
            if b is not None:
                buckets[b]["arrivals"] += 1
            b = _bucket(r.get("atd"))
            if b is not None:
                buckets[b]["departures"] += 1

        blocks = [
            {"bucket_start": k, "arrivals": v["arrivals"], "departures": v["departures"]}
            for k, v in sorted(buckets.items())
        ]
        return {
            **_envelope(anchor=anchor),
            "bucket_hours": bucket_hours,
            "blocks": blocks,
        }
