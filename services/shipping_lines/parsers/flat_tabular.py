"""Readers for the tabular shipping-line files + the flat (named-header) parser.

``read_rows`` is the single physical reader for CSV / .xls / .xlsx (shared with the
record-labelled parser). ``parse_flat`` handles the terminals whose first row is
already the real container-header row (EAL_BMCT, IAL APMT, IAL BMCT, and every
.xls/.xlsx list): one container per data row, mapped via
:func:`services.shipping_lines.parsers.column_maps.map_container_row`.
"""
from __future__ import annotations

import csv
from typing import Any, Optional

from .column_maps import map_container_row
from .common import ParsedList, ShippingLineParseError, clean, norm_header


def read_rows(path: str) -> list[list[Any]]:
    """Read a CSV / .xls / .xlsx (first worksheet) into a list of raw cell rows.

    Trailing fully-empty rows are trimmed. Raises :class:`ShippingLineParseError`
    for an unreadable/empty workbook — a STRUCTURAL failure, not a bad value."""
    lower = path.lower()
    try:
        if lower.endswith(".csv"):
            rows = _read_csv(path)
        elif lower.endswith(".xlsx"):
            rows = _read_xlsx(path)
        elif lower.endswith(".xls"):
            rows = _read_xls(path)
        else:
            raise ShippingLineParseError(f"unsupported tabular file: {path}")
    except ShippingLineParseError:
        raise
    except Exception as exc:  # noqa: BLE001 — any reader failure is structural
        raise ShippingLineParseError(f"cannot read {path}: {exc}") from exc
    while rows and all(c is None or str(c).strip() == "" for c in rows[-1]):
        rows.pop()
    if not rows:
        raise ShippingLineParseError(f"empty file: {path}")
    return rows


def _read_csv(path: str) -> list[list[Any]]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return [list(r) for r in csv.reader(fh, dialect)]


def _read_xlsx(path: str) -> list[list[Any]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _read_xls(path: str) -> list[list[Any]]:
    import xlrd

    book = xlrd.open_workbook(path)
    sh = book.sheet_by_index(0)
    return [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]


def looks_record_labelled(rows: list[list[Any]]) -> bool:
    """True for the HDR/RecordLabel/CTR "flat-file EDI" shape (GTI, NSFT feeds)."""
    for r in rows[:5]:
        if r and clean(r[0]) and clean(r[0]).upper() in ("HDRADVANCE", "HEADERFIELD"):
            return True
    return False


def _header_context(containers: list[dict[str, Any]]) -> dict[str, Optional[str]]:
    """Best-effort list-level envelope from the first row that carries each value."""
    def first(field: str) -> Optional[str]:
        for c in containers:
            if c.get(field):
                return c[field]
        return None

    return {
        "vessel_visit": first("vessel_visit"),
        "voyage": first("voyage"),
        "line_code": first("shipping_line_code"),
        "direction": None,
    }


def parse_flat(path: str, *, list_type: str, terminal: str) -> ParsedList:
    """Parse a named-header tabular list (row 0 = real container headers)."""
    rows = read_rows(path)
    header = [clean(c) or "" for c in rows[0]]
    if not any(header):
        raise ShippingLineParseError(f"no header row in {path}")

    containers: list[dict[str, Any]] = []
    for raw in rows[1:]:
        raw_row = {header[i]: raw[i] for i in range(min(len(header), len(raw))) if header[i]}
        mapped = map_container_row(raw_row, list_type=list_type, terminal=terminal)
        if mapped is not None:
            containers.append(mapped)

    ctx = _header_context(containers)
    return ParsedList(
        header={"list_type": list_type, "terminal": terminal, **ctx},
        containers=containers,
        record_count=len(containers),
    )
