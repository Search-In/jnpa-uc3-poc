"""Berthing Reports FULL PDF extractor (UC-III module 7) — verbatim, no data loss.

Captures EVERY table/panel on a terminal berthing-report PDF exactly as printed
(table_name + original columns + rows + page), complementing — never replacing — the
normalised vessel-call parser in :mod:`services.berthing.pdf_parsers`.

Approach (see docs/BERTHING_PDF_DATA_AUDIT.md §5): the reports are multi-panel free
layouts, so ``extract_tables()`` is unreliable and line text interleaves side-by-side
panels. We therefore work from **word coordinates** (``page.extract_words`` → x0/x1/top):

  1. detect the terminal (reuses :func:`pdf_parsers.detect_terminal`);
  2. load that terminal's declarative template — an ordered list of panels, each with a
     ``title`` (the unique section heading, used to locate the panel top + disambiguate
     stacked tables that share a column header), an optional ``find`` (the column-header
     line, searched at/below the title), and an x-band (fraction of page width) that
     isolates the panel from the ones beside it;
  3. per panel: read column anchors from the header word x-positions (or, for headerless
     label/value panels, discover columns by x-clustering), bound the panel vertically by
     the next panel below in an overlapping band, then assign each data word to its
     nearest-left column anchor — so interleaved panels never cross-contaminate;
  4. **word-level coverage**: every word a panel consumes is marked; any leftover words
     become an ``UNCAPTURED_TEXT`` table (``{"_raw": ...}``) so nothing is ever dropped.
     A panel whose header is missing is still emitted (empty, with a note).

Pure + DB-free (unit-testable). No change to the normalised path.
"""
from __future__ import annotations

import datetime as _dt
import io
import re
from typing import Any, Optional

from . import pdf_parsers as PDF

# ---------------------------------------------------------------- template model
# Panel fields:
#   name    – table_name
#   title   – tokens (ALL on one line, in band) locating the panel top / heading
#   find    – tokens locating the column-header line at/below the title; None → headerless
#   require – extra token required on the find line (disambiguates identical headers)
#   band    – (lo, hi) x fractions of page width isolating the panel
#   hlines  – number of header lines merged for column anchors (default 1)
_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "APMT": [
        {"name": "TIME_TABLE",         "title": ["Time", "Table"],        "find": ["Date", "Time", "Height"], "band": (0.0, 1.0), "hlines": 1},
        {"name": "ON_BERTH_VESSEL",    "title": ["ON", "BERTH", "VESSEL"], "find": ["Berth", "Vessel", "VIA"], "require": "ETC", "band": (0.0, 1.0), "hlines": 2},
        {"name": "SAILED_VESSEL",      "title": None,                     "find": ["Berth", "Vessel", "VIA"], "require": "Sailing", "band": (0.0, 1.0), "hlines": 2},
        {"name": "VESSELS_EXPECTED",   "title": ["Vessels", "Expected"],  "find": ["VIA", "Vessel", "Name", "LOA"], "band": (0.0, 0.76), "hlines": 2},
        {"name": "YARD_INVENTORY",     "title": ["Yard", "Inventory"],    "find": None, "band": (0.76, 0.875)},
        {"name": "GATE_MOVEMENTS",     "title": ["GATE", "MOVEMENTS"],    "find": ["GATE", "Cntrs"], "band": (0.875, 1.0), "hlines": 1},
        {"name": "RAIL_PENDENCY",      "title": ["RAIL", "PENDENCY"],     "find": None, "band": (0.82, 1.0)},
        {"name": "CFS_PENDENCY",       "title": ["CFS", "PENDENCY"],      "find": None, "band": (0.0, 0.76)},
        {"name": "TRAFFIC_THROUGHPUT", "title": ["THROUGHPUT"],           "find": ["VESSEL", "IMP", "EXP"], "band": (0.74, 1.0), "hlines": 1},
    ],
    "BMCT": [
        {"name": "TIDE_TABLE",         "title": ["Tide", "Table"],        "find": ["Date", "Time", "Height"], "band": (0.0, 0.30), "hlines": 1},
        {"name": "YARD_INVENTORY",     "title": ["Yard", "Inv"],          "find": None, "band": (0.72, 0.845)},
        {"name": "GATE_MOVEMENTS",     "title": ["GATE", "MOVEMENTS"],    "find": ["GATE", "Cntrs"], "band": (0.845, 1.0), "find_band": (0.80, 1.0), "hlines": 1},
        {"name": "VESSELS_ON_BERTHED", "title": ["VESSELS", "ON", "BERTHED"], "find": ["Berth", "Vessel", "VIA"], "band": (0.0, 1.0), "hlines": 2},
        {"name": "SAILED_VESSEL",      "title": ["SAILED", "VESSEL"],     "find": ["Berth", "Vessel", "VIA"], "band": (0.0, 1.0), "hlines": 2},
        {"name": "VESSELS_EXPECTED",   "title": ["VESSELS", "EXPECTED"],  "find": ["Vessel", "Name", "Service"], "band": (0.0, 0.74), "hlines": 2},
        {"name": "ICD_PENDENCY",       "title": None,                     "find": ["Dest", "Moves", "Teus"], "band": (0.74, 0.83), "find_band": (0.72, 1.0), "hlines": 1},
        {"name": "CFS_PENDENCY",       "title": None,                     "find": ["Dest", "Moves", "Teus"], "band": (0.83, 1.0), "find_band": (0.72, 1.0), "hlines": 1},
        {"name": "TRAFFIC_THROUGHPUT", "title": None,                     "find": ["VESSEL", "IMPORT", "EXPORT"], "band": (0.70, 1.0), "hlines": 1},
    ],
    "NSFT": [
        {"name": "TIDE_TABLE",         "title": ["Tide", "Table"],        "find": ["Time", "Tide"], "band": (0.0, 0.75), "hlines": 1},
        {"name": "VESSEL_SAILED_24H",  "title": ["Vessel", "Sailed"],     "find": ["SR", "Vessel", "Name"], "require": "Sailed", "band": (0.0, 0.72), "hlines": 1},
        {"name": "VESSEL_AT_BERTH",    "title": ["Vessel", "at", "Berth"], "find": ["SR", "Vessel", "Name"], "require": "ETC", "band": (0.0, 0.72), "hlines": 1},
        {"name": "VESSELS_EXPECTED",   "title": ["Vessels", "Expected"],  "find": ["SR", "Vessel", "Name"], "require": "ETA", "band": (0.0, 0.72), "hlines": 2},
        {"name": "YARD_INVENTORY",     "title": ["Yard", "Inventory"],    "find": None, "band": (0.66, 1.0)},
        {"name": "GATE_MOVEMENT_24H",  "title": ["Gate", "Movement"],     "find": None, "band": (0.66, 1.0), "find_band": (0.66, 1.0)},
        {"name": "ICD_PENDANCY",       "title": ["ICD", "Pendancy"],      "find": None, "band": (0.55, 0.86), "find_band": (0.55, 0.90)},
        {"name": "CFS_PENDANCY",       "title": ["CFS", "Pendancy"],      "find": None, "band": (0.80, 1.0), "find_band": (0.66, 1.0)},
    ],
    "DPWORLD": [
        {"name": "TIDE_TABLE",         "title": None,                     "find": ["DATE", "TIME", "HEIGHT"], "band": (0.0, 0.75), "hlines": 1},
        {"name": "YARD_INV",           "title": ["YARD", "INV"],          "find": None, "band": (0.75, 1.0)},
        {"name": "VESSELS_ON_BERTH",   "title": ["VESSELS", "ON", "BERTH"], "find": ["BERTH", "VESSEL", "VIA"], "band": (0.0, 1.0), "hlines": 1},
        {"name": "SAILED_VESSELS",     "title": ["SAILED", "VESSELS"],    "find": ["BERTH", "VESSEL", "VIA"], "band": (0.0, 1.0), "hlines": 1},
        {"name": "VESSELS_EXPECTED",   "title": ["VESSELS", "EXPECTED"],  "find": ["VESSEL", "NAME", "VIA"], "band": (0.0, 0.74), "hlines": 2},
        {"name": "ICD_PENDENCY",       "title": None,                     "find": ["DEST", "MVS", "TEUS"], "band": (0.745, 0.86), "find_band": (0.70, 1.0), "hlines": 1},
        {"name": "CFS_PENDENCY",       "title": None,                     "find": ["DEST", "MVS", "TEUS"], "band": (0.86, 1.0), "find_band": (0.70, 1.0), "hlines": 1},
        {"name": "TRAFFIC_THROUGHPUT", "title": None,                     "find": ["VSL", "IMP", "EXP"], "band": (0.60, 1.0), "hlines": 1},
    ],
}


def template_key(terminal: str) -> str:
    return "DPWORLD" if terminal in ("NSICT", "NSIGT") else terminal


# ---------------------------------------------------------------- column calibration
# Terminal + table specific column boxes, hand-calibrated from the REAL data-word x
# positions across the 25 PDFs (absolute page x; the layouts are template-generated so x
# is stable across a terminal's files). Each entry is an ordered list of (column_name,
# x_start); x_end is the next column's x_start (last → the panel band edge). These take
# priority over the generic header-anchor columns so wide values (vessel names) stay in
# their own column instead of bleeding into the berth column. Words left of the first
# box, or in no box, go to UNCAPTURED_DATA (no data loss). Only the vessel tables are
# calibrated (the identity columns that must be exact: berth, vessel, voyage, LOA,
# ETA, dates); other panels keep the generic coordinate fallback.
_ON = "Ops Commenced"
CALIBRATION: dict[str, dict[str, list[tuple[str, float]]]] = {
    "APMT": {
        "ON_BERTH_VESSEL": [
            ("Berth", 0), ("Vessel", 40), ("VIA", 110), ("LOA", 138),
            ("Alongside Date", 175), ("Alongside Time", 212), ("Side", 248),
            (f"{_ON} Date", 272), (f"{_ON} Time", 308),
            ("Ops Completed Date", 355), ("Ops Completed Time", 412),
            ("QC Boom Date", 468), ("QC Boom Time", 512),
            ("Imp", 545), ("Imp Bal", 580), ("Exp", 610), ("Exp Bal", 640),
            ("Arrival BFL Date", 666), ("Arrival BFL Time", 698), ("Max Draft", 726),
            ("ETC Date", 756), ("ETC Time", 788),
        ],
        "SAILED_VESSEL": [
            ("Berth", 0), ("Vessel", 40), ("VIA", 110), ("LOA", 138),
            ("Alongside Date", 175), ("Alongside Time", 212), ("Side", 248),
            (f"{_ON} Date", 272), (f"{_ON} Time", 308),
            ("Ops Completed Date", 355), ("Ops Completed Time", 412),
            ("QC Boom Date", 468), ("QC Boom Time", 512),
            ("Imp", 545), ("Imp Bal", 580), ("Exp", 610), ("Exp Bal", 640),
            ("Arrival BFL Date", 666), ("Arrival BFL Time", 698), ("Max Draft", 726),
            ("Sailing Date", 756), ("Sailing Time", 788),
        ],
        "VESSELS_EXPECTED": [
            ("VIA", 0), ("Vessel", 40), ("Draft", 110), ("LOA", 138),
            ("ETA Date", 175), ("ETA Time", 212),
            ("Arrival BFL Date", 248), ("Arrival BFL Time", 278),
            ("Gate Open Date", 300), ("Gate Open Time", 328),
            ("Reefer Opening Date", 352), ("Reefer Opening Time", 383),
            ("Reefer Cut-Off Date", 412), ("Reefer Cut-Off Time", 438),
            ("Cut-Off Date", 465), ("Cut-Off Time", 493), ("Service", 518), ("Line", 556),
        ],
    },
    "BMCT": {
        "VESSELS_ON_BERTHED": [
            ("Berth", 90), ("Vessel", 150), ("VIA", 250), ("LOA", 315),
            ("Berthing Date", 370), ("Berthing Time", 450), ("Side", 520),
            (f"{_ON} Date", 575), (f"{_ON} Time", 640), ("ETD Date", 780), ("ETD Time", 845),
            ("IMP", 885), ("IMP Bal", 918), ("EXP", 955), ("EXP Bal", 992), ("Max Draft", 1035),
        ],
        "SAILED_VESSEL": [
            ("Berth", 90), ("Vessel", 150), ("VIA", 250), ("LOA", 315),
            ("Berthing Date", 370), ("Berthing Time", 450), ("Side", 520),
            (f"{_ON} Date", 575), (f"{_ON} Time", 640),
            ("Ops Completed Date", 710), ("Ops Completed Time", 800),
            ("Sailing Date", 895), ("Sailing Time", 965), ("Max Draft", 1035),
        ],
        "VESSELS_EXPECTED": [
            ("VIA No.", 90), ("Vessel Name", 150), ("Service", 240), ("Line", 272),
            ("LOA", 305), ("Draft", 345), ("ETA Date", 380), ("ETA Time", 420),
            ("Cargo", 460), ("Gate Open Date", 560), ("Gate Open Time", 598),
            ("Reefer Opening Date", 628), ("Reefer Opening Time", 662),
            ("Reefer Cut-OFF Date", 700), ("Reefer Cut-OFF Time", 733),
            ("Cut-OFF Date", 800), ("Cut-OFF Time", 831),
        ],
    },
    "NSFT": {
        "VESSEL_SAILED_24H": [
            ("SR No", 0), ("Vessel Name", 60), ("Via No", 130), ("LOA", 168),
            ("Service", 200), ("Line", 238), ("Berthed Date", 268), ("Berthed Time", 292),
            (f"{_ON} Date", 312), (f"{_ON} Time", 337),
            ("Ops Completed Date", 355), ("Ops Completed Time", 378),
            ("Sailed Date", 398), ("Sailed Time", 420),
            ("Import Moves", 448), ("Export Moves", 490), ("Total Moves", 530),
        ],
        "VESSEL_AT_BERTH": [
            ("SR No", 0), ("Vessel Name", 60), ("Via No", 130), ("LOA", 168),
            ("Service", 200), ("Line", 238), ("Berthed Date", 268), ("Berthed Time", 292),
            (f"{_ON} Date", 312), (f"{_ON} Time", 337), ("ETC Date", 355), ("ETC Time", 378),
            ("Import Moves", 405), ("Import Balance", 450), ("Export Moves", 490),
            ("Export Balance", 530),
        ],
        "VESSELS_EXPECTED": [
            ("SR No", 0), ("Vessel Name", 60), ("VIA No", 130), ("LOA", 165),
            ("Service", 200), ("Line", 238), ("ETA Date", 268), ("ETA Time", 292),
            ("Gate Open Dry Date", 312), ("Gate Open Dry Time", 337),
            ("Gate Open Reefer Date", 355), ("Gate Open Reefer Time", 378),
            ("Gate Cut-off Dry Date", 398), ("Gate Cut-off Dry Time", 420),
            ("Gate Cut-off Reefer Date", 440), ("Gate Cut-off Reefer Time", 462),
            ("Import TEUs", 490), ("Export TEUs", 530),
        ],
    },
    # NSICT and NSIGT share the DP-World layout but are NOT pixel-identical — NSIGT's x
    # scale runs a few px left of NSICT's (growing rightward), so each gets its own boxes.
    "NSICT": {
        "VESSELS_ON_BERTH": [
            ("Berth", 30), ("Vessel Name", 80), ("VIA", 145), ("LOA", 175),
            ("Service", 215), ("Berth Side", 262), ("Import", 298), ("Export", 338),
            ("TTL MVS", 375), ("ATA Date", 495), ("ATA Time", 525),
            ("Ops Commence Date", 560), ("Ops Commence Time", 588),
            ("ETC Date", 628), ("ETC Time", 658), ("ETD", 700),
        ],
        "SAILED_VESSELS": [
            ("Berth", 30), ("Vessel Name", 80), ("VIA", 145), ("LOA", 175),
            ("Service", 215), ("Berth Side", 262), ("ATA Date", 300), ("ATA Time", 340),
            ("Ops Commence Date", 400), ("Ops Commence Time", 460),
            ("ATC Date", 540), ("ATC Time", 600), ("ATD Date", 660), ("ATD Time", 720),
        ],
        "VESSELS_EXPECTED": [
            ("SR No", 40), ("Vessel Name", 65), ("VIA", 180), ("LOA", 225),
            ("Service", 262), ("VOA", 300), ("IMP", 330), ("EXP", 358), ("TOTAL", 378),
            ("ETA Date", 400), ("ETA Time", 428),
            ("Gate Cutoff 1", 458), ("Gate Cutoff 2", 500), ("Gate Cutoff 3", 545),
        ],
    },
    "NSIGT": {
        "VESSELS_ON_BERTH": [
            ("Berth", 35), ("Vessel Name", 82), ("VIA", 142), ("LOA", 168),
            ("Service", 205), ("Berth Side", 248), ("Import", 285), ("Export", 320),
            ("TTL MVS", 350), ("ATA Date", 470), ("ATA Time", 502),
            ("Ops Commence Date", 535), ("Ops Commence Time", 563),
            ("ETC Date", 605), ("ETC Time", 636), ("ETD", 690),
        ],
        "SAILED_VESSELS": [
            ("Berth", 35), ("Vessel Name", 82), ("VIA", 142), ("LOA", 168),
            ("Service", 205), ("Berth Side", 248), ("ATA Date", 290), ("ATA Time", 330),
            ("Ops Commence Date", 390), ("Ops Commence Time", 450),
            ("ATC Date", 530), ("ATC Time", 590), ("ATD Date", 650), ("ATD Time", 710),
        ],
        "VESSELS_EXPECTED": [
            ("SR No", 44), ("Vessel Name", 68), ("VIA", 178), ("LOA", 218),
            ("Service", 250), ("VOA", 290), ("IMP", 318), ("EXP", 335), ("TOTAL", 356),
            ("ETA Date", 380), ("ETA Time", 410),
            ("Gate Cutoff 1", 438), ("Gate Cutoff 2", 480), ("Gate Cutoff 3", 540),
        ],
    },
}


# ---------------------------------------------------------------- report date
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_report_date(text: str) -> Optional[_dt.date]:
    """Report date from the PDF body. Prefers the explicit 'Date:' / 'DATE:' header line
    so trailing tide/vessel dates never win; falls back to the first date-like token."""
    head = "\n".join(text.splitlines()[:6])
    for scope in (
        "\n".join(l for l in text.splitlines()[:6] if re.search(r"date", l, re.I)),
        head, text,
    ):
        for rx, fn in (
            (r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", lambda m: _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
            (r"(\d{1,2})\.(\d{1,2})\.(\d{4})", lambda m: _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
            (r"(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](\d{2,4})", None),
        ):
            m = re.search(rx, scope)
            if not m:
                continue
            try:
                if fn:
                    return fn(m)
                mon = _MONTHS.get(m.group(2)[:3].lower())
                if mon:
                    yr = int(m.group(3)); yr += 2000 if yr < 100 else 0
                    return _dt.date(yr, mon, int(m.group(1)))
            except (ValueError, KeyError):
                continue
    return None


# ---------------------------------------------------------------- word lines
class _Word:
    __slots__ = ("text", "x0", "x1", "top", "used")

    def __init__(self, w: dict) -> None:
        self.text = w["text"]; self.x0 = float(w["x0"]); self.x1 = float(w["x1"])
        self.top = float(w["top"]); self.used = False


def _cluster_lines(words: list[dict], tol: float = 3.0) -> list[list[_Word]]:
    lines: list[dict[str, Any]] = []
    for raw in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        w = _Word(raw)
        for ln in lines:
            if abs(ln["top"] - w.top) <= tol:
                ln["ws"].append(w); break
        else:
            lines.append({"top": w.top, "ws": [w]})
    lines.sort(key=lambda l: l["top"])
    return [sorted(l["ws"], key=lambda w: w.x0) for l in lines]


def _line_text(ws: list[_Word]) -> str:
    return " ".join(w.text for w in ws)


def _in_band(w: _Word, lo: float, hi: float) -> bool:
    return lo - 2 <= w.x0 < hi


def _matches(ws: list[_Word], find: list[str], lo: float, hi: float) -> bool:
    toks = [w.text.upper() for w in ws if _in_band(w, lo, hi)]
    joined = " ".join(toks)
    return all(any(f.upper() == t for t in toks) or f.upper() in joined for f in find)


# ---------------------------------------------------------------- anchors
def _anchors(hlines: list[list[_Word]], lo: float, hi: float) -> list[tuple[float, str]]:
    """Column anchors (x, label) merged across header line(s) within the band."""
    cols: list[dict[str, Any]] = []
    for ws in hlines:
        for w in ws:
            if not _in_band(w, lo, hi):
                continue
            for c in cols:
                if abs(c["x"] - w.x0) <= 8:
                    c["parts"].append((w.top, w.text)); c["x"] = min(c["x"], w.x0); break
            else:
                cols.append({"x": w.x0, "parts": [(w.top, w.text)]})
    cols.sort(key=lambda c: c["x"])
    return [(c["x"], " ".join(t for _, t in sorted(c["parts"]))) for c in cols]


def _positional_anchors(rows: list[list[_Word]], lo: float, hi: float) -> list[tuple[float, str]]:
    """Discover columns by x-clustering all words in a headerless panel band."""
    xs = sorted({round(w.x0) for ws in rows for w in ws if _in_band(w, lo, hi)})
    if not xs:
        return []
    cols = [xs[0]]
    for x in xs[1:]:
        if x - cols[-1] > 14:            # column gap
            cols.append(x)
    return [(float(x), f"col_{i + 1}") for i, x in enumerate(cols)]


def _cols_with_ranges(anchors: list[tuple[float, str]], lo: float, hi: float) -> list[dict]:
    """Turn column anchors into ordered {name, x_start, x_end} boxes that TILE the band
    contiguously: column 0 owns from the band start (so a data value printed slightly left
    of its header is never lost), each column ends where the next begins, the last ends at
    the band edge. Duplicate header names are kept verbatim (positional rows don't need
    unique keys) — so 'Date Time Date Time' stays exactly that."""
    cols: list[dict] = []
    for i, (x, name) in enumerate(anchors):
        x_start = lo if i == 0 else x
        x_end = anchors[i + 1][0] if i + 1 < len(anchors) else hi
        cols.append({"name": (name or f"col_{i + 1}"), "x_start": round(x_start, 1),
                     "x_end": round(x_end, 1)})
    return cols


def _calibrated_cols(calib: list[tuple[str, float]], lo: float, hi: float) -> list[dict]:
    """Turn a calibration list [(name, x_start)] into {name, x_start, x_end} boxes clamped
    to the panel band; x_end = next column's x_start (last → band edge). Column 0 keeps its
    calibrated left edge (NOT extended to the band start) so a stray value left of it lands
    in UNCAPTURED_DATA rather than polluting the first column."""
    cols: list[dict] = []
    for i, (name, xs) in enumerate(calib):
        x_end = calib[i + 1][1] if i + 1 < len(calib) else hi
        xs_c, xe_c = max(float(xs), lo), min(float(x_end), hi)
        if xe_c <= xs_c:
            continue
        cols.append({"name": name, "x_start": round(xs_c, 1), "x_end": round(xe_c, 1)})
    return cols


def _row_values(ws: list[_Word], cols: list[dict]) -> tuple[list[str], list[str]]:
    """Positional row: one value per column (empty string preserved), by x-range. A word
    that falls in no column box goes to the uncaptured bucket (→ UNCAPTURED_DATA). Every
    consumed word is marked used for the document-level coverage guarantee."""
    buckets: list[list[str]] = [[] for _ in cols]
    uncaptured: list[str] = []
    for w in ws:
        placed = False
        for i, c in enumerate(cols):
            if c["x_start"] - 2 <= w.x0 < c["x_end"]:
                buckets[i].append(w.text); w.used = True; placed = True
                break
        if not placed:
            uncaptured.append(w.text); w.used = True
    return [" ".join(b) for b in buckets], uncaptured


# ---------------------------------------------------------------- extraction
def extract_tables(content: bytes, filename: str) -> dict[str, Any]:
    """Extract every panel from a berthing PDF. Raises ValueError only when the PDF is
    unreadable or the terminal cannot be detected."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("unreadable_pdf: PDF support unavailable (pdfplumber not installed)") from exc
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unreadable_pdf: {exc}") from exc
    with pdf:
        page = pdf.pages[0]
        width = float(page.width)
        page_count = len(pdf.pages)
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    det = PDF.detect_terminal(text)
    if det is None:
        raise ValueError("could_not_detect_terminal: PDF does not match a known JNPA terminal layout")
    terminal = det[0]
    report_date = parse_report_date(text)
    lines = _cluster_lines(words)
    panels = _TEMPLATES[template_key(terminal)]

    # 1) locate each panel: title line (top/heading) + find line (column header). Panels are
    # separated by their x-band, so two panels that share one header line (ICD/CFS pendency)
    # both match it — each reads only its own band. On-berth vs sailed share a band but are
    # disambiguated by `require` (ETC vs Sailing) and by searching below each title.
    located: list[dict[str, Any]] = []
    for spec in panels:
        lo, hi = spec["band"][0] * width, spec["band"][1] * width      # DATA band (extraction)
        fb = spec.get("find_band", spec["band"])
        flo, fhi = fb[0] * width, fb[1] * width                         # FIND band (locate header)
        start = 0
        title_top = None
        if spec.get("title"):
            for li, ws in enumerate(lines):
                if _matches(ws, spec["title"], flo, fhi):
                    title_top = ws[0].top; start = li; break
        anchor_li = None
        if spec.get("find"):
            for li in range(start, len(lines)):
                ws = lines[li]
                if not _matches(ws, spec["find"], flo, fhi):
                    continue
                if spec.get("require") and spec["require"].upper() not in _line_text(ws).upper():
                    continue
                anchor_li = li; break
        top = title_top if title_top is not None else (lines[anchor_li][0].top if anchor_li is not None else None)
        located.append({"spec": spec, "top": top, "anchor_li": anchor_li, "lo": lo, "hi": hi,
                        "headerless": spec.get("find") is None})

    tops = [l["top"] for l in located if l["top"] is not None]

    # 2) extract each panel; bound below by the next panel header in an overlapping band
    tables: list[dict[str, Any]] = []
    header_line_ids: set[int] = set()
    for loc in located:
        spec = loc["spec"]
        if loc["top"] is None:
            tables.append({"table_name": spec["name"], "columns": [], "rows": [],
                           "row_count": 0, "page_number": 1, "extraction_note": "section_not_found"})
            continue
        lo, hi = loc["lo"], loc["hi"]
        # vertical end = nearest other panel top below, in an overlapping band
        y_end = float("inf")
        for other in located:
            if other is loc or other["top"] is None:
                continue
            if other["top"] > loc["top"] + 1 and not (other["hi"] <= lo or other["lo"] >= hi):
                y_end = min(y_end, other["top"])

        # Headerless when the template has no find, OR the find header line was not located
        # (title present but column header absent) — fall back to positional, never crash.
        if loc["headerless"] or loc["anchor_li"] is None:
            data_start = loc["top"]
            body = [ws for ws in lines
                    if data_start - 1 <= ws[0].top < y_end and any(_in_band(w, lo, hi) for w in ws)]
            anchors = _positional_anchors(body, lo, hi)
            header_bottom = data_start - 1
        else:
            hl = spec.get("hlines", 1)
            hdr = lines[loc["anchor_li"]: loc["anchor_li"] + hl]
            for hli in range(loc["anchor_li"], loc["anchor_li"] + hl):
                header_line_ids.add(hli)
            anchors = _anchors(hdr, lo, hi)
            header_bottom = hdr[-1][0].top

        # Calibrated boxes take priority over the generic header-anchor columns (pixel-perfect
        # identity columns); fall back to generic coordinate columns when no calibration exists.
        calib = CALIBRATION.get(terminal, {}).get(spec["name"])
        cols = _calibrated_cols(calib, lo, hi) if calib else _cols_with_ranges(anchors, lo, hi)
        rows: list[dict[str, Any]] = []
        any_unc = False
        for ws in lines:
            if ws[0].top <= header_bottom or ws[0].top >= y_end:
                continue
            band_ws = [w for w in ws if _in_band(w, lo, hi)]
            if not band_ws:
                continue
            values, unc = _row_values(band_ws, cols)
            if any(v.strip() for v in values) or unc:
                rows.append({"values": values, "_unc": unc})
                if unc:
                    any_unc = True
        # No-data-loss: expose any per-row unmapped words as a trailing UNCAPTURED_DATA column.
        if any_unc:
            cols.append({"name": "UNCAPTURED_DATA", "x_start": round(lo, 1), "x_end": round(hi, 1)})
        for r in rows:
            unc = r.pop("_unc")
            if any_unc:
                r["values"].append(" ".join(unc))
        tables.append({"table_name": spec["name"], "columns": cols, "rows": rows,
                       "row_count": len(rows), "page_number": 1,
                       "extraction_note": None if rows else "empty"})

    # 3) word-level coverage → nothing dropped (document level)
    uncaptured: list[dict[str, Any]] = []
    for li, ws in enumerate(lines):
        if li in header_line_ids:
            continue
        leftover = [w.text for w in ws if not w.used]
        if leftover:
            uncaptured.append({"values": [" ".join(leftover)]})
    if uncaptured:
        tables.append({"table_name": "UNCAPTURED_TEXT",
                       "columns": [{"name": "_raw", "x_start": 0.0, "x_end": round(width, 1)}],
                       "rows": uncaptured, "row_count": len(uncaptured), "page_number": 1,
                       "extraction_note": "raw_fallback (not attributed to a template panel)"})

    named = [t for t in tables if t["table_name"] != "UNCAPTURED_TEXT"]
    return {
        "terminal": terminal,
        "report_date": report_date.isoformat() if report_date else None,
        "page_count": page_count,
        "file_name": filename,
        "tables": tables,
        "table_count": len(named),
        "total_rows": sum(t["row_count"] for t in tables),
        "missing_sections": [t["table_name"] for t in named
                             if t["extraction_note"] == "section_not_found"],
        "uncaptured_lines": len(uncaptured),
    }
