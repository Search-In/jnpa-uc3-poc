"""LEO (Let Export Order) ``.xlsx`` parser.

Columns: ``SB Number | SB Date | Site ID | Rotation Number | LEO Date | Action``.
Header row is row 0; blank rows (no SB Number) are skipped.
"""
from __future__ import annotations

from typing import Any

from .common import ParsedMessage, clean, coerce_cell_date

_EXPECTED = ("SB Number", "SB Date", "Site ID", "Rotation Number", "LEO Date", "Action")


def parse_leo_xlsx(path: str) -> ParsedMessage:
    """Parse a LEO workbook into a :class:`ParsedMessage`.

    ``payload = {"rows": [ {sb_no, sb_date, site_id, rotation_no, leo_date, action} ]}``."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    out: list[dict[str, Any]] = []
    for r in rows[1:]:  # skip header
        if not r:
            continue
        sb_no = clean(str(r[0])) if r[0] is not None else None
        if not sb_no:
            continue
        out.append({
            "sb_no": sb_no,
            "sb_date": coerce_cell_date(r[1] if len(r) > 1 else None),
            "site_id": clean(str(r[2])) if len(r) > 2 and r[2] is not None else None,
            "rotation_no": clean(str(r[3])) if len(r) > 3 and r[3] is not None else None,
            "leo_date": coerce_cell_date(r[4] if len(r) > 4 else None),
            "action": clean(str(r[5])) if len(r) > 5 and r[5] is not None else None,
        })

    message = {
        "message_type": "LEO",
        "module": "LEO",
        "primary_ref": out[0]["sb_no"] if out else None,
    }
    return ParsedMessage(message=message, payload={"rows": out}, record_count=len(out))
