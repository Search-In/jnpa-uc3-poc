"""Details_of_Port_Crafts.pdf parser → core.port_craft. Pure, no DB.

The port-craft roster is a single PDF table (HIRED + OWNED sections) of tug/launch
particulars. Text is extracted with the SHARED berthing pdfplumber utility
(``services.berthing.pdf_parsers.extract_text_from_bytes``) — no new PDF dependency.

Extraction quirk: the multi-line "Main Engines" column wraps ABOVE its row, so the
engine fragment precedes each craft's serial number. Rows are segmented at each
"knots" (the trailing Bollard Pull / Designed Speed cell), then parsed with a tolerant
regex that forbids commas in the name/owner captures (engine fragments always contain
commas, real names never do — this is what keeps the engine text out of the name).

"NEVER DROP CLIENT DATA": every craft record keeps the full raw segment in
``extras['raw']``; a segment that cannot be split enough to yield a name (the required
key) is surfaced as a typed warning with its raw text rather than silently lost.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..upload_parsers import ParseResult

# One craft row. Name/owner forbid commas so engine fragments cannot leak in.
_ROW = re.compile(
    r"(\d{1,2})\s+"                                              # serial
    r"([^,]+?)\s+"                                               # name (+ trailing type)
    r"(Pilot Launch|VIP Launch|Security Launch|Utility Launch|Launch|Tug)\s+"  # type
    r"(Hired|Owned)\s+"                                          # owned/hired
    r"([^,]+?)\s+"                                               # owner
    r"((?:[A-Za-z]{3}\s*-?\s*\d{2})|\d{4})\s+"                   # year built
    r"([\d.]+)\s*M?\s+([\d.]+)\s*M?\s+([\d.]+)\s*M?\s+"          # loa / breadth / draft
    r"(.*)",                                                     # engines-mid + bollard/speed
    re.IGNORECASE,
)
_SECTION = re.compile(r".*(HIRED CRAFTS|Owned Craft|Speed)", re.IGNORECASE | re.DOTALL)


def _num(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    m = re.search(r"[-\d.]+", v)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def _clean(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", v).strip()
    return s or None


def parse_port_craft_pdf(content: bytes, filename: Optional[str] = None) -> ParseResult:
    res = ParseResult()
    try:
        from services.berthing.pdf_parsers import extract_text_from_bytes
        raw_text = extract_text_from_bytes(content)
    except ValueError as exc:
        res.rejected = True
        res.err(None, None, "unreadable_pdf", str(exc))
        return res
    except Exception as exc:  # noqa: BLE001
        res.rejected = True
        res.err(None, None, "pdf_parse_error", f"could not extract PDF text: {exc}")
        return res

    norm = re.sub(r"\s+", " ", raw_text).strip()
    segments = re.findall(r".+?knots", norm)
    res.row_count = len(segments)
    seen: set[str] = set()

    for i, seg in enumerate(segments, start=1):
        m = _ROW.search(seg)
        if not m:
            # Cannot split to a name (the required key) — surface raw, never drop.
            res.warn(i, None, "unparsed_craft_row",
                     f"could not parse craft particulars: {seg.strip()[:200]}")
            continue
        serial, name, ctype, oh, owner, year, loa, breadth, draft, rest = m.groups()
        name = _clean(name)
        if not name:
            res.warn(i, None, "missing_name", f"craft row with no name: {seg.strip()[:200]}")
            continue
        if name in seen:
            res.duplicate_count += 1
            continue
        seen.add(name)

        # Engines: the wrapped pre-fragment (before the serial, minus section markers)
        # + the mid-row fragment (rest, minus the trailing bollard/speed cell).
        pre = _SECTION.sub("", seg[:m.start()]).strip(" ,")
        mid = re.sub(r"[\d.]+\s*T?\s*/?\s*[\d.]+\s*knots.*$", "", rest).strip(" ,")
        engines = _clean(f"{pre} {mid}")
        bol = re.search(r"([\d.]+)\s*T\s*/", rest)
        spd = re.search(r"([\d.]+)\s*knots", rest)

        rec: dict[str, Any] = {
            "_target": "port_craft", "_message": "PORT_CRAFT", "_source_file": filename,
            "name": name,
            "craft_type": _clean(ctype),
            "owned_or_hired": _clean(oh),
            "owner_name": _clean(owner),
            "year_built": re.sub(r"\s*-\s*", "-", _clean(year) or ""),
            "loa_m": _num(loa),
            "breadth_m": _num(breadth),
            "draft_m": _num(draft),
            "main_engines": engines,
            "bollard_pull_t": _num(bol.group(1)) if bol else None,
            "design_speed_kn": _num(spd.group(1)) if spd else None,
            # NEVER drop client data: the full raw row is preserved verbatim.
            "extras": {"raw": _clean(seg), "serial": _clean(serial)},
        }
        res.records.append(rec)

    res.preview = [{
        "Name": r["name"], "Type": r.get("craft_type") or "—",
        "Owned/Hired": r.get("owned_or_hired") or "—",
        "Owner": r.get("owner_name") or "—", "Year": r.get("year_built") or "—",
        "LOA": r.get("loa_m"), "Bollard": r.get("bollard_pull_t"),
    } for r in res.records[:20]]
    return res
