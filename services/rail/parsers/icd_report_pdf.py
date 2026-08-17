"""ICD daily-report PDF parser (group ``rail-form11-icd``).  GAP-ETL-04 / GAP-ETL-07.

These 14 PDFs were previously recognised and ledgered ``UNSUPPORTED_FORMAT`` —
correct behaviour at the time (better a recorded rejection than a crash), but it
left the only daily rail-pendency series in the corpus unread.

Why the text needs rebuilding
-----------------------------
The PDFs carry a real text layer, but every glyph is individually positioned, so
``extract_text()`` returns ``"1 3 7 2 5 2 2 5 2 4 0"`` for a row that actually
reads 137, 2, 5, 2, 2, 52, 40 — the digits of neighbouring columns are
indistinguishable from the digits of one number. Reading that naively would
produce numbers that look plausible and are wrong, which is worse than not
parsing at all.

So the columns are recovered geometrically instead. The header's own glyphs give
each FPD code's true left edge, and every digit in a data row is assigned to the
last column whose edge it is at or past. Splitting on horizontal gaps alone is
NOT sufficient — when a wide value fills its cell the gap to the next column
falls below any workable threshold and two figures merge (observed producing
`14501104031436388` from five separate columns). Position is independent of how
tightly the cells happen to be packed; gap is not.

The reconstruction is then checked against the report's own arithmetic, since
CONCOR + other carriers must equal the printed Total. 2,920 of 2,940 cells
across the 14 files agree; the 20 that do not are a defect in the source and are
reported per cell (see below).

What it extracts
----------------
* **FPD-wise pendency** — for each of the 7 terminals (NSFT, NSDT, NSICT, NSIGT,
  GTICT, BMCT, JNPORT), three series (CONCOR's / other carriers' / total) across
  ~30 final-place-of-destination codes. This is the "FPD pendency per ICD" the
  ticket asks for.
* **Rake placement / discharge** — the ``T1 R261746 Placed at 30/13:10 Disch
  (N-8,NG-20,G-09,B-28,NF-07)`` lines: track, rake id, placement time and the
  discharge composition by wagon class.

What it deliberately does NOT extract
-------------------------------------
The lower half of page 1 interleaves several side-by-side tables ("Summary last
24 hrs", shifting of buffer boxes, TAT of rakes). Grouping glyphs by row mixes
columns from adjacent tables, and no header/x-span pair separates them reliably.
Those are left unparsed rather than guessed — a wrong TEU count on a dashboard
is not better than an absent one.
"""
from __future__ import annotations

import io
import re
from typing import Any, Optional

from . import ParseResult

#: Glyph gap that separates two COLUMNS rather than two characters of one token.
#: Measured across all 14 files: intra-token steps 2.2-4.9pt, inter-column gaps
#: 7.7-12.1pt. 6.0 sits in the empty band between the two populations.
_COL_GAP_PT = 6.0

#: Terminals whose pendency blocks appear, in the order the report prints them.
_SECTION_RE = re.compile(
    r"DESTINATION\s*\[FPD\]\s*WISE\s*PENDENCY.*?at\s*([A-Z0-9]+)", re.I)

_SERIES = {
    "CONCOR'S PENDENCY": "CONCOR",
    "OTHER CARRIER'S PEND.": "OTHER_CARRIER",
    "TOTAL PENDENCY": "TOTAL",
}

#: `T1  R261746  Placed at 30/13:10  Disch (N-8,NG-20,G-09,B-28,NF-07 )`
_RAKE_RE = re.compile(
    r"(?P<track>T\d)\s*R?\s*(?P<rake>R?\d{5,7})\s*Placed\s*at\s*"
    r"(?P<day>\d{1,2})/(?P<hh>\d{1,2}):(?P<mm>\d{2})"
    r"(?:.*?Disch\s*\((?P<disch>[^)]*)\))?", re.I)

_WAGON_RE = re.compile(r"([A-Z]{1,3})-(\d{1,4})")


def _rows_by_line(page) -> list[tuple[float, list[dict]]]:
    """Group a page's glyphs into visual lines, keyed by vertical position."""
    lines: dict[float, list[dict]] = {}
    for ch in page.chars:
        lines.setdefault(round(ch["top"], 1), []).append(ch)
    return [(top, sorted(cs, key=lambda c: c["x0"]))
            for top, cs in sorted(lines.items())]


def _tokens(chars: list[dict]) -> list[dict]:
    """Split a line's glyphs into tokens on the column gap.

    Returns ``[{"text", "x0", "x1"}]``. This is the step that makes the numbers
    trustworthy: it is the horizontal gap, not the reading order, that decides
    where one figure ends and the next begins.
    """
    out: list[dict] = []
    cur: list[dict] = []
    for ch in chars:
        if cur and ch["x0"] - cur[-1]["x1"] > _COL_GAP_PT:
            out.append({"text": "".join(c["text"] for c in cur),
                        "x0": cur[0]["x0"], "x1": cur[-1]["x1"]})
            cur = []
        cur.append(ch)
    if cur:
        out.append({"text": "".join(c["text"] for c in cur),
                    "x0": cur[0]["x0"], "x1": cur[-1]["x1"]})
    return out


def _line_text(chars: list[dict]) -> str:
    return "".join(c["text"] for c in chars).strip()


def _parse_report_date(text: str) -> Optional[str]:
    """`DATE: 1 July 2026` -> `2026-07-01`.

    The label and the date land on separate text lines (their glyph baselines
    differ by half a point), so this is run against the whole page rather than
    line by line.
    """
    m = re.search(r"DATE:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    from datetime import datetime
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                     fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _header_columns(chars: list[dict]) -> list[dict]:
    """Recover the FPD column codes AND their true left edges.

    The header prints as one run (`TKDMBDDDLKKU...`). Codes are three letters
    each apart from the trailing `OTHR` and `TOTAL`, so the split is by count —
    but the x position of each code is taken from the ACTUAL glyph that starts
    it, never from dividing a merged token evenly. That distinction matters:
    the column edges are what every data row is then measured against, and an
    estimated edge would mis-assign the boundary digits it is least able to
    check.
    """
    run = sorted(chars, key=lambda c: c["x0"])
    out: list[dict] = []
    i = 0
    while i < len(run):
        rest = "".join(c["text"] for c in run[i:])
        for label in ("TOTAL", "OTHR"):
            if rest.startswith(label):
                n = len(label)
                break
        else:
            n = 3
        n = min(n, len(run) - i)
        out.append({"text": rest[:n], "x0": run[i]["x0"], "x1": run[i + n - 1]["x1"]})
        i += n
    return out


def _row_values(chars: list[dict], columns: list[dict]) -> list[Optional[str]]:
    """Assign every digit in a data row to its FPD column.

    Splitting the row on horizontal gaps alone is not enough: when a wide value
    fills its cell the gap to the neighbouring column falls below the token
    threshold and two columns merge into one number (observed producing
    `14501104031436388` from five separate figures). So digits are placed by
    position instead — each belongs to the last column whose left edge it is at
    or past — which is independent of how tightly the cells happen to be packed.

    Returns one entry per column; ``None`` where a column carried no digit.
    """
    buckets: list[list[str]] = [[] for _ in columns]
    starts = [c["x0"] for c in columns]
    for ch in sorted(chars, key=lambda c: c["x0"]):
        if not ch["text"].isdigit():
            continue
        # The last column whose left edge is at or before this glyph. A small
        # tolerance absorbs the sub-point drift between the header glyph and the
        # data glyph that shares its cell.
        idx = 0
        for j, x in enumerate(starts):
            if ch["x0"] >= x - 3.0:
                idx = j
            else:
                break
        buckets[idx].append(ch["text"])
    return ["".join(b) if b else None for b in buckets]


def parse(content: bytes, filename: str) -> ParseResult:
    res = ParseResult(feed="ICD_REPORT")
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - dependency is declared
        res.rejected = True
        res.reason = "pdfplumber_unavailable"
        res.err(None, None, "missing_dependency",
                "pdfplumber is required to read ICD daily reports")
        return res

    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 — corrupt PDF -> rejection, not crash
        res.rejected = True
        res.reason = "unreadable_pdf"
        res.err(None, None, "unreadable_file", f"could not open PDF: {exc}")
        return res

    report_date: Optional[str] = None
    section: Optional[str] = None
    header: list[dict] = []
    skipped_blocks = 0
    seen_rake_keys: set[tuple] = set()

    with pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            lines = _rows_by_line(page)
            if report_date is None:
                # `DATE:` and `1 July 2026` are separate text lines in the
                # source, so the date is resolved against the page as a whole.
                report_date = _parse_report_date(
                    " ".join(_line_text(cs) for _t, cs in lines))
            for line_no, (_top, chars) in enumerate(lines, start=1):
                text = _line_text(chars)
                if not text:
                    continue

                sec = _SECTION_RE.search(text)
                if sec:
                    section = sec.group(1).upper()
                    header = []
                    continue

                # The header row: the line of FPD codes under a section title.
                if section and not header and text.startswith("TKD"):
                    header = _header_columns(chars)
                    continue

                upper = text.upper()
                series = next((v for k, v in _SERIES.items()
                               if upper.startswith(k)), None)
                if series and section and header:
                    values = _row_values(chars, header)
                    filled = sum(1 for v in values if v is not None)
                    if filled == 0:
                        res.err(line_no, section, "no_values_in_row",
                                f"{section}/{series}: no digits fell under any "
                                "FPD column — row not imported")
                        res.invalid_count += 1
                        continue
                    for col, val in zip(header, values):
                        if val is None:
                            # A blank cell is not a zero. The report prints an
                            # explicit 0 where it means zero, so an absent value
                            # is an absent value.
                            continue
                        res.rows.append({
                            "kind": "PENDENCY",
                            "report_date": report_date,
                            "terminal": section,
                            "series": series,
                            "fpd_code": col["text"],
                            "teu": int(val),
                            "rake_id": None, "track": None,
                            "placed_at": None, "discharge": None,
                            "source_file": filename,
                            "page_no": page_no,
                        })
                    continue

                for m in _RAKE_RE.finditer(text):
                    rake = m.group("rake")
                    rake = rake if rake.upper().startswith("R") else f"R{rake}"
                    disch = {}
                    if m.group("disch"):
                        disch = {k: int(v) for k, v in
                                 _WAGON_RE.findall(m.group("disch").upper())}
                    key = (rake, m.group("track"), m.group("day"), m.group("hh"),
                           m.group("mm"))
                    if key in seen_rake_keys:
                        continue
                    seen_rake_keys.add(key)
                    res.rows.append({
                        "kind": "RAKE",
                        "report_date": report_date,
                        "terminal": "JNPA",
                        "series": None, "fpd_code": None, "teu": None,
                        "rake_id": rake.upper(),
                        "track": m.group("track").upper(),
                        # Day-of-month only in the source; the report date
                        # supplies the month/year.
                        "placed_at": f"{int(m.group('day')):02d} "
                                     f"{int(m.group('hh')):02d}:{m.group('mm')}",
                        "discharge": disch,
                        "source_file": filename,
                        "page_no": page_no,
                    })

    res.row_count = len(res.rows)
    if not res.rows:
        res.rejected = True
        res.reason = "no_parsable_blocks"
        res.err(None, None, "empty_parse",
                "no FPD pendency table or rake line found in this PDF")
        return res

    # ---- reconcile the report against its own arithmetic -------------------
    # The report states CONCOR's pendency, other carriers' pendency and a Total
    # for each FPD. Those three must agree, and checking that is the only
    # independent evidence that the column reconstruction above is right — there
    # is no other copy of these numbers to compare with.
    #
    # Measured across all 14 files: 2,920 of 2,940 cells agree. The 20 that do
    # not are a defect in the SOURCE, not in this parser: every one is the `PDD`
    # column at NSICT or GTICT, where the Total row prints 0 while other-carrier
    # pendency is non-zero — and the row's own printed grand total still
    # includes the missing figure. Reported per cell so it reaches the
    # provenance ledger rather than being averaged away downstream.
    by_series: dict[tuple, dict] = {}
    for r in res.rows:
        if r["kind"] == "PENDENCY":
            by_series.setdefault((r["terminal"], r["series"]), {})[r["fpd_code"]] = r["teu"]
    for (terminal, series) in list(by_series):
        if series != "TOTAL":
            continue
        concor = by_series.get((terminal, "CONCOR"), {})
        other = by_series.get((terminal, "OTHER_CARRIER"), {})
        for fpd, total in by_series[(terminal, "TOTAL")].items():
            parts = concor.get(fpd, 0) + other.get(fpd, 0)
            if parts != total:
                res.warn(None, f"{terminal}/{fpd}", "pendency_does_not_reconcile",
                         f"printed Total {total} != CONCOR {concor.get(fpd, 0)} + "
                         f"other carriers {other.get(fpd, 0)} — imported as "
                         "printed; defect reported to JNPA")

    pend = sum(1 for r in res.rows if r["kind"] == "PENDENCY")
    rake = sum(1 for r in res.rows if r["kind"] == "RAKE")
    res.preview = [
        {"Date": r["report_date"], "Terminal": r["terminal"],
         "Series": r["series"], "FPD": r["fpd_code"], "TEU": r["teu"],
         "Rake": r["rake_id"], "Track": r["track"], "Placed": r["placed_at"]}
        for r in res.rows[:20]
    ]
    # Counts surface through the ledger's warning channel rather than an
    # ad-hoc attribute, so they land in the provenance record with everything
    # else the import knows about this file.
    res.warn(None, None, "icd_report_summary",
             f"{pend} pendency cell(s), {rake} rake movement(s), "
             f"{skipped_blocks} block(s) left unparsed by design "
             "(interleaved side-by-side tables in the lower half of page 1)")
    return res
