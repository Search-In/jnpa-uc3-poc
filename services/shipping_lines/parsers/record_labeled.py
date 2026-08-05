"""Parser for the record-labelled "flat-file EDI" shipping-line feeds (GTI, NSFT).

These files interleave record types in column 0:

    Header / HeaderField  row   -> HDR field names
    HDRADVANCE            row   -> HDR values (Line_Code, Vessel_Code, Voyage_Number, Direction, ...)
    RecordLabel           row   -> the REAL container column names
    CTR                  rows   -> one container each

The container column names therefore live in the ``RecordLabel`` row (NOT row 0),
so we resolve them there, then map each ``CTR`` row exactly like a flat list.
"""
from __future__ import annotations

from typing import Any, Optional

from .column_maps import map_container_row
from .common import ParsedList, ShippingLineParseError, clean, norm_header
from .flat_tabular import read_rows

_HDR_NAME_MARKERS = {"HEADER", "HEADERFIELD"}
_HDR_VALUE_MARKER = "HDRADVANCE"
_LABEL_MARKERS = {"RECORDLABEL"}
_DATA_MARKER = "CTR"

# HDR field aliases (normalised) -> ledger envelope key.
_HDR_MAP = {
    "linecode": "line_code",
    "line": "line_code",
    "vesselcode": "vessel_visit",
    "obvesselvisit": "vessel_visit",
    "voyagenumber": "voyage",
    "voyage": "voyage",
    "direction": "direction",
    "messagetype": "direction",
}


def _row0(r: list[Any]) -> str:
    return (clean(r[0]) or "").upper() if r else ""


def parse_record_labelled(path: str, *, list_type: str, terminal: str) -> ParsedList:
    rows = read_rows(path)

    hdr_names: Optional[list[Any]] = None
    hdr_values: Optional[list[Any]] = None
    label_names: Optional[list[Any]] = None
    data_rows: list[list[Any]] = []

    for r in rows:
        marker = _row0(r)
        if marker in _HDR_NAME_MARKERS:
            hdr_names = r
        elif marker == _HDR_VALUE_MARKER:
            hdr_values = r
        elif marker in _LABEL_MARKERS:
            label_names = r
        elif marker == _DATA_MARKER:
            data_rows.append(r)

    if label_names is None:
        raise ShippingLineParseError(f"no RecordLabel row in {path}")

    labels = [clean(c) or "" for c in label_names]
    containers: list[dict[str, Any]] = []
    for raw in data_rows:
        raw_row = {labels[i]: raw[i] for i in range(min(len(labels), len(raw))) if labels[i]}
        mapped = map_container_row(raw_row, list_type=list_type, terminal=terminal)
        if mapped is not None:
            containers.append(mapped)

    # List-level envelope from the HDR name/value pair (best-effort).
    envelope: dict[str, Optional[str]] = {
        "vessel_visit": None, "voyage": None, "line_code": None, "direction": None,
    }
    if hdr_names and hdr_values:
        for i in range(min(len(hdr_names), len(hdr_values))):
            key = _HDR_MAP.get(norm_header(hdr_names[i]))
            val = clean(hdr_values[i])
            if key and val and not envelope.get(key):
                envelope[key] = val
    if not envelope["line_code"]:
        envelope["line_code"] = next((c["shipping_line_code"] for c in containers
                                      if c.get("shipping_line_code")), None)

    return ParsedList(
        header={"list_type": list_type, "terminal": terminal, **envelope},
        containers=containers,
        record_count=len(containers),
    )
