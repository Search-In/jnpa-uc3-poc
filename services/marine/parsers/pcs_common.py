"""Shared helpers for the NLP-Marine PCS parsers — pure, no I/O, no DB.

Value coercion + the PCS datetime grammar seen across the real customer files:

  * ``DDMMYYYY:HH:MM``   — estimated times (CALINF/BERMAN EDTA/EDTD): '11022026:17:00'
  * ``DDMMYYYY HH:MM``   — actuals (VESARR/VESDEP): '29072026 05:18'
  * ``DDMMYYYYHHMMSS``   — IssuedDateTime (14 digits): '15072026173448'
  * ``DDMMYYYYHHMM``     — 12-digit variant
  * ``DDMMYYYY``         — date-only

All datetimes are returned tz-aware in IST, matching the rest of the marine module.
Kept separate from services.customs.parsers.common so the two modules stay
decoupled (same conventions, no cross-import).
"""
from __future__ import annotations

import math

import datetime as _dt
from typing import Any, Optional

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


class MarineParseError(Exception):
    """Raised by a message parser when a document cannot yield any record."""


def clean(value: Any) -> Optional[str]:
    """Trim to a non-empty string, or None. Also nulls common PCS sentinels."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() in ("NIL", "NA", "N/A", "NULL", "-"):
        return None
    return s


def to_num(value: Any) -> Optional[float]:
    """PCS numeric field -> float, or None when it carries no usable number.

    NON-FINITE VALUES ARE NOT NUMBERS. `float()` ACCEPTS 'NaN', 'inf', 'Infinity' and an
    overflowing literal like '1e400' — it does not raise — so a source document carrying
    any of those used to pass straight through, land in a PostgreSQL `numeric` column
    (which permits NaN), and then break JSON serialisation for the whole page at response
    time. One bad cell took out `GET /api/marine/vessels` entirely.

    Returning None keeps the existing contract exactly: an unparseable measurement was
    already None, and a non-finite one is no more of a measurement than 'abc' is. The
    same guard is stated in parsers/bathymetry_model._num.
    """
    s = clean(value)
    if s is None:
        return None
    try:
        f = float(s.replace(",", ""))
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def to_int(value: Any) -> Optional[int]:
    n = to_num(value)
    return int(n) if n is not None else None


def yn_bool(value: Any) -> Optional[bool]:
    """PCS Y/N flag → bool. Unknown/blank → None (tri-state, never a false 'N')."""
    s = clean(value)
    if s is None:
        return None
    u = s.upper()
    if u in ("Y", "YES", "TRUE", "1"):
        return True
    if u in ("N", "NO", "FALSE", "0"):
        return False
    return None


_DT_FORMATS = ("%d%m%Y:%H:%M", "%d%m%Y %H:%M", "%d%m%Y%H%M%S", "%d%m%Y%H%M", "%d%m%Y")


def parse_pcs_dt(value: Any) -> Optional[_dt.datetime]:
    """Parse any of the PCS datetime shapes to a tz-aware IST datetime, else None."""
    s = clean(value)
    if s is None:
        return None
    for fmt in _DT_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_pcs_date(value: Any) -> Optional[_dt.date]:
    """Parse a PCS/ISO date to a date, else None. Accepts DDMMYYYY and YYYY-MM-DD."""
    s = clean(value)
    if s is None:
        return None
    if s.isdigit() and len(s) == 8:
        try:
            return _dt.date(int(s[4:8]), int(s[2:4]), int(s[0:2]))
        except ValueError:
            return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def ft(el: Any, tag: str) -> Optional[str]:
    """First cleaned descendant text for ``tag`` under ``el`` (or None)."""
    if el is None:
        return None
    return clean(el.findtext(f".//{tag}"))


# --------------------------------------------------------------------------- call lifecycle
# Vocabulary for core.vessel_call.status, set by the message that ADVANCES the call.
# Migration 0038 [D8] leaves the column free-text on purpose, so these are conventions
# owned by the parser layer, not a DB constraint — a later message family (VESARR/VESDEP)
# adds its own terms without a migration.
CALL_STATUS_PLANNED = "Planned"                # CALINF: voyage registered, no VCN yet
CALL_STATUS_VCN_ALLOTTED = "VCN Allotted"      # CALINV: PCS allots the VCN (ALLOTMENTOFVCN)
CALL_STATUS_BERTH_PLANNED = "Berth Planned"    # BERMAN: berth application lodged against the VCN
CALL_STATUS_BERTH_ALLOTTED = "Berth Allotted"  # BERALT: a specific berth is assigned

# VCN terminal infix -> canonical core.ref_terminal.code.
# A full PCS VCN is IN + NSA1 + <terminal infix (2)> + 0 + <series><serial>, e.g.
# 'INNSA1BM0R3119' -> 'BM' -> BMCT. Corpus-verified over the BERMAN sample (NS/BM/ND/NF);
# GT and NG are included from the documented scheme. The map is deliberately CLOSED.
_VCN_TERMINAL_INFIX = {
    "NS": "NSICT", "NF": "NSFT", "GT": "APMT",
    "NG": "NSIGT", "BM": "BMCT", "ND": "NSDT",
}


def terminal_from_vcn(vcn: Any) -> Optional[str]:
    """Canonical terminal code carried in a full PCS VCN, or None.

    BERMAN carries no DockORTOCode tag — the terminal is encoded ONLY in the VCN infix —
    so this is the sole terminal source for a berth application. A short VIA form
    ('S0527'), a truncated value or an unrecognised infix all return None: an absent
    terminal is recoverable, a wrongly-guessed one is not.
    """
    s = clean(vcn)
    if s is None or len(s) < 8:
        return None
    return _VCN_TERMINAL_INFIX.get(s[6:8].upper())


def via_from_vcn(vcn: Any) -> Optional[str]:
    """Short VIA carried in the tail of a full PCS VCN, or None.

    A full VCN is IN + NSA1 + <terminal (2)> + 0 + <VIA>, so the VIA is everything from
    index 9: 'INNSA1ND0S6544' -> 'S6544'. Verified against the BERALT corpus, where the
    journal's own VIA_NO column equals this slice on 364 of 364 messages (zero
    mismatches, every VCN exactly 14 characters).

    Returns None for anything that is not a full VCN, so a short VIA passed in by mistake
    is never re-sliced into nonsense.
    """
    s = clean(vcn)
    if s is None or len(s) < 10:
        return None
    return s[9:].strip().upper() or None
