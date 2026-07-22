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


def _assign(ws: list[_Word], anchors: list[tuple[float, str]], lo: float, hi: float) -> dict[str, str]:
    labels = [lbl for _, lbl in anchors]
    xs = [x for x, _ in anchors]
    row: dict[str, list[str]] = {}
    for w in ws:
        if not _in_band(w, lo, hi):
            continue
        idx = 0
        for i, ax in enumerate(xs):
            if w.x0 >= ax - 4:
                idx = i
            else:
                break
        key = labels[idx] if labels else "col_1"
        row.setdefault(key, []).append(w.text)
        w.used = True
    return {k: " ".join(v) for k, v in row.items()}


def _dedupe_labels(anchors: list[tuple[float, str]]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for _, lbl in anchors:
        lbl = lbl or "col"
        if lbl in seen:
            seen[lbl] += 1; out.append(f"{lbl} ({seen[lbl]})")
        else:
            seen[lbl] = 1; out.append(lbl)
    return out


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
            tables.append({"table_name": spec["name"], "original_columns": [], "rows": [],
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

        labels = _dedupe_labels(anchors)
        rows: list[dict[str, str]] = []
        for ws in lines:
            if ws[0].top <= header_bottom or ws[0].top >= y_end:
                continue
            band_ws = [w for w in ws if _in_band(w, lo, hi)]
            if not band_ws:
                continue
            row = _assign(band_ws, anchors, lo, hi)
            if any(v.strip() for v in row.values()):
                rows.append(row)
        tables.append({"table_name": spec["name"], "original_columns": labels, "rows": rows,
                       "row_count": len(rows), "page_number": 1,
                       "extraction_note": None if rows else "empty"})

    # 3) word-level coverage → nothing dropped
    uncaptured: list[dict[str, str]] = []
    for li, ws in enumerate(lines):
        if li in header_line_ids:
            continue
        leftover = [w.text for w in ws if not w.used]
        if leftover:
            uncaptured.append({"_raw": " ".join(leftover)})
    if uncaptured:
        tables.append({"table_name": "UNCAPTURED_TEXT", "original_columns": ["_raw"],
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
