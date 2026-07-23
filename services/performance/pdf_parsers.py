"""Performance & Daily Reports PDF parsers (Module 12) — the ONE parsing engine.

These parsers were validated field-by-field against the official JNPA PDFs and are
reused VERBATIM here; this module only relocates them from
``scripts/import_performance_reports.py`` into the service layer so BOTH callers share
one implementation (no drift):

  * ``scripts/import_performance_reports.py`` — offline bulk backfill (imports from here)
  * ``services.performance.upload_service``   — the interactive client upload flow

The only adaptation is I/O: the offline script opens a filesystem ``Path``, while an
upload arrives as ``bytes``. :func:`open_pdf` normalises both, so every parser below
takes a ``source`` that is either a ``Path`` or raw ``bytes``. Extraction logic,
column maths and terminal mapping are unchanged.

Report type -> parser:
    Daily Status Report  -> parse_daily()   -> traffic / tonnage / status / vessels
    FY JN Port TEUs      -> parse_monthly() -> monthly
    NLDS / LDB Analytics -> parse_ldb()     -> port_dwell / facility / congestion /
                                               routes / weather

Pure + DB-free (unit-testable). Raises ValueError (never a bare ImportError or a
pdfplumber crash) so the upload layer degrades to a clean REJECTED instead of a 500.
"""
from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

PdfSource = Union[Path, bytes, str]

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# Terminal-label normalisation -> canonical perf_terminals.code
_TERM_ALIAS = {
    "NSFT": "NSFT", "NSICT": "NSICT", "NSIGT": "NSIGT", "NSDT": "NSDT",
    "JNPCT": "JNPCT",
    "GTI": "APMT", "APM": "APMT", "APMT": "APMT",
    "BMCT": "BMCT", "BMCTPL": "BMCT", "BMCTPSA": "BMCT", "PSA": "BMCT",
    "JN PORT": "JN_PORT", "JNPORT": "JN_PORT", "JN_PORT": "JN_PORT",
    "TOTAL": "TOTAL",
}


# --- primitive parsers -------------------------------------------------------
def num(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    # pdfplumber wraps a too-wide number across lines (e.g. '17,228,831.8\n0' for
    # 17228831.80) — join the wrapped digits by removing the newline, not spacing it.
    s = str(raw).replace(",", "").replace("\n", "").strip()
    if s in ("", "-", "–", "—", "N/A", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def as_int(raw: Any) -> Optional[int]:
    f = num(raw)
    return int(round(f)) if f is not None else None


def pct(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).replace("%", "").replace(",", "").replace("\n", "").strip()
    if s in ("", "-", "–", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def norm_terminal(raw: Any) -> Optional[str]:
    """Return the canonical terminal code, resolving aliases and full names like
    'Nhava Sheva Freeport Terminal (NSFT)'."""
    if raw is None:
        return None
    s = str(raw).replace("\n", " ").strip()
    if not s:
        return None
    m = re.search(r"\(([A-Za-z]+)\)", s)          # code inside parentheses
    if m and m.group(1).upper() in _TERM_ALIAS:
        return _TERM_ALIAS[m.group(1).upper()]
    key = s.upper()
    return _TERM_ALIAS.get(key)


def parse_dt(raw: Any) -> Optional[dt.datetime]:
    """Parse 'DD-MM-YYYY HH:MM' (vessel berth times) into IST-aware datetime."""
    if raw is None:
        return None
    s = str(raw).replace("\n", " ").strip()
    if not s:
        return None
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _flat(table: List[list]) -> str:
    return " ".join(str(c) for row in table for c in row if c)


# --- source handling ---------------------------------------------------------
def open_pdf(source: PdfSource):
    """Open a Path / str path / raw upload bytes as a pdfplumber document.

    Raises ValueError (not ImportError / a pdfplumber traceback) so callers can turn
    any unreadable input into a clean validation error rather than a 500.
    """
    try:
        import pdfplumber
    except ImportError as exc:      # library absent from the runtime image
        raise ValueError("unreadable_pdf: PDF support is unavailable on the server "
                         "(pdfplumber is not installed)") from exc
    try:
        return pdfplumber.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray))
                               else str(source))
    except Exception as exc:        # noqa: BLE001 — corrupt / encrypted / not-a-PDF
        raise ValueError(f"unreadable_pdf: {exc}") from exc


def _has_text(pdf) -> bool:
    """True when the document carries a text layer. Scanned/image-only PDFs have none
    and cannot be parsed without OCR — callers surface `scanned_pdf` for those."""
    for page in pdf.pages:
        if (page.extract_text() or "").strip():
            return True
    return False

# =====================================================================
# DAILY STATUS REPORT
# =====================================================================
_DATE_RE = re.compile(r"(\d{2})[.\-](\d{2})[.\-](\d{4})")
_TERM_A = ["NSFT", "NSICT", "NSIGT", "APMT", "BMCT", "NSDT"]      # section A order
_TERM_C = ["JNPCT", "NSICT", "NSIGT", "APMT", "BMCT", "NSFT"]     # section C (rail) order
_TONNAGE_CATS = ["BPCL", "NSDT", "JJLTPL", "OTHER", "BULK_TOTAL",
                 "CONTAINER_TOTAL", "JNPA_TOTAL"]


def daily_date_from_name(name: str) -> Optional[dt.date]:
    m = _DATE_RE.search(name)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def _is_period_row(row: list) -> bool:
    return bool(row) and str(row[0]).strip().upper() in ("DAY", "MONTH", "YEAR")


def parse_daily(source: PdfSource, filename: str) -> Optional[Dict[str, Any]]:
    """Extract every section of one Daily Status Report PDF into row dicts."""
    report_date = daily_date_from_name(filename)
    if report_date is None:
        return None
    traffic: Dict[Tuple[str, str], Dict[str, Any]] = {}   # (terminal, period) -> row
    tonnage: List[Dict[str, Any]] = []
    status: Dict[str, Dict[str, Any]] = {}                # terminal -> row
    vessels: List[Dict[str, Any]] = []

    def traffic_row(term: str, period: str) -> Dict[str, Any]:
        key = (term, period)
        if key not in traffic:
            traffic[key] = {"report_date": report_date, "terminal_code": term,
                            "period": period}
        return traffic[key]

    def status_row(term: str) -> Dict[str, Any]:
        if term not in status:
            status[term] = {"report_date": report_date, "terminal_code": term}
        return status[term]

    with open_pdf(source) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables():
                if not t or not t[0]:
                    continue
                flat = _flat(t)
                header0 = str(t[0][0] or "").strip()
                # ---- Section A: container terminal TEUs ----
                if "JNPA AVG" in flat:
                    for row in t:
                        if not _is_period_row(row):
                            continue
                        period = str(row[0]).strip().upper()
                        cells = row[1:]
                        for k, term in enumerate(_TERM_A):
                            b = k * 4
                            r = traffic_row(term, period)
                            r["vessels"] = as_int(cells[b])
                            r["imp_teus"] = num(cells[b + 1])
                            r["exp_teus"] = num(cells[b + 2])
                            r["total_teus"] = num(cells[b + 3])
                        if len(cells) > 24:
                            traffic_row("JN_PORT", period)["total_teus"] = num(cells[24])
                # ---- Section B: tonnage ---- (markers unique to the tonnage table;
                # NOT just 'BPCL'/'JJLTPL', which also appear as terminal rows in
                # the Section-H vessel list)
                elif "Bulk Total" in flat and "Container Total" in flat:
                    for row in t:
                        if not _is_period_row(row):
                            continue
                        period = str(row[0]).strip().upper()
                        c = row
                        mapping = {
                            "BPCL": {"vessels": as_int(c[1]), "liquid_tonnes": num(c[2]),
                                     "total_tonnes": num(c[2])},
                            "NSDT": {"vessels": as_int(c[3]), "dry_bulk_tonnes": num(c[4]),
                                     "break_bulk_tonnes": num(c[5]), "liquid_tonnes": num(c[6])},
                            "JJLTPL": {"vessels": as_int(c[7]), "liquid_tonnes": num(c[8]),
                                       "total_tonnes": num(c[8])},
                            "OTHER": {"dry_bulk_tonnes": num(c[9]), "break_bulk_tonnes": num(c[10])},
                            "BULK_TOTAL": {"vessels": as_int(c[11]), "total_tonnes": num(c[12])},
                            "CONTAINER_TOTAL": {"vessels": as_int(c[13]), "total_tonnes": num(c[14])},
                            "JNPA_TOTAL": {"vessels": as_int(c[15]), "total_tonnes": num(c[16])},
                        }
                        for cat, vals in mapping.items():
                            rec = {"report_date": report_date, "category": cat, "period": period}
                            rec.update(vals)
                            if cat == "NSDT":  # derive total for multi-cargo terminal
                                rec["total_tonnes"] = sum(v for v in (rec.get("dry_bulk_tonnes"),
                                    rec.get("break_bulk_tonnes"), rec.get("liquid_tonnes")) if v)
                            elif cat == "OTHER":
                                rec["total_tonnes"] = sum(v for v in (rec.get("dry_bulk_tonnes"),
                                    rec.get("break_bulk_tonnes")) if v)
                            tonnage.append(rec)
                # ---- Section C: rail operations ----
                elif "JNPCT" in flat and "Rake" in flat:
                    for row in t:
                        if not _is_period_row(row):
                            continue
                        period = str(row[0]).strip().upper()
                        cells = row[1:]
                        for k, term in enumerate(_TERM_C):
                            b = k * 4
                            r = traffic_row(term, period)
                            r["rakes"] = as_int(cells[b])
                            r["rail_dis_teus"] = num(cells[b + 1])
                            r["rail_ldg_teus"] = num(cells[b + 2])
                            r["rail_total_teus"] = num(cells[b + 3])
                        if len(cells) > 25:
                            jr = traffic_row("JN_PORT", period)
                            jr["rakes"] = as_int(cells[24])
                            jr["rail_total_teus"] = num(cells[25])
                # ---- Section D: import pendency ----
                elif "ICD Pendency" in flat or "CFS Pendency" in flat:
                    cols = [norm_terminal(x) for x in t[0][1:]]
                    for row in t[1:]:
                        label = str(row[0] or "").upper()
                        field = "icd_pendency_teus" if "ICD" in label else \
                                ("cfs_pendency_teus" if "CFS" in label else None)
                        if not field:
                            continue
                        for ci, term in enumerate(cols):
                            if term:
                                status_row(term)[field] = num(row[ci + 1])
                # ---- Section E: yard inventory ----
                elif "Yard Inventory" in flat or "Usable capacity" in flat:
                    cols = [norm_terminal(x) for x in t[0][1:]]
                    field_map = {
                        "IMPORT": ("yard_import_teus", num),
                        "EXPORT": ("yard_export_teus", num),
                        "TRANSHIPMENT": ("yard_transhipment_teus", num),
                        "TOTAL": ("yard_total_teus", num),
                        "USABLE": ("yard_usable_capacity_teus", num),
                        "OCCUPANCY": ("yard_occupancy_pct", pct),
                    }
                    for row in t[1:]:
                        label = str(row[0] or "").upper()
                        key = ("OCCUPANCY" if "OCCUPANCY" in label else
                               "USABLE" if "USABLE" in label else
                               "TRANSHIPMENT" if "TRANSHIP" in label else
                               "IMPORT" if label.startswith("IMPORT") else
                               "EXPORT" if label.startswith("EXPORT") else
                               "TOTAL" if label.strip() == "TOTAL" else None)
                        if not key:
                            continue
                        field, conv = field_map[key]
                        for ci, term in enumerate(cols):
                            if term:
                                status_row(term)[field] = conv(row[ci + 1])
                # ---- Section F: gate movements ----
                elif "Gate Movements" in flat:
                    cols = [norm_terminal(x) for x in t[0][1:]]
                    fmap = {"IN": "gate_in_teus", "OUT": "gate_out_teus", "TOTAL": "gate_total_teus"}
                    for row in t[1:]:
                        label = str(row[0] or "").strip().upper()
                        field = fmap.get(label)
                        if not field:
                            continue
                        for ci, term in enumerate(cols):
                            if term:
                                status_row(term)[field] = num(row[ci + 1])
                # ---- Section G: reefer slots ----
                elif "Reefer slots" in flat:
                    cols = [norm_terminal(x) for x in t[0][1:]]
                    for row in t[1:]:
                        label = str(row[0] or "").upper()
                        field = ("reefer_total_slots" if "TOTAL" in label else
                                 "reefer_occupied_slots" if "OCCUPIED" in label else
                                 "reefer_available_slots" if "AVAILABLE" in label else None)
                        if not field:
                            continue
                        for ci, term in enumerate(cols):
                            if term:
                                status_row(term)[field] = as_int(row[ci + 1])
                # ---- Section H: vessels under operation ----
                elif header0 == "Terminal" and "Berth No" in flat:
                    last_term = None
                    for row in t[1:]:
                        term = norm_terminal(row[0]) or (str(row[0]).strip().upper() if row[0] else None)
                        if term:
                            last_term = term
                        term = term or last_term
                        berth = str(row[1] or "").strip()
                        vessel = str(row[3] or "").strip()
                        if not berth or not vessel:
                            continue   # idle berth (no vessel under operation) -> skip
                        vessels.append({
                            "report_date": report_date, "terminal_code": term or "UNKNOWN",
                            "berth_no": berth, "via_no": str(row[2] or "").strip() or None,
                            "vessel_name": vessel,
                            "cargo_commodity": str(row[4] or "").strip() or None,
                            "berthed_on": parse_dt(row[5]),
                            "expected_completion": parse_dt(row[6]),
                        })
    return {
        "report_date": report_date,
        "as_of_ts": dt.datetime.combine(report_date, dt.time(7, 0), tzinfo=IST),
        "source_file": filename,
        "traffic": list(traffic.values()),
        "tonnage": tonnage,
        "status": list(status.values()),
        "vessels": vessels,
    }

# =====================================================================
# MONTHLY JN PORT TEUs
# =====================================================================
_TERM_M = ["NSDT", "NSFT", "NSICT", "NSIGT", "APMT", "BMCT", "JN_PORT"]


def parse_monthly(source: PdfSource, filename: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open_pdf(source) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables():
                if not t:
                    continue
                for row in t:
                    if len(row) < 30:
                        continue
                    yr = str(row[0] or "").strip()
                    mon = str(row[1] or "").strip().upper()
                    if not re.fullmatch(r"\d{4}", yr) or mon not in MONTHS:
                        continue
                    year = int(yr)
                    mnum = MONTHS[mon]
                    fy_start = year if mnum >= 4 else year - 1
                    fiscal_year = f"FY-{fy_start}-{str(fy_start + 1)[2:]}"
                    month_date = dt.date(year, mnum, 1)
                    for k, term in enumerate(_TERM_M):
                        b = 2 + k * 4
                        out.append({
                            "fiscal_year": fiscal_year, "month_date": month_date,
                            "year_label": yr, "month_label": mon, "terminal_code": term,
                            "vessel_calls": as_int(row[b]),
                            "discharge_teus": num(row[b + 1]),
                            "load_teus": num(row[b + 2]),
                            "total_teus": num(row[b + 3]),
                        })
    # de-dup within file on (month_date, terminal) keeping first
    seen: set = set()
    deduped = []
    for r in out:
        key = (r["month_date"], r["terminal_code"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped

# =====================================================================
# NLDS / LDB ANALYTICS
# =====================================================================
_LDB_MONTHS = {"JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5,
               "JUNE": 6, "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10,
               "NOVEMBER": 11, "DECEMBER": 12}


def ldb_month_from_name(name: str) -> Optional[dt.date]:
    m = re.search(r"([A-Za-z]+)_(\d{4})", name)
    if m and m.group(1).upper() in _LDB_MONTHS:
        return dt.date(int(m.group(2)), _LDB_MONTHS[m.group(1).upper()], 1)
    return None


def parse_ldb(source: PdfSource, filename: str) -> Dict[str, List[Dict[str, Any]]]:
    rm = ldb_month_from_name(filename)
    if rm is None:
        return {}
    port_dwell: List[Dict[str, Any]] = []
    facility: List[Dict[str, Any]] = []
    congestion: List[Dict[str, Any]] = []
    routes: List[Dict[str, Any]] = []
    weather: List[Dict[str, Any]] = []

    def dwell_rows(table, cycle, segment):
        for row in table:
            term = norm_terminal(row[0])
            if not term or len(row) < 3:
                continue
            cur, prev = num(row[1]), num(row[2])
            if cur is None and prev is None:
                continue
            port_dwell.append({"report_month": rm, "terminal_code": term, "cycle": cycle,
                               "segment": segment, "dwell_hours": cur, "dwell_hours_prev": prev})

    def facility_rows(table, ftype):
        for row in table:
            # two facility columns per row: [name, cur, prev, '', name, cur, prev]
            for base in (0, 4):
                if base + 2 >= len(row):
                    continue
                nm = str(row[base] or "").replace("\n", " ").strip()
                cur, prev = num(row[base + 1]), num(row[base + 2])
                if not nm or nm.upper() in ("CFS", "ICD") or "Dwell Time" in nm:
                    continue
                if cur is None and prev is None:
                    continue
                facility.append({"report_month": rm, "facility_type": ftype,
                                 "facility_name": nm,
                                 "facility_name_norm": re.sub(r"[^A-Z0-9]", "", nm.upper()),
                                 "dwell_hours": cur, "dwell_hours_prev": prev})

    with open_pdf(source) as pdf:
        pages = pdf.pages
        for idx, page in enumerate(pages):
            tbls = page.extract_tables()
            flat_page = (page.extract_text() or "")
            for t in tbls:
                if not t or not t[0]:
                    continue
                flat = _flat(t)
                first = str(t[0][0] or "").strip()
                # Port dwell — exec summary (Import/Export cycle) and via-train/truck pages
                if first in ("Import Cycle", "Export Cycle") and "Port" in flat and "Terminals" in flat:
                    cyc = "IMPORT" if first == "Import Cycle" else "EXPORT"
                    # detect train vs truck via page title
                    seg = ("TRAIN" if "via TRAIN" in flat_page or "via Train" in flat_page and "Truck" not in flat[:40]
                           else "OVERALL")
                    # pages 14/20 have BOTH train (table0) and truck (table1); disambiguate below
                    dwell_rows(t[2:], cyc, "OVERALL" if "Snapshot" in flat_page or idx == 3 else seg)
                # CFS facility dwell
                elif "CFS Dwell Time" in flat and "CFS" in flat:
                    facility_rows(t[1:], "CFS")
                elif first == "ICD" and "Mar" in flat or ("ICD Dwell Time" in flat):
                    facility_rows(t, "ICD")
                # Congestion clusters
                elif "Cluster" in flat and "Congestion" in flat and "% of Total" in flat:
                    cyc = "IMPORT" if "Import" in flat_page else "EXPORT"
                    for row in t[1:]:
                        m = re.search(r"(\d+)", str(row[0] or ""))
                        if not m:
                            continue
                        congestion.append({
                            "report_month": rm, "cycle": cyc, "cluster_no": int(m.group(1)),
                            "cluster_name": str(row[1] or "").replace("\n", " ").strip() or None,
                            "cfs_count": as_int(row[2]), "pct_containers": pct(row[3]),
                            "congestion_level": (str(row[4] or "").strip().upper() or None),
                        })
                # Train route modal share
                elif "Route" in flat and ("Vadodra" in flat or "Ratlam" in flat):
                    cyc = "IMPORT" if "Import" in flat_page else "EXPORT"
                    for row in t:
                        nm = str(row[0] or "").replace("\n", " ").strip()
                        if "Route" not in nm or nm.lower().startswith("route"):
                            continue
                        share = pct(row[-1])
                        if share is None:
                            continue
                        routes.append({"report_month": rm, "cycle": cyc, "transport_mode": "TRAIN",
                                       "route_name": nm, "pct_share": share})
                # Weather-conditioned dwell (terminal-wise)
                elif first in ("IMPORT CYCLE", "EXPORT CYCLE") and "Weather" in flat:
                    cyc = "IMPORT" if first == "IMPORT CYCLE" else "EXPORT"
                    for row in t[1:]:
                        term = norm_terminal(row[0])
                        if not term:
                            continue
                        for w, val in (("NORMAL", num(row[1])), ("ABNORMAL", num(row[2]))):
                            if val is not None:
                                weather.append({"report_month": rm, "terminal_code": term,
                                                "cycle": cyc, "weather": w, "dwell_hours": val})

    # Train/truck segmented dwell (pages 14 import, 20 export): re-scan by page title
    with open_pdf(source) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if "Dwell Time Performance" not in txt:
                continue
            cyc = "IMPORT" if "Import Cycle" in txt else ("EXPORT" if "Export Cycle" in txt else None)
            if not cyc or "via TRAIN" not in txt:
                continue
            tbls = page.extract_tables()
            # table0 = TRAIN, table1 = TRUCK (both [term, mar, feb])
            for t, seg in ((tbls[0] if tbls else None, "TRAIN"),
                           (tbls[1] if len(tbls) > 1 else None, "TRUCK")):
                if not t:
                    continue
                dwell_rows(t[2:], cyc, seg)

    # de-dup port_dwell on unique key (OVERALL rows may be captured twice)
    seen: set = set()
    pd2 = []
    for r in port_dwell:
        k = (r["report_month"], r["terminal_code"], r["cycle"], r["segment"])
        if k in seen:
            continue
        seen.add(k)
        pd2.append(r)
    return {"port_dwell": pd2, "facility": facility, "congestion": congestion,
            "routes": routes, "weather": weather}

