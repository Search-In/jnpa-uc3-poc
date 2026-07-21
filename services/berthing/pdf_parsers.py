"""Berthing Reports PDF parsers (UC-III module 7) — per-terminal, real-file driven.

The five JNPA container terminals publish DAILY berthing reports as single-page PDFs
with FIVE DIFFERENT layouts (there is no CSV/XLS/XLSX source). This module turns each
PDF into the common normalised vessel-call model consumed by
services.berthing.repository.BerthingRepository.persist — the SAME model the
interactive CSV/XLS/XLSX Data-Upload sub-module produces.

Layouts (verified against Digital Twin/Data/7-Berthing Reports):

* APMT / BMCT      — berth-anchored rows (APM01.. / BMCT01..); "on-berth" + "sailed"
                     sections. Vessel name precedes the VIA/rotation code (S0xxx).
                     Dates are "dd-Mon HH:MM" (no year → taken from the report date).
                     The forward-looking "Expected" section is VIA-FIRST with service/
                     line codes interleaved, so its vessel-name boundary is ambiguous —
                     those rows are intentionally SKIPPED to keep names clean.
* NSFT             — serial-anchored rows; vessel precedes VIA; full "dd-mm-yyyy HH:MM"
                     datetimes; all three sections (sailed / at-berth / expected) parse.
* NSICT / NSIGT    — berth-anchored (CB0x) on-berth + sailed; serial-anchored Expected.
                     Vessel precedes VIA everywhere; "dd/mm/yyyy HH:MM" datetimes;
                     Expected ETA is "Ddd/dd/mm HH:MM".

Required fields per the corpus: terminal, vessel_name, voyage_number. imo_number is
absent from every file; berth_number is absent for NSFT; eta only appears in Expected.
All of those stay nullable. A row is dropped only if it has no vessel name or no VIA.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Optional

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# folder name -> (canonical terminal, anchor kind)
TERMINALS: dict[str, tuple[str, str]] = {
    "APM Terminals":  ("APMT", "APM"),
    "BMCT_PSA":       ("BMCT", "BMCT"),
    "NSFT":           ("NSFT", "NSFT"),
    "NSICT_DP World": ("NSICT", "CB"),
    "NSIGT_DP World": ("NSIGT", "CB"),
}

_ANCHOR = {
    "APM":  re.compile(r"^(APM\d{2})\b"),
    "BMCT": re.compile(r"^(BMCT\d{2})\b"),
    "CB":   re.compile(r"^(CB\d{2})\b"),
    "SERIAL": re.compile(r"^(\d{1,2})\s+(.+)$"),
}
# VIA / rotation code, optionally glued to a 0-4 letter prefix (DP World "AGMS0655").
_VIA = re.compile(r"\b([A-Z]{0,4})(S0\d{3,4})\b")
_DT_FULL = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s+(\d{1,2}):(\d{2})\b")
_DT_DAYMON = re.compile(r"\b(\d{1,2})-([A-Za-z]{3})\s+(\d{1,2}):(\d{2})\b")
_ETA_DP = re.compile(r"[A-Za-z]{3}/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
    start=1)}

# lifecycle ordering used for status advancement + the reporting sections
STATUS_ORDER = {"EXPECTED": 0, "ARRIVED": 1, "BERTH_ASSIGNED": 2, "BERTHING_STARTED": 3,
                "CARGO_OPERATION": 4, "COMPLETED": 5, "DEPARTED": 6}


def report_year(text: str, filename: str) -> int:
    m = re.search(r"20\d{2}", filename) or re.search(r"20\d{2}", text)
    if m:
        return int(m.group(0))
    m = re.search(r"-(\d{2})\b", filename)          # e.g. '...-26.pdf'
    return 2000 + int(m.group(1)) if m else _dt.date.today().year


def _section_status(line: str) -> Optional[str]:
    """Map a section-header line to its raw section bucket, or None."""
    u = line.upper()
    if "EXPECTED" in u:
        return "EXPECTED"
    if "SAIL" in u or "SAILING TIME" in u:               # SAILED VESSEL(S) / Vessel Sailed
        return "DEPARTED"
    if "ON BERTH" in u or "AT BERTH" in u or "ON BERTHED" in u or "VESSELS ON" in u:
        return "BERTHED"
    return None


def _mk(year: int, day: int, month: int, hh: int, mm: int) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime(year, month, day, hh, mm, tzinfo=IST)
    except ValueError:
        return None


def _full_dts(line: str) -> list[_dt.datetime]:
    out = []
    for d, m, y, hh, mm in _DT_FULL.findall(line):
        v = _mk(int(y), int(d), int(m), int(hh), int(mm))
        if v:
            out.append(v)
    return out


def _daymon_dts(line: str, year: int) -> list[_dt.datetime]:
    out = []
    for d, mon, hh, mm in _DT_DAYMON.findall(line):
        mi = _MONTHS.get(mon.lower())
        if mi:
            v = _mk(year, int(d), mi, int(hh), int(mm))
            if v:
                out.append(v)
    return out


def _times(kind: str, section: str, line: str, year: int) -> dict[str, _dt.datetime]:
    """Best-effort per-terminal timestamp extraction, mapped positionally by section."""
    out: dict[str, _dt.datetime] = {}
    if kind == "CB":
        if section == "EXPECTED":
            m = _ETA_DP.search(line)
            if m:
                v = _mk(year, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
                if v:
                    out["eta"] = v
        else:
            ds = _full_dts(line)
            keys = (["ata", "cargo_operation_start", "cargo_operation_end", "departure_time"]
                    if section == "DEPARTED"
                    else ["ata", "cargo_operation_start", "cargo_operation_end"])
            for k, v in zip(keys, ds):
                out[k] = v
    elif kind == "NSFT":
        ds = _full_dts(line)
        if section == "EXPECTED":
            if ds:
                out["eta"] = ds[0]
        else:
            keys = (["ata", "cargo_operation_start", "cargo_operation_end", "departure_time"]
                    if section == "DEPARTED"
                    else ["ata", "cargo_operation_start", "cargo_operation_end"])
            for k, v in zip(keys, ds):
                out[k] = v
    else:  # APM / BMCT — dd-Mon HH:MM pairs (no year)
        ds = _daymon_dts(line, year)
        if ds:
            out["ata"] = ds[0]
            if len(ds) > 1:
                out["cargo_operation_start"] = ds[1]
            if section == "DEPARTED":
                out["departure_time"] = ds[-1]
    if out.get("ata") and "berthing_time" not in out:
        out["berthing_time"] = out["ata"]                # alongside == berthing instant
    return out


def _lifecycle_status(section: str, times: dict) -> str:
    if section == "EXPECTED":
        return "ARRIVED" if times.get("ata") else "EXPECTED"
    if section == "DEPARTED":
        return "DEPARTED"
    # on-berth
    return "CARGO_OPERATION" if times.get("cargo_operation_start") else "BERTH_ASSIGNED"


def parse_text(text: str, terminal: str, kind: str, *, filename: str,
               source_file: Optional[str] = None) -> list[dict[str, Any]]:
    """Parse one report's extracted text into normalised vessel-call records."""
    year = report_year(text, filename)
    records: list[dict[str, Any]] = []
    section: Optional[str] = None
    for raw_line in text.splitlines():
        sec = _section_status(raw_line)
        if sec:
            section = sec
            continue
        if section is None:
            continue
        stripped = raw_line.strip()
        berth: Optional[str] = None
        rest: Optional[str] = None
        # 1) berth anchor (on-berth / sailed) — vessel precedes VIA (clean)
        if kind in ("APM", "BMCT", "CB"):
            a = _ANCHOR[kind].match(stripped)
            if a:
                berth = a.group(1)
                rest = stripped[a.end():].strip()
        # 2) serial anchor — NSFT (all sections) + DP World EXPECTED — vessel precedes VIA
        if rest is None and (kind == "NSFT" or (kind == "CB" and section == "EXPECTED")):
            a = _ANCHOR["SERIAL"].match(stripped)
            if a:
                rest = a.group(2)
        if rest is None:
            continue
        vm = _VIA.search(rest)
        if not vm:                                        # no rotation code → not a vessel row
            continue
        vessel = rest[:vm.start()].strip(" .")
        if not vessel or len(vessel) < 2:
            continue
        voyage = vm.group(2)
        times = _times(kind, section, raw_line, year)
        status = _lifecycle_status(section, times)
        rec = {
            "terminal": terminal,
            "vessel_name": vessel,
            "imo_number": None,
            "voyage_number": voyage,
            "shipping_line": None,
            "berth_number": berth,
            "eta": times.get("eta"),
            "ata": times.get("ata"),
            "berthing_time": times.get("berthing_time"),
            "departure_time": times.get("departure_time"),
            "cargo_operation_start": times.get("cargo_operation_start"),
            "cargo_operation_end": times.get("cargo_operation_end"),
            "status": status,
            "source_file": source_file or filename,
        }
        records.append(rec)
    return records


def parse_pdf_bytes(content: bytes, terminal: str, kind: str, *, filename: str,
                    source_file: Optional[str] = None) -> list[dict[str, Any]]:
    """Extract page text with pdfplumber and parse it. Raises ValueError if unreadable."""
    import io

    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unreadable_pdf: {exc}") from exc
    if not text.strip():
        raise ValueError("empty_pdf")
    return parse_text(text, terminal, kind, filename=filename, source_file=source_file)
