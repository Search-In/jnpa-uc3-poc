"""Daily-report item mapper + CSV renderer.

Maps the ``daily-reports`` JSON items onto the ``daily_status`` performance
report row and renders them to the exact CSV template
:func:`services.performance.upload_parsers.template_csv` produces — so mapped
rows land through ``UploadService.import_file("daily_status", ...)`` (the same
validated ``jnpa.perf_*`` pipeline the manual dump upload uses), NO new SQL.

Real API shape (captured Aug-2026). Each item is one **per-date** port status
report whose terminal rows are NESTED in ``byTerminal``; ``portTotals`` is the
port-level aggregate (kept in the raw snapshot, not rendered as a terminal row):

    {
      "reportType": "DAILY_STATUS_REPORT",
      "reportDate": "2026-08-05",
      "portTotals": {...},
      "byTerminal": [
        {"terminal","vesselsOnBerth","arrivals","sailings","dischargeMoves",
         "loadMoves","yardImportTeu","yardExportTeu"}, ...
      ]
    }

The mapper iterates ``item.byTerminal`` for the rows and takes the date from the
item. A flat item (no ``byTerminal`` array — the simulator's synthetic keys or a
future shape) is treated as a single terminal row via the same alias table. Only
the fields the ``daily_status`` template can hold are rendered; moves and other
extras are recognised (so not flagged as drift) but not rendered.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional

from . import MapOutcome

# canonical field -> accepted NORMALISED keys.
_ALIASES: Dict[str, tuple] = {
    "terminal": ("terminal", "terminalcode", "terminalname", "term", "facility"),
    "vessels": ("vesselsonberth", "vesselsatberth", "vessels", "vesselcount",
                "vesselcalls", "berthedvessels", "vesselsberthed"),
    "yard_import_teus": ("yardimportteu", "yardimportteus", "yardimport",
                         "importyardteu"),
    "yard_export_teus": ("yardexportteu", "yardexportteus", "yardexport",
                         "exportyardteu"),
    "yard_transhipment_teus": ("yardtranshipmentteu", "yardtranshipmentteus",
                               "yardtranshipment", "transhipmentteu",
                               "transshipmentteu"),
    "imp_teus": ("impteus", "importteus", "imports", "importteu",
                 "importthroughput"),
    "exp_teus": ("expteus", "exportteus", "exports", "exportteu",
                 "exportthroughput"),
    "total_teus": ("teuhandled", "totalteus", "totalteu", "teus", "teu",
                   "throughput", "totalthroughput"),
    "yard_occupancy_pct": ("yardoccupancypct", "yardoccupancy", "occupancy",
                           "occupancypct", "yardutilisation", "yardutilization"),
    # recognised (so not flagged as drift) but not rendered — no template column:
    "discharge_moves": ("dischargemoves", "dischmoves", "importmoves"),
    "load_moves": ("loadmoves", "exportmoves"),
    "arrivals": ("arrivals", "arrival"),
    "sailings": ("sailings", "sailing", "departures"),
}

# keys carried on the report ITEM (not the terminal row).
_ITEM_KEYS = ("reportdate", "date", "day", "reporttype", "reportgeneratedat",
              "modifieddate", "porttotals", "byterminal", "sourceformat")


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).strip().lower())


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _pick(norm_row: Dict[str, Any], field: str):
    for alias in _ALIASES[field]:
        if alias in norm_row:
            value = _clean(norm_row[alias])
            if value is not None:
                return value, alias
    return None, None


def _recognised(norm_row: Dict[str, Any]) -> int:
    return sum(1 for f in _ALIASES if _pick(norm_row, f)[0] is not None)


def _map_terminal(raw: Dict[str, Any], *, report_date: str,
                  unmapped: set) -> Optional[Dict[str, Any]]:
    norm = {_norm(k): v for k, v in raw.items() if _norm(k)}
    picked: Dict[str, str] = {}
    consumed: set = set()
    for field in _ALIASES:
        value, alias = _pick(norm, field)
        if value is not None:
            picked[field] = value
            consumed.add(alias)
    unmapped.update(k for k in norm if k not in consumed and k not in _ITEM_KEYS)
    terminal = picked.get("terminal")
    if not terminal:
        return None
    yi = _num(picked.get("yard_import_teus"))
    ye = _num(picked.get("yard_export_teus"))
    ytr = _num(picked.get("yard_transhipment_teus"))
    parts = [x for x in (yi, ye, ytr) if x is not None]
    yard_total: Any = sum(parts) if parts else None
    if yard_total is not None and yard_total == int(yard_total):
        yard_total = int(yard_total)
    return {
        "report_date": report_date,
        "terminal": terminal,
        "vessels": picked.get("vessels"),
        "imp_teus": picked.get("imp_teus"),        # throughput (blank on live)
        "exp_teus": picked.get("exp_teus"),        # throughput (blank on live)
        "total_teus": picked.get("total_teus"),    # throughput total, if given
        "yard_import_teus": picked.get("yard_import_teus"),
        "yard_export_teus": picked.get("yard_export_teus"),
        "yard_transhipment_teus": picked.get("yard_transhipment_teus"),
        "yard_total_teus": yard_total,             # computed yard inventory sum
        "yard_occupancy_pct": picked.get("yard_occupancy_pct"),
    }


def map_daily_items(items: Any, *, report_date: str) -> MapOutcome:
    """Map raw daily-report items to daily_status rows. Never raises."""
    try:
        if not items:
            return MapOutcome("RAW_ONLY", [], [], "empty report answer — no items")

        rows: List[Dict[str, Any]] = []
        unmapped: set = set()
        max_recognised = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            item_date = _clean(item.get("reportDate")) or report_date
            by_terminal = item.get("byTerminal")
            if isinstance(by_terminal, list) and by_terminal:
                for term_row in by_terminal:
                    if not isinstance(term_row, dict):
                        continue
                    max_recognised = max(
                        max_recognised,
                        _recognised({_norm(k): v for k, v in term_row.items()}))
                    row = _map_terminal(term_row, report_date=item_date,
                                        unmapped=unmapped)
                    if row:
                        rows.append(row)
            else:
                max_recognised = max(
                    max_recognised,
                    _recognised({_norm(k): v for k, v in item.items()}))
                row = _map_terminal(item, report_date=item_date, unmapped=unmapped)
                if row:
                    rows.append(row)

        if max_recognised < 2:
            return MapOutcome("RAW_ONLY", [], sorted(unmapped),
                              "unrecognised item shape (<2 known keys)")
        if not rows:
            return MapOutcome("RAW_ONLY", [], sorted(unmapped),
                              "recognised shape but no terminal to key a "
                              "daily_status row")
        return MapOutcome("MAPPED", rows, sorted(unmapped),
                          f"mapped {len(rows)} daily_status row(s)")
    except Exception as exc:  # noqa: BLE001 - a mapper must never raise
        return MapOutcome("MAP_FAILED", [], [],
                          f"daily mapper error: {type(exc).__name__}: {exc}")


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def render_daily_csv(rows: List[Dict[str, Any]]) -> Optional[bytes]:
    """Render mapped rows to the daily_status upload CSV template (full column
    set, blanks for fields we do not carry). Returns None when empty."""
    if not rows:
        return None
    from services.performance.upload_parsers import DAILY_STATUS_COLS as COLS

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLS)
    for row in rows:
        cell = {
            "report_date": _s(row.get("report_date")),
            "terminal_code": _s(row.get("terminal")),
            "vessels": _s(row.get("vessels")),
            "imp_teus": _s(row.get("imp_teus")),
            "exp_teus": _s(row.get("exp_teus")),
            "total_teus": _s(row.get("total_teus")),
            "yard_import_teus": _s(row.get("yard_import_teus")),
            "yard_export_teus": _s(row.get("yard_export_teus")),
            "yard_transhipment_teus": _s(row.get("yard_transhipment_teus")),
            "yard_total_teus": _s(row.get("yard_total_teus")),
            "yard_occupancy_pct": _s(row.get("yard_occupancy_pct")),
        }
        writer.writerow([cell.get(col, "") for col in COLS])
    return buf.getvalue().encode("utf-8")


__all__ = ["map_daily_items", "render_daily_csv"]
