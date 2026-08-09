"""5-day berthing plan (spec UI-028 / UC1-024) — confirmed vs indicative.

Confirmed entries come from ``core.berthing_record`` (JNPA terminal berthing
reports). Indicative entries are twin-generated from PCS ``core.vessel_call``
declarations that are not already backed by a terminal report, plus a short
post-occupancy projection on the same berth when the twin can see a next ETA.

Honesty rules:
  * Confirmed bars stay solid; indicative bars are hatched by the SPA.
  * Missing departure times are flagged ``end_estimated=True``.
  * Estimated stays are capped at 48 h so the Gantt stays readable and
    what-if drag works (bars longer than the 5-day horizon cannot move).
  * Vessels still marked occupying with no sail, whose capped bar would fall
    entirely before the pin, get a short pin-local estimated bar instead of a
    multi-day stretch from ATA through the pin.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.marine.berthing_plan")

CONFIRMED_SOURCE = "JNPA terminal berthing reports"
INDICATIVE_SOURCE = "twin-generated"

_DEFAULT_SPAN = timedelta(hours=24)
#: Max drawn length for estimated ends — keeps bars ≤ ~2 days so they fit inside
#: the 5-day horizon and remain draggable in what-if replan.
_MAX_ESTIMATED_SPAN = timedelta(hours=48)
_OCCUPYING_STATUSES = frozenset({
    "BERTH_ASSIGNED", "BERTHING_STARTED", "CARGO_OPERATION",
})
_LEFT_STATUSES = frozenset({"COMPLETED", "DEPARTED", "CANCELLED"})


def _aware(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    return None


def _norm_name(name: Any) -> str:
    return " ".join(str(name or "").upper().split())


def _cap_estimated(start: datetime, end: datetime) -> datetime:
    """Clamp an estimated end so the drawn bar never exceeds ``_MAX_ESTIMATED_SPAN``."""
    max_end = start + _MAX_ESTIMATED_SPAN
    return end if end <= max_end else max_end


def resolve_window(
    *,
    at: Optional[datetime],
    days: int,
    latest_actual: Optional[datetime],
) -> tuple[datetime, datetime, datetime]:
    """Return (window_start, window_end, anchor).

    When ``at`` is omitted, anchor on the latest berthing actual so a cold start
    still paints a populated 5-day board.
    """
    days = max(1, min(int(days), 14))
    if at is not None:
        anchor = _aware(at) or datetime.now(timezone.utc)
    elif latest_actual is not None:
        anchor = _aware(latest_actual) or datetime.now(timezone.utc)
    else:
        anchor = datetime.now(timezone.utc)
    start = anchor
    end = anchor + timedelta(days=days)
    return start, end, anchor


def confirmed_span(
    row: Mapping[str, Any],
    *,
    anchor: datetime,
) -> Optional[tuple[datetime, datetime, bool]]:
    """(start, end, end_estimated) for one berthing_record, or None if undated.

    Estimated ends are capped at 48 h from start. Pin-stretch (ATA → pin+24 h) is
    deliberately NOT applied here — see ``assemble_berthing_plan`` for the short
    pin-local rescue bar when a vessel is still occupying.
    """
    start = _aware(row.get("berthing_time")) or _aware(row.get("ata")) or _aware(row.get("eta"))
    if start is None:
        return None
    departure = _aware(row.get("departure_time"))
    cargo_end = _aware(row.get("cargo_operation_end"))
    status = str(row.get("status") or "").strip().upper()

    if departure is not None:
        return start, departure, False

    if cargo_end is not None:
        estimated = status in _OCCUPYING_STATUSES
        end = _cap_estimated(start, cargo_end) if estimated else cargo_end
        return start, end, estimated

    # No end carried by the source — default 24 h stay, always an estimate.
    return start, start + _DEFAULT_SPAN, True


def entry_overlaps(start: datetime, end: datetime, win_start: datetime, win_end: datetime) -> bool:
    return start < win_end and end > win_start


def build_confirmed_entry(
    row: Mapping[str, Any],
    *,
    anchor: datetime,
    win_start: Optional[datetime] = None,
    win_end: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    span = confirmed_span(row, anchor=anchor)
    if span is None:
        return None
    start, end, end_estimated = span
    status = str(row.get("status") or "").strip().upper()
    clipped = False
    alongside_since: Optional[datetime] = None

    if win_start is not None and win_end is not None:
        if not entry_overlaps(start, end, win_start, win_end):
            # Still occupying, no sail, cargo ops not finished before the window:
            # show a short pin-local estimated bar instead of stretching ATA→pin.
            cargo_end = _aware(row.get("cargo_operation_end"))
            departure = _aware(row.get("departure_time"))
            if (
                departure is None
                and status in _OCCUPYING_STATUSES
                and start < anchor
                and start < win_end
                and (cargo_end is None or cargo_end >= win_start)
            ):
                alongside_since = start
                start = win_start
                end = win_start + _DEFAULT_SPAN
                end_estimated = True
                clipped = True
            else:
                return None

    berth_raw = str(row.get("berth_number") or "").strip()
    source_file = str(row.get("source_file") or "").strip()
    source = CONFIRMED_SOURCE
    if source_file:
        source = f"{CONFIRMED_SOURCE} ({source_file})"
    if alongside_since is not None:
        source = f"{source}; alongside since {alongside_since.isoformat()}"
    return {
        "kind": "confirmed",
        "source": source,
        "berth_code": berth_raw,
        "berth_raw": berth_raw,
        "terminal": str(row.get("terminal") or "").strip(),
        "vessel_name": str(row.get("vessel_name") or "").strip(),
        "voyage_no": str(row.get("voyage_number") or "").strip(),
        "imo_no": str(row.get("imo_number") or "").strip(),
        "shipping_line": str(row.get("shipping_line") or "").strip(),
        "status": status,
        "start_ts": start,
        "end_ts": end,
        "end_estimated": end_estimated,
        "ref": f"berthing_record:{row.get('id')}",
        "vcn": "",
        "via_no": "",
        "_clipped": clipped,
    }


def build_indicative_entry(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Twin-generated bar from a PCS vessel_call (ETA/ETB → estimated ETD)."""
    start = _aware(row.get("etb")) or _aware(row.get("eta"))
    if start is None:
        return None
    end = _aware(row.get("etd"))
    end_estimated = end is None
    if end is None:
        end = start + _DEFAULT_SPAN
    if end_estimated:
        end = _cap_estimated(start, end)
    berth_code = str(row.get("berth_code") or "").strip()
    berth_raw = berth_code or str(row.get("berth_raw") or "").strip()
    via = str(row.get("via_no") or "").strip()
    vcn = str(row.get("vcn") or "").strip()
    name = str(row.get("vessel_name") or "").strip()
    if not name:
        name = f"VIA {via}" if via else (f"VCN {vcn}" if vcn else "(unnamed)")
    return {
        "kind": "indicative",
        "source": INDICATIVE_SOURCE,
        "berth_code": berth_code,
        "berth_raw": berth_raw,
        "terminal": str(row.get("terminal_code") or "").strip(),
        "vessel_name": name,
        "voyage_no": str(row.get("voyage_no") or "").strip(),
        "imo_no": str(row.get("imo_no") or "").strip(),
        "shipping_line": "",
        "status": str(row.get("status") or "").strip() or "INDICATIVE",
        "start_ts": start,
        "end_ts": end,
        "end_estimated": end_estimated,
        "ref": f"vessel_call:{row.get('call_id')}",
        "vcn": vcn,
        "via_no": via,
    }


def project_next_on_berth(
    confirmed: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    win_start: datetime,
    win_end: datetime,
) -> Optional[dict[str, Any]]:
    """Place a twin next-call on the same berth after a confirmed occupancy ends."""
    berth = str(confirmed.get("berth_code") or "").strip()
    if not berth:
        return None
    conf_end = _aware(confirmed.get("end_ts"))
    if conf_end is None:
        return None
    start = max(conf_end, win_start)
    if start >= win_end:
        return None
    entry = build_indicative_entry(candidate)
    if entry is None:
        return None
    # Re-anchor onto the vacated berth. The twin chose the slot — end is always
    # an estimate relative to that projected start (UI-028 honesty).
    end = start + _DEFAULT_SPAN
    entry["berth_code"] = berth
    entry["berth_raw"] = berth
    entry["terminal"] = entry["terminal"] or str(confirmed.get("terminal") or "")
    entry["start_ts"] = start
    entry["end_ts"] = end
    entry["end_estimated"] = True
    entry["ref"] = f"vessel_call:{candidate.get('call_id')}:next@{berth}"
    entry["status"] = "INDICATIVE"
    if not entry_overlaps(start, end, win_start, win_end):
        return None
    return entry


def assemble_berthing_plan(
    *,
    confirmed_rows: Sequence[Mapping[str, Any]],
    call_rows: Sequence[Mapping[str, Any]],
    win_start: datetime,
    win_end: datetime,
    anchor: datetime,
) -> list[dict[str, Any]]:
    """Build the honesty-labelled plan entries for one window."""
    confirmed: list[dict[str, Any]] = []
    confirmed_names: set[str] = set()
    for row in confirmed_rows:
        entry = build_confirmed_entry(
            row, anchor=anchor, win_start=win_start, win_end=win_end,
        )
        if entry is None:
            continue
        # Berth Gantt is berth-axis: unassigned EXPECTED rows stay off the board
        # (they remain in core.berthing_record for list UIs).
        if not str(entry.get("berth_code") or "").strip():
            continue
        entry.pop("_clipped", None)
        confirmed.append(entry)
        name = _norm_name(entry["vessel_name"])
        if name:
            confirmed_names.add(name)

    indicative: list[dict[str, Any]] = []
    used_calls: set[int] = set()

    # 1) PCS declarations that already carry a berth allotment and are not on a
    #    terminal report — render as indicative on that berth.
    unberthed: list[Mapping[str, Any]] = []
    for row in call_rows:
        name = _norm_name(row.get("vessel_name"))
        if name and name in confirmed_names:
            continue
        if not str(row.get("berth_code") or "").strip():
            unberthed.append(row)
            continue
        entry = build_indicative_entry(row)
        if entry is None:
            continue
        if not entry_overlaps(entry["start_ts"], entry["end_ts"], win_start, win_end):
            continue
        indicative.append(entry)
        if row.get("call_id") is not None:
            used_calls.add(int(row["call_id"]))

    # 2) Twin next-call on berths whose confirmed bar ends inside the window —
    #    place leftover unberthed PCS ETAs as hatched follow-ons (UI-028 demo).
    open_calls = [
        r for r in unberthed
        if r.get("call_id") is not None and int(r["call_id"]) not in used_calls
    ]
    open_calls.sort(key=lambda r: (_aware(r.get("eta")) or win_end, int(r.get("call_id") or 0)))

    berths_with_followon: set[str] = set()
    for conf in sorted(confirmed, key=lambda e: e["end_ts"]):
        berth = str(conf.get("berth_code") or "").strip()
        if not berth or berth in berths_with_followon:
            continue
        if not open_calls:
            break
        if conf["end_ts"] >= win_end:
            continue
        cand = open_calls.pop(0)
        projected = project_next_on_berth(
            conf, cand, win_start=win_start, win_end=win_end,
        )
        if projected is None:
            open_calls.insert(0, cand)
            continue
        indicative.append(projected)
        berths_with_followon.add(berth)
        used_calls.add(int(cand["call_id"]))

    entries = confirmed + indicative
    entries.sort(key=lambda e: (e["start_ts"], e["berth_code"], e["vessel_name"]))
    return entries


class BerthingPlanService:
    """Read orchestration for ``GET /api/marine/berthing-plan``."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def latest_actual(self) -> Optional[datetime]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT max(COALESCE(berthing_time, ata, eta)) AS ts "
                "FROM core.berthing_record"
            ))).mappings().first()
        return _aware(row["ts"]) if row else None

    async def load_confirmed(self) -> list[dict[str, Any]]:
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT id, terminal, vessel_name, imo_number, voyage_number, "
                "       shipping_line, berth_number, eta, ata, berthing_time, "
                "       departure_time, cargo_operation_start, cargo_operation_end, "
                "       status, source_file "
                "FROM core.berthing_record "
                "WHERE COALESCE(berthing_time, ata, eta) IS NOT NULL "
                "ORDER BY COALESCE(berthing_time, ata, eta), id"
            ))).mappings().all()
        return [dict(r) for r in rows]

    async def load_calls(self, win_start: datetime, win_end: datetime) -> list[dict[str, Any]]:
        """PCS calls whose ETA/ETB falls in an expanded window (for twin follow-ons)."""
        # Look slightly before the pin so a call already declared can still project.
        lo = win_start - timedelta(days=2)
        hi = win_end + timedelta(days=1)
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT c.call_id, c.vcn, c.via_no, c.imo_no, "
                "       COALESCE(NULLIF(trim(c.vessel_name), ''), "
                "                (SELECT v.vessel_name FROM core.vessel v "
                "                  WHERE v.imo_no = c.imo_no LIMIT 1), '') AS vessel_name, "
                "       c.voyage_no, "
                "       c.eta, c.etd, c.etb, c.status, "
                "       (SELECT t.code FROM core.ref_terminal t "
                "         WHERE t.terminal_id = c.terminal_id) AS terminal_code, "
                "       (SELECT b.code FROM core.ref_berth b "
                "         WHERE b.berth_id = c.berth_id) AS berth_code "
                "FROM core.vessel_call c "
                "WHERE COALESCE(c.etb, c.eta) IS NOT NULL "
                "  AND COALESCE(c.etb, c.eta) >= :lo "
                "  AND COALESCE(c.etb, c.eta) < :hi "
                "ORDER BY COALESCE(c.etb, c.eta), c.call_id"
            ), {"lo": lo, "hi": hi})).mappings().all()
        return [dict(r) for r in rows]

    async def plan(self, *, at: Optional[datetime] = None, days: int = 5) -> dict[str, Any]:
        latest = await self.latest_actual()
        win_start, win_end, anchor = resolve_window(at=at, days=days, latest_actual=latest)
        confirmed_rows = await self.load_confirmed()
        call_rows = await self.load_calls(win_start, win_end)
        entries = assemble_berthing_plan(
            confirmed_rows=confirmed_rows,
            call_rows=call_rows,
            win_start=win_start,
            win_end=win_end,
            anchor=anchor,
        )
        observed = latest or anchor
        data_mode = "CACHED" if entries else "NO_DATA"
        return {
            "data_mode": data_mode,
            "source": "core.berthing_record + twin vessel_call projections",
            "observed_at": observed,
            "as_of": anchor,
            "window": {
                "start": win_start,
                "end": win_end,
                "anchor": anchor,
            },
            "entries": entries,
        }
