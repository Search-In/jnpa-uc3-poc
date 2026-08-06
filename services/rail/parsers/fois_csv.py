"""FOIS Train Intimation CSV parser (group ``rail-fois``).

The NLDS/FOIS feed is a daily CSV of scheduled rake arrivals at JNPA. Real
header (verbatim from the corpus):

    Eda, Edd, ZoneTo, Last Reporting Station, Units, Station From,
    Last Reporting Division, RakeId, RakeName, Station To, ZoneFrom,
    Last Reporting Zone, Loaded Empty Flag (L/E), Last Status Time

Dates are ``DDMMYYYY:HH:MM`` (IST). RakeId is the train identity. Columns are
alias-mapped (header wording drifts), the known fields map to canonical keys
and anything else is preserved verbatim in ``extra``.

A "no arrivals" day ships a single quoted sentence instead of a table
(e.g. `"As of 23-06-2026 08:30:00, there are no scheduled train arrivals..."`);
that is a VALID empty intimation, not a rejection.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from jnpa_shared.iso6346 import is_valid_container_no  # noqa: F401 (parity import)

from . import (
    ParseResult, clean, norm_header, parse_ts, to_int, upper_or_none,
)

# canonical field -> accepted NORMALISED header aliases (first present wins).
ALIASES: dict[str, tuple[str, ...]] = {
    "rake_id": ("rakeid", "rakeno", "trainno", "trainid"),
    "rake_name": ("rakename", "trainname"),
    "units": ("units", "noofunits", "unit", "wagons"),
    "station_from": ("stationfrom", "fromstation", "originstation"),
    "station_to": ("stationto", "tostation", "destinationstation"),
    "zone_from": ("zonefrom", "fromzone"),
    "zone_to": ("zoneto", "tozone"),
    "last_reporting_station": ("lastreportingstation", "lastreportstation"),
    "last_reporting_division": ("lastreportingdivision", "lastreportdivision"),
    "last_reporting_zone": ("lastreportingzone", "lastreportzone"),
    "loaded_empty_flag": ("loadedemptyflagle", "loadedemptyflag", "leflag",
                          "loadedempty", "le"),
    "eda": ("eda", "eta", "estimateddatearrival", "estimatedarrival"),
    "edd": ("edd", "etd", "estimateddatedeparture", "estimateddeparture"),
    "last_status_time": ("laststatustime", "statustime", "lastupdate"),
}

# The one column we cannot do without.
_REQUIRED_LABEL = "RakeId"
_REQUIRED_ALIASES = ALIASES["rake_id"]

_TS_FIELDS = ("eda", "edd", "last_status_time")
_TEXT_FIELDS = ("rake_name",)
_UPPER_FIELDS = ("station_from", "station_to", "zone_from", "zone_to",
                 "last_reporting_station", "last_reporting_division",
                 "last_reporting_zone", "loaded_empty_flag")

_NO_ARRIVALS = "no scheduled train arrivals"


def _read_csv(content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    all_rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not all_rows:
        return [], []
    header = [c.strip() for c in all_rows[0]]
    rows = [{header[i]: (r[i] if i < len(r) else None)
             for i in range(len(header))}
            for r in all_rows[1:]]
    return header, rows


def _pick(row_norm: dict[str, Any], canonical: str):
    for alias in ALIASES.get(canonical, ()):
        if alias in row_norm:
            v = clean(row_norm[alias])
            if v is not None:
                return v
    return None


def parse(content: bytes, filename: str) -> ParseResult:
    res = ParseResult(feed="FOIS")

    # "no arrivals" sentinel day → valid, empty (never a rejection).
    head = content[:400].decode("utf-8-sig", errors="replace").lower()
    if _NO_ARRIVALS in head:
        res.reason = "no_scheduled_arrivals"
        return res

    header, rows = _read_csv(content)
    res.row_count = len(rows)

    hset = {norm_header(h) for h in header if norm_header(h)}
    if not any(a in hset for a in _REQUIRED_ALIASES):
        res.rejected = True
        res.reason = "missing_rake_id_column"
        res.err(None, _REQUIRED_LABEL, "missing_column",
                f"{_REQUIRED_LABEL} column not found — not a FOIS Train "
                "Intimation file.")
        return res

    known_aliases = {a for aliases in ALIASES.values() for a in aliases}
    for i, raw in enumerate(rows, start=1):
        row_norm = {norm_header(k): v for k, v in raw.items() if norm_header(k)}

        rake_id = upper_or_none(_pick(row_norm, "rake_id"))
        if not rake_id:
            res.err(i, _REQUIRED_LABEL, "empty_rake_id",
                    "RakeId is empty")
            res.invalid_count += 1
            continue

        rec: dict[str, Any] = {"rake_id": rake_id, "source_file": filename}
        for field in _TS_FIELDS:
            rec[field] = parse_ts(_pick(row_norm, field))
        for field in _TEXT_FIELDS:
            rec[field] = clean(_pick(row_norm, field))
        for field in _UPPER_FIELDS:
            rec[field] = upper_or_none(_pick(row_norm, field))
        rec["units"] = to_int(_pick(row_norm, "units"))

        # anything not mapped to a canonical field is kept verbatim.
        extra = {k: clean(v) for k, v in raw.items()
                 if norm_header(k) and norm_header(k) not in known_aliases
                 and clean(v) is not None}
        rec["extra"] = extra
        res.rows.append(rec)

    res.preview = [{
        "RakeId": r["rake_id"], "RakeName": r.get("rake_name"),
        "From": r.get("station_from"), "To": r.get("station_to"),
        "ETA": (r["eda"].strftime("%d/%m/%Y %H:%M") if r.get("eda") else None),
        "L/E": r.get("loaded_empty_flag"),
    } for r in res.rows[:20]]
    return res


__all__ = ["parse", "ALIASES"]
