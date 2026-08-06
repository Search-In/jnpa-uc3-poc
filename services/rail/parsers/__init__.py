"""Pure rail-file parsers — bytes in, canonical rows + errors out, no DB.

Mirrors :mod:`services.cfs_ecy.upload_parsers` in spirit: a parser is a pure
``(content: bytes, filename: str) -> ParseResult`` function. The service layer
(:mod:`services.rail.fois_service` / :mod:`services.rail.form11_icd_service`)
hands ``ParseResult.rows`` to :class:`services.rail.repository.RailRepository`
for idempotent persistence and never re-implements any parsing itself.

Two parsers live here:
  * :func:`fois_csv.parse` — the NLDS/FOIS Train Intimation CSV.
  * :func:`form11_icd.parse` — Form 11 XLSX + CTO TXT (dispatch by extension /
    content); ICD daily-report PDFs are recognised and returned as
    ``feed='UNSUPPORTED'`` so the service can reject-not-crash.

Both emit a shared :class:`ParseResult` carrying a ``feed`` tag
(FOIS | FORM11 | CTO | UNSUPPORTED) that the service uses to pick the target
table.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Optional

# JNPA operates in IST. The rail feeds carry naive local timestamps; we stamp
# them Asia/Kolkata (UTC+5:30) so the timestamptz columns store the correct
# instant — identical to scripts/import_cfs_ecy_codeco.py.
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

FEEDS = ("FOIS", "FORM11", "CTO")


# --------------------------------------------------------------- ParseResult
class ParseResult:
    """The parse envelope shared by every rail parser."""

    def __init__(self, feed: Optional[str] = None) -> None:
        self.feed = feed                          # FOIS | FORM11 | CTO | UNSUPPORTED
        self.rows: list[dict[str, Any]] = []      # valid, mapped canonical rows
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.preview: list[dict[str, Any]] = []
        self.row_count = 0
        self.invalid_count = 0
        self.duplicate_count = 0
        self.rejected = False                      # structural failure (bad file)
        self.unsupported = False                   # a format we deliberately skip
        self.reason: Optional[str] = None          # machine reason for reject/unsupported

    def err(self, row: Optional[int], col: Optional[str], code: str,
            detail: str, raw: Any = None) -> None:
        self.errors.append({"row_number": row, "column_name": col,
                            "error_code": code, "error_detail": detail,
                            "raw_value": (None if raw is None else str(raw))})

    def warn(self, row: Optional[int], col: Optional[str], code: str,
             detail: str) -> None:
        self.warnings.append({"row_number": row, "column_name": col,
                             "error_code": code, "error_detail": detail})


# --------------------------------------------------------------- scalar helpers
def norm_header(name: Any) -> str:
    """Collapse a header to letters+digits only, lowercase, so
    'RakeId' / 'Rake Id' / 'RAKE_ID' all map to one key."""
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


def clean(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    v = str(raw).strip()
    return v or None


def upper_or_none(raw: Any) -> Optional[str]:
    v = clean(raw)
    return v.upper() if v else None


def to_int(raw: Any) -> Optional[int]:
    v = clean(raw)
    if v is None:
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def to_float(raw: Any) -> Optional[float]:
    v = clean(raw)
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# The rail feeds mix several naive-local date/time spellings.
_TS_FORMATS = (
    "%d%m%Y:%H:%M",       # FOIS: 22072026:23:52
    "%d%m%Y:%H:%M:%S",
    "%d-%m-%Y %H:%M",     # CTO variant A: 25-05-2026 11:00
    "%d-%m-%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",     # CTO variant B: 22.06.2026 12:00
    "%d.%m.%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%d%m%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
)


def parse_ts(raw: Any) -> Optional[_dt.datetime]:
    """Parse one of the rail date/time spellings to an IST-aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, _dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=IST)
    if isinstance(raw, _dt.date):
        return _dt.datetime(raw.year, raw.month, raw.day, tzinfo=IST)
    s = clean(raw)
    if not s:
        return None
    for fmt in _TS_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except ValueError:
        return None


def parse_date_time(date_raw: Any, time_raw: Any) -> Optional[_dt.datetime]:
    """Combine a separate date + time cell (the CTO layouts) into one IST
    datetime. Falls back to date-only when the time is missing/unparseable."""
    d = clean(date_raw)
    t = clean(time_raw)
    if d is None:
        return None
    if t:
        combined = parse_ts(f"{d} {t}")
        if combined is not None:
            return combined
    return parse_ts(d)


__all__ = [
    "ParseResult", "IST", "FEEDS", "norm_header", "clean", "upper_or_none",
    "to_int", "to_float", "parse_ts", "parse_date_time",
]
