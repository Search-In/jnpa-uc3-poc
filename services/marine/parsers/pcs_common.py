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
    s = clean(value)
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


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
