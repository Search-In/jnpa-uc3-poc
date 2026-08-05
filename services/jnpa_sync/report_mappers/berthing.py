"""Berthing-report item mapper + CSV renderer.

Maps the ``berthing-reports`` JSON items onto the normalised vessel-call model
the berthing upload pipeline already validates, then renders them to the exact
CSV template :func:`services.berthing.upload_parsers.template_csv` produces —
so mapped rows land through ``BerthingUploadService.import_file`` (sha-ledger
dedup, per-terminal validation) with NO new SQL.

Real API shape (captured from the live endpoint, Aug-2026). Each report item is
one **per-terminal** daily berthing report; the vessel rows are NESTED:

    {
      "reportType": "DAILY_BERTHING_REPORT",
      "terminal": "APMT",
      "reportDate": "2026-08-05",
      "summary": {...},
      "vesselCalls": [
        {"vesselName","voyage","line","berth","alongside","operationsStart",
         "operationsEnd","sailed","importMoves","exportMoves","totalMoves"}, ...
      ]
    }

So the mapper iterates ``item.vesselCalls`` for the rows and takes the terminal
from the item. A flat item (no ``vesselCalls`` array — e.g. the simulator's
synthetic keys or a future shape) is treated as a single vessel row via the same
alias table, so the MAPPED path still runs offline and drift degrades, never
raises.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional

from . import MapOutcome

# canonical field -> accepted NORMALISED keys (first non-empty wins). Includes
# the live vesselCalls field names and the simulator's synthetic keys.
_ALIASES: Dict[str, tuple] = {
    "vessel_name": ("vesselname", "vessel", "vesselnm", "shipname", "ship",
                    "name"),
    "vessel_call": ("voyage", "vesselcall", "vcn", "voyagenumber", "voyageno",
                    "rotation", "rotationno", "callsign", "viano", "via"),
    "line": ("line", "shippingline", "carrier", "operator", "linecode",
             "lineoperator"),
    "imo": ("imo", "imonumber", "imono"),
    "berth": ("berth", "berthno", "bertnno", "berthnumber", "berthcode",
              "berthid"),
    "eta": ("eta", "expectedarrival", "expectedtimeofarrival", "arrival"),
    "ata": ("alongside", "ata", "actualarrival", "berthed", "arrivaltime"),
    "berthing_time": ("madefast", "berthingtime", "etb", "expectedberthing",
                      "expectedtimeofberthing", "berthing"),
    "cargo_start": ("operationsstart", "cargooperationstart", "opsstart",
                    "cargostart", "cargostarttime", "workstart"),
    "cargo_end": ("operationsend", "cargooperationend", "opsend", "cargoend",
                  "cargoendtime", "workend"),
    "etd": ("sailed", "atd", "departure", "departuretime", "etd",
            "expecteddeparture", "sailing", "sailingtime"),
    "status": ("status", "callstatus", "vesselstatus"),
}

# keys carried on the report ITEM (not the vessel row) — never counted as a
# recognised vessel-row mapping key.
_ITEM_KEYS = ("reportdate", "date", "day", "terminal", "terminalcode",
              "reporttype", "reportgeneratedat", "modifieddate", "summary",
              "vesselcalls", "sourceformat")


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).strip().lower())


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pick(norm_row: Dict[str, Any], field: str):
    for alias in _ALIASES[field]:
        if alias in norm_row:
            value = _clean(norm_row[alias])
            if value is not None:
                return value, alias
    return None, None


def _recognised(norm_row: Dict[str, Any]) -> int:
    return sum(1 for f in _ALIASES if _pick(norm_row, f)[0] is not None)


def _map_call(raw: Dict[str, Any], *, terminal: Optional[str],
              unmapped: set) -> Optional[Dict[str, Any]]:
    """Map one vessel-call dict → a normalised row (or None if unkeyable)."""
    norm = {_norm(k): v for k, v in raw.items() if _norm(k)}
    picked: Dict[str, str] = {}
    consumed: set = set()
    for field in _ALIASES:
        value, alias = _pick(norm, field)
        if value is not None:
            picked[field] = value
            consumed.add(alias)
    unmapped.update(k for k in norm if k not in consumed and k not in _ITEM_KEYS)
    vessel = picked.get("vessel_name")
    voyage = picked.get("vessel_call")
    if not (vessel and voyage):
        return None
    return {
        "terminal": picked.get("terminal") or terminal,
        "vessel_name": vessel,
        "vessel_call": voyage,
        "line": picked.get("line"),
        "imo": picked.get("imo"),
        "berth": picked.get("berth"),
        "eta": picked.get("eta"),
        "ata": picked.get("ata"),
        "berthing_time": picked.get("berthing_time") or picked.get("ata"),
        "etd": picked.get("etd"),
        "cargo_start": picked.get("cargo_start"),
        "cargo_end": picked.get("cargo_end"),
        "status": picked.get("status")
        or ("Departed" if picked.get("etd") else "Alongside"),
    }


def map_berthing_items(items: Any, *, report_date: str,
                       terminal: Optional[str]) -> MapOutcome:
    """Map raw berthing-report items to vessel-call rows. Never raises."""
    try:
        if not items:
            return MapOutcome("RAW_ONLY", [], [], "empty report answer — no items")

        rows: List[Dict[str, Any]] = []
        unmapped: set = set()
        max_recognised = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            item_terminal = _clean(item.get("terminal")) or terminal
            calls = item.get("vesselCalls")
            if isinstance(calls, list) and calls:
                # Real nested shape: iterate the per-terminal vessel calls.
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    max_recognised = max(
                        max_recognised,
                        _recognised({_norm(k): v for k, v in call.items()}))
                    row = _map_call(call, terminal=item_terminal,
                                    unmapped=unmapped)
                    if row:
                        rows.append(row)
            else:
                # Flat item (simulator/synthetic or an unforeseen shape).
                max_recognised = max(
                    max_recognised,
                    _recognised({_norm(k): v for k, v in item.items()}))
                row = _map_call(item, terminal=item_terminal, unmapped=unmapped)
                if row is not None:
                    rows.append(row)

        if max_recognised < 2:
            return MapOutcome("RAW_ONLY", [], sorted(unmapped),
                              "unrecognised item shape (<2 known keys)")
        if not rows:
            return MapOutcome("RAW_ONLY", [], sorted(unmapped),
                              "recognised shape but no vessel_name+vessel_call "
                              "to key a berthing row")
        return MapOutcome("MAPPED", rows, sorted(unmapped),
                          f"mapped {len(rows)} berthing vessel-call row(s)")
    except Exception as exc:  # noqa: BLE001 - a mapper must never raise
        return MapOutcome("MAP_FAILED", [], [],
                          f"berthing mapper error: {type(exc).__name__}: {exc}")


def render_berthing_csv(rows: List[Dict[str, Any]]) -> Optional[bytes]:
    """Render mapped rows to the berthing upload CSV template (column order and
    header taken verbatim from the upload parser, so the tabular reader accepts
    it). Returns None when nothing renderable is present."""
    if not rows:
        return None
    from services.berthing.upload_parsers import _TEMPLATE_COLS as COLS

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLS)
    for row in rows:
        cell = {
            "Terminal": row.get("terminal") or "",
            "Vessel Name": row.get("vessel_name") or "",
            "IMO Number": row.get("imo") or "",
            "Voyage Number": row.get("vessel_call") or "",
            "Shipping Line": row.get("line") or "",
            "Berth Number": row.get("berth") or "",
            "ETA": row.get("eta") or "",
            "ATA": row.get("ata") or "",
            "Berthing Time": row.get("berthing_time") or "",
            "Departure Time": row.get("etd") or "",
            "Cargo Operation Start": row.get("cargo_start") or "",
            "Cargo Operation End": row.get("cargo_end") or "",
            "Status": row.get("status") or "",
        }
        writer.writerow([cell.get(col, "") for col in COLS])
    return buf.getvalue().encode("utf-8")


__all__ = ["map_berthing_items", "render_berthing_csv"]
