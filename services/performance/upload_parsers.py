"""Performance upload — CSV/XLSX parsing, validation, and mapping to perf_* rows.

Pure functions (no DB, no HTTP). Reads an uploaded CSV or XLSX for one of the three
report types, validates columns/dates/numbers/terminals, and maps valid rows into
insert-ready records for the EXISTING jnpa.perf_* tables. Terminal codes are
normalised the same way as the PDF importer (GTI→APMT, BMCTPL→BMCT).

For daily_status and monthly_teu it also derives the JN_PORT aggregate row (and the
daily TOTAL status row) from the uploaded terminal rows, so an upload immediately
drives the dashboard KPI cards (which read the JN_PORT / TOTAL rollups).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from typing import Any, Optional

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

_TERM_ALIAS = {
    "NSFT": "NSFT", "NSICT": "NSICT", "NSIGT": "NSIGT", "NSDT": "NSDT", "JNPCT": "JNPCT",
    "GTI": "APMT", "APM": "APMT", "APMT": "APMT",
    "BMCT": "BMCT", "BMCTPL": "BMCT", "BMCTPSA": "BMCT", "PSA": "BMCT",
    "JN PORT": "JN_PORT", "JNPORT": "JN_PORT", "JN_PORT": "JN_PORT",
}
_CONTAINER_TERMINALS = ("NSFT", "NSICT", "NSIGT", "APMT", "BMCT", "NSDT")


def norm_terminal(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    m = re.search(r"\(([A-Za-z]+)\)", s)
    if m and m.group(1).upper() in _TERM_ALIAS:
        return _TERM_ALIAS[m.group(1).upper()]
    return _TERM_ALIAS.get(s.upper())


def _num(raw: Any) -> tuple[Optional[float], bool]:
    """(value, ok). Blank -> (None, True); non-numeric -> (None, False)."""
    if raw is None:
        return None, True
    s = str(raw).replace(",", "").strip()
    if s == "" or s in ("-", "–", "—"):
        return None, True
    try:
        return float(s), True
    except ValueError:
        return None, False


def _int(raw: Any) -> tuple[Optional[int], bool]:
    v, ok = _num(raw)
    return (int(round(v)) if v is not None else None), ok


def _date(raw: Any) -> Optional[dt.date]:
    if raw is None:
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# --- report-type specifications ---------------------------------------------
DAILY_STATUS_COLS = [
    "report_date", "terminal_code", "vessels", "imp_teus", "exp_teus", "total_teus",
    "yard_import_teus", "yard_export_teus", "yard_transhipment_teus", "yard_total_teus",
    "yard_usable_capacity_teus", "yard_occupancy_pct", "icd_pendency_teus",
    "cfs_pendency_teus", "gate_in_teus", "gate_out_teus", "gate_total_teus",
    "reefer_total_slots", "reefer_occupied_slots", "reefer_available_slots",
]
MONTHLY_TEU_COLS = ["month_date", "terminal_code", "vessel_calls", "discharge_teus",
                    "load_teus", "total_teus"]
LDB_REPORT_COLS = ["report_month", "terminal_code", "cycle", "segment",
                   "dwell_hours", "dwell_hours_prev"]

SPECS: dict[str, dict[str, Any]] = {
    "daily_status": {"columns": DAILY_STATUS_COLS, "required": ["report_date", "terminal_code"],
                     "example": ["2026-06-01", "NSFT", "2", "1775", "1748", "3523",
                                 "5859", "6729", "5720", "18308", "23433", "78.13",
                                 "1153", "1988", "1588", "1428", "3016", "1104", "548", "556"]},
    "monthly_teu": {"columns": MONTHLY_TEU_COLS, "required": ["month_date", "terminal_code"],
                    "example": ["2026-06-01", "NSFT", "48", "55951", "56326", "112277"]},
    "ldb_report": {"columns": LDB_REPORT_COLS,
                   "required": ["report_month", "terminal_code", "cycle", "segment"],
                   "example": ["2026-06-01", "NSFT", "IMPORT", "OVERALL", "22.8", "29.3"]},
}


def template_csv(report_type: str) -> str:
    spec = SPECS[report_type]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(spec["columns"])
    w.writerow(spec["example"])
    return buf.getvalue()


# --- reading -----------------------------------------------------------------
def read_rows(content: bytes, filename: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (header, rows) from a CSV or XLSX byte payload. Raises ValueError on
    an unreadable/empty file or unsupported extension."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        try:
            header = [str(c).strip() if c is not None else "" for c in next(it)]
        except StopIteration:
            wb.close()
            raise ValueError("empty_file")
        rows = []
        for values in it:
            if not any(v not in (None, "") for v in values):
                continue
            rows.append({header[i]: (values[i] if i < len(values) else None)
                         for i in range(len(header))})
        wb.close()
        return header, rows
    if name.endswith(".csv") or name == "" or name.endswith(".txt"):
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        all_rows = [r for r in reader if any((c or "").strip() for c in r)]
        if not all_rows:
            raise ValueError("empty_file")
        header = [c.strip() for c in all_rows[0]]
        rows = []
        for r in all_rows[1:]:
            # skip commented/example helper lines
            if r and str(r[0]).strip().startswith("#"):
                continue
            rows.append({header[i]: (r[i] if i < len(r) else None) for i in range(len(header))})
        return header, rows
    raise ValueError("unsupported_format")


# --- validation + mapping ----------------------------------------------------
class ParseResult:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.preview: list[dict[str, Any]] = []
        self.report_keys: set = set()   # report_date / month_date / report_month values
        self.row_count = 0
        self.rejected = False           # structural failure (wrong template)

    def err(self, row: Optional[int], col: Optional[str], code: str, detail: str, raw: Any = None):
        self.errors.append({"row_number": row, "column_name": col, "error_code": code,
                            "error_detail": detail, "raw_value": (None if raw is None else str(raw))})

    def warn(self, row: Optional[int], col: Optional[str], code: str, detail: str):
        self.warnings.append({"row_number": row, "column_name": col, "error_code": code,
                              "error_detail": detail})


def _check_header(res: ParseResult, report_type: str, header: list[str]) -> bool:
    expected = SPECS[report_type]["columns"]
    hset, eset = set(header), set(expected)
    missing = [c for c in expected if c not in hset]
    if missing:
        res.err(None, None, "missing_columns",
                f"template mismatch — missing columns: {', '.join(missing)}")
        res.rejected = True
        return False
    return True


def parse(report_type: str, header: list[str], rows: list[dict[str, Any]]) -> ParseResult:
    res = ParseResult()
    if report_type not in SPECS:
        res.err(None, None, "unknown_report_type", f"unknown report type '{report_type}'")
        res.rejected = True
        return res
    if not _check_header(res, report_type, header):
        return res
    res.row_count = len(rows)
    if report_type == "daily_status":
        _parse_daily(res, rows)
    elif report_type == "monthly_teu":
        _parse_monthly(res, rows)
    else:
        _parse_ldb(res, rows)
    return res


def _numcols(res: ParseResult, rownum: int, row: dict, cols: list[str]) -> dict[str, Optional[float]]:
    out: dict[str, Optional[float]] = {}
    for c in cols:
        v, ok = _num(row.get(c))
        if not ok:
            res.err(rownum, c, "invalid_number", f"'{row.get(c)}' is not a number", row.get(c))
        out[c] = v
    return out


def _parse_daily(res: ParseResult, rows: list[dict]) -> None:
    traffic, status = [], []
    for i, row in enumerate(rows, start=2):     # row 1 = header
        rd = _date(row.get("report_date"))
        term = norm_terminal(row.get("terminal_code"))
        if rd is None:
            res.err(i, "report_date", "invalid_date", f"'{row.get('report_date')}' is not a valid date", row.get("report_date"))
        if term is None:
            res.err(i, "terminal_code", "unknown_terminal", f"'{row.get('terminal_code')}' is not a known terminal", row.get("terminal_code"))
        nums = _numcols(res, i, row, [c for c in DAILY_STATUS_COLS if c not in ("report_date", "terminal_code")])
        occ = nums.get("yard_occupancy_pct")
        if occ is not None and (occ < 0 or occ > 100):
            res.warn(i, "yard_occupancy_pct", "occupancy_out_of_range", f"{occ}% outside 0–100")
        if rd is None or term is None:
            continue
        res.report_keys.add(rd)
        traffic.append({"report_date": rd, "terminal_code": term, "period": "DAY",
                        "vessels": (int(nums["vessels"]) if nums.get("vessels") is not None else None),
                        "imp_teus": nums.get("imp_teus"), "exp_teus": nums.get("exp_teus"),
                        "total_teus": nums.get("total_teus")})
        status.append({"report_date": rd, "terminal_code": term,
                       "icd_pendency_teus": nums.get("icd_pendency_teus"),
                       "cfs_pendency_teus": nums.get("cfs_pendency_teus"),
                       "yard_import_teus": nums.get("yard_import_teus"),
                       "yard_export_teus": nums.get("yard_export_teus"),
                       "yard_transhipment_teus": nums.get("yard_transhipment_teus"),
                       "yard_total_teus": nums.get("yard_total_teus"),
                       "yard_usable_capacity_teus": nums.get("yard_usable_capacity_teus"),
                       "yard_occupancy_pct": occ,
                       "gate_in_teus": nums.get("gate_in_teus"), "gate_out_teus": nums.get("gate_out_teus"),
                       "gate_total_teus": nums.get("gate_total_teus"),
                       "reefer_total_slots": (int(nums["reefer_total_slots"]) if nums.get("reefer_total_slots") is not None else None),
                       "reefer_occupied_slots": (int(nums["reefer_occupied_slots"]) if nums.get("reefer_occupied_slots") is not None else None),
                       "reefer_available_slots": (int(nums["reefer_available_slots"]) if nums.get("reefer_available_slots") is not None else None)})
    # derive JN_PORT traffic + TOTAL status rollups per report_date from real rows
    _daily_rollups(traffic, status)
    res.records = {"snapshot": [{"report_date": d} for d in sorted(res.report_keys)],
                   "traffic": traffic, "status": status}
    res.preview = [dict(r) for r in status[:20]]


def _sum(vals):
    xs = [v for v in vals if v is not None]
    return sum(xs) if xs else None


def _daily_rollups(traffic: list[dict], status: list[dict]) -> None:
    from collections import defaultdict
    by_date_t = defaultdict(list)
    for r in traffic:
        if r["terminal_code"] in _CONTAINER_TERMINALS:
            by_date_t[r["report_date"]].append(r)
    for d, rs in by_date_t.items():
        traffic.append({"report_date": d, "terminal_code": "JN_PORT", "period": "DAY",
                        "vessels": _sum(r.get("vessels") for r in rs),
                        "imp_teus": _sum(r.get("imp_teus") for r in rs),
                        "exp_teus": _sum(r.get("exp_teus") for r in rs),
                        "total_teus": _sum(r.get("total_teus") for r in rs)})
    by_date_s = defaultdict(list)
    for r in status:
        if r["terminal_code"] in _CONTAINER_TERMINALS:
            by_date_s[r["report_date"]].append(r)
    for d, rs in by_date_s.items():
        yt = _sum(r.get("yard_total_teus") for r in rs)
        uc = _sum(r.get("yard_usable_capacity_teus") for r in rs)
        status.append({"report_date": d, "terminal_code": "TOTAL",
                       "icd_pendency_teus": _sum(r.get("icd_pendency_teus") for r in rs),
                       "cfs_pendency_teus": _sum(r.get("cfs_pendency_teus") for r in rs),
                       "yard_import_teus": _sum(r.get("yard_import_teus") for r in rs),
                       "yard_export_teus": _sum(r.get("yard_export_teus") for r in rs),
                       "yard_transhipment_teus": _sum(r.get("yard_transhipment_teus") for r in rs),
                       "yard_total_teus": yt,
                       "yard_usable_capacity_teus": uc,
                       "yard_occupancy_pct": (round(yt / uc * 100, 2) if yt is not None and uc else None),
                       "gate_in_teus": _sum(r.get("gate_in_teus") for r in rs),
                       "gate_out_teus": _sum(r.get("gate_out_teus") for r in rs),
                       "gate_total_teus": _sum(r.get("gate_total_teus") for r in rs),
                       "reefer_total_slots": _sum(r.get("reefer_total_slots") for r in rs),
                       "reefer_occupied_slots": _sum(r.get("reefer_occupied_slots") for r in rs),
                       "reefer_available_slots": _sum(r.get("reefer_available_slots") for r in rs)})


def _parse_monthly(res: ParseResult, rows: list[dict]) -> None:
    monthly = []
    for i, row in enumerate(rows, start=2):
        md = _date(row.get("month_date"))
        term = norm_terminal(row.get("terminal_code"))
        if md is None:
            res.err(i, "month_date", "invalid_date", f"'{row.get('month_date')}' is not a valid date", row.get("month_date"))
        if term is None:
            res.err(i, "terminal_code", "unknown_terminal", f"'{row.get('terminal_code')}' is not a known terminal", row.get("terminal_code"))
        nums = _numcols(res, i, row, ["vessel_calls", "discharge_teus", "load_teus", "total_teus"])
        if md is None or term is None:
            continue
        md = md.replace(day=1)
        fy_start = md.year if md.month >= 4 else md.year - 1
        res.report_keys.add(md)
        monthly.append({"fiscal_year": f"FY-{fy_start}-{str(fy_start + 1)[2:]}",
                        "month_date": md, "year_label": str(md.year),
                        "month_label": [k for k, v in MONTHS.items() if v == md.month][0],
                        "terminal_code": term,
                        "vessel_calls": (int(nums["vessel_calls"]) if nums.get("vessel_calls") is not None else None),
                        "discharge_teus": nums.get("discharge_teus"),
                        "load_teus": nums.get("load_teus"), "total_teus": nums.get("total_teus")})
    # JN_PORT monthly rollup per month from real container-terminal rows
    from collections import defaultdict
    by_m = defaultdict(list)
    for r in monthly:
        if r["terminal_code"] in _CONTAINER_TERMINALS:
            by_m[r["month_date"]].append(r)
    for md, rs in by_m.items():
        fy_start = md.year if md.month >= 4 else md.year - 1
        monthly.append({"fiscal_year": f"FY-{fy_start}-{str(fy_start + 1)[2:]}", "month_date": md,
                        "year_label": str(md.year),
                        "month_label": [k for k, v in MONTHS.items() if v == md.month][0],
                        "terminal_code": "JN_PORT",
                        "vessel_calls": _sum(r.get("vessel_calls") for r in rs),
                        "discharge_teus": _sum(r.get("discharge_teus") for r in rs),
                        "load_teus": _sum(r.get("load_teus") for r in rs),
                        "total_teus": _sum(r.get("total_teus") for r in rs)})
    res.records = {"monthly": monthly}
    res.preview = [dict(r) for r in monthly[:20]]


def _parse_ldb(res: ParseResult, rows: list[dict]) -> None:
    ldb = []
    for i, row in enumerate(rows, start=2):
        rm = _date(row.get("report_month"))
        term = norm_terminal(row.get("terminal_code"))
        cycle = str(row.get("cycle") or "").strip().upper()
        segment = str(row.get("segment") or "").strip().upper()
        if rm is None:
            res.err(i, "report_month", "invalid_date", f"'{row.get('report_month')}' is not a valid date", row.get("report_month"))
        if term is None:
            res.err(i, "terminal_code", "unknown_terminal", f"'{row.get('terminal_code')}' is not a known terminal", row.get("terminal_code"))
        if cycle not in ("IMPORT", "EXPORT"):
            res.err(i, "cycle", "invalid_cycle", f"'{row.get('cycle')}' must be IMPORT or EXPORT", row.get("cycle"))
        if segment not in ("OVERALL", "TRUCK", "TRAIN"):
            res.err(i, "segment", "invalid_segment", f"'{row.get('segment')}' must be OVERALL/TRUCK/TRAIN", row.get("segment"))
        nums = _numcols(res, i, row, ["dwell_hours", "dwell_hours_prev"])
        if rm is None or term is None or cycle not in ("IMPORT", "EXPORT") or segment not in ("OVERALL", "TRUCK", "TRAIN"):
            continue
        rm = rm.replace(day=1)
        res.report_keys.add(rm)
        ldb.append({"report_month": rm, "terminal_code": term, "cycle": cycle, "segment": segment,
                    "dwell_hours": nums.get("dwell_hours"), "dwell_hours_prev": nums.get("dwell_hours_prev")})
    res.records = {"ldb_port_dwell": ldb}
    res.preview = [dict(r) for r in ldb[:20]]
