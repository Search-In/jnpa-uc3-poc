"""Shared parsing helpers + the ``ParsedList`` envelope every shipping-line parser
returns.

Pure functions only. Mirrors :mod:`services.customs.parsers.common`: the coercers
are DELIBERATELY lenient — the official customer files are the source of truth, so a
malformed optional field yields ``None`` (recorded as-is in ``raw``) rather than
raising. Structural problems (unreadable workbook, missing sheet, not XML) DO raise
:class:`ShippingLineParseError`.

The dataset is heterogeneous (per-terminal column names, KG vs MT weights, a
record-labelled flat-file EDI shape, and CODECO-XML embedded in a cell), so the
mapping from source headers to the canonical container dict is alias-driven
(:data:`ALIASES`) rather than one bespoke parser per file.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from jnpa_shared.iso6346 import is_valid_container_no

# JNPA timestamps carry no timezone; they are wall-clock IST. Identical convention
# to services/customs/parsers/common.py and scripts/import_cfs_ecy_codeco.py.
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


class ShippingLineParseError(Exception):
    """Raised only for STRUCTURAL failures (unreadable file, missing sheet, not XML)
    — never for a single malformed field value."""


@dataclass
class ParsedList:
    """The uniform output of every shipping-line parser.

    ``header`` holds list-level envelope columns for core.sl_import_file
    (list_type, terminal, physical_format, vessel_visit, voyage, line_code,
    direction). ``containers`` holds canonical IAL/EAL line-item dicts;
    ``delivery_orders`` holds canonical EDO/CODECO dicts. ``record_count`` is the
    number of leaf rows (the unit the import stats count). Parsers never touch the DB."""

    header: dict[str, Any]
    containers: list[dict[str, Any]] = field(default_factory=list)
    delivery_orders: list[dict[str, Any]] = field(default_factory=list)
    record_count: int = 0


# --------------------------------------------------------------------------- text
def clean(value: Any) -> Optional[str]:
    """Trim + collapse internal whitespace; ``None`` if empty. Also treats the common
    ``NIL`` / ``NA`` sentinels the terminals use for "no value" as empty."""
    if value is None:
        return None
    s = " ".join(str(value).split())
    if not s or s.upper() in ("NIL", "NA", "N/A", "NULL", "-"):
        return None
    return s


def norm_header(name: Any) -> str:
    """Normalise a source header for alias matching: lowercase, drop everything that
    is not a letter or digit (spaces, underscores, parentheses, hyphens)."""
    if name is None:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


# ------------------------------------------------------------------------- numbers
def to_num(value: Any) -> Optional[float]:
    s = clean(value)
    if not s:
        return None
    s = s.replace(",", "")
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    # Reject NaN / ±Inf: 'nan' and 'inf' parse via float() but are not usable weights.
    import math
    return f if math.isfinite(f) else None


# ------------------------------------------------------------------- container / iso
def norm_container(value: Any) -> Optional[str]:
    """Uppercase, strip non-alphanumerics (some feeds pad/space the number)."""
    s = clean(value)
    if not s:
        return None
    return re.sub(r"[^A-Z0-9]", "", s.upper()) or None


def container_valid(container_no: Optional[str]) -> bool:
    return bool(container_no) and is_valid_container_no(container_no)


# ------------------------------------------------------------------- enum coercion
def norm_freight_kind(value: Any) -> str:
    """Map a Status / FreightKind / LoadStatus value to FULL / EMPTY / UNKNOWN.

    Note: ``E`` in a freight-kind column means EMPTY (MTY); ``E`` in a *category*
    column means EXPORT — the two are mapped from different source columns."""
    s = clean(value)
    if not s:
        return "UNKNOWN"
    u = s.upper()
    if u in ("F", "FCL", "FULL"):
        return "FULL"
    if u in ("E", "MT", "MTY", "EMPTY"):
        return "EMPTY"
    return "UNKNOWN"


def norm_category(value: Any) -> Optional[str]:
    """Map a Category / ShippingStatusCode value to IMPORT / EXPORT / TRANSHIP / None."""
    s = clean(value)
    if not s:
        return None
    u = s.upper()
    if u in ("I", "IM", "IMP", "IMPORT"):
        return "IMPORT"
    if u in ("E", "EX", "EXP", "EXPORT"):
        return "EXPORT"
    if u in ("T", "TP", "TRANS", "TRANSHIP", "TRANSHIPMENT"):
        return "TRANSHIP"
    return "OTHER"


# --------------------------------------------------------------------------- dates
def parse_ddmmyyyy(value: Any) -> Optional[_dt.date]:
    """Parse an ICEGATE/CODECO ``DDMMYYYY`` date (e.g. ``'12062026'`` -> 2026-06-12)."""
    s = clean(value)
    if not s or not s.isdigit() or len(s) != 8:
        return None
    try:
        return _dt.date(int(s[4:8]), int(s[2:4]), int(s[0:2]))
    except ValueError:
        return None


def parse_ddmmyyyy_time(value: Any) -> Optional[_dt.datetime]:
    """Parse ``DDMMYYYY:HH:MM`` (e.g. ``'12062026:02:53'``) as an IST instant."""
    s = clean(value)
    if not s or ":" not in s:
        return None
    date_part, _, time_part = s.partition(":")
    d = parse_ddmmyyyy(date_part)
    if d is None:
        return None
    bits = time_part.split(":")
    hh = int(bits[0]) if bits and bits[0].isdigit() else 0
    mm = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        hh, mm = 0, 0
    return _dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


def parse_ddmmyyyyhhmmss(value: Any) -> Optional[_dt.datetime]:
    """Parse a CODECO ``DDMMYYYYHHMMSS`` stamp (e.g. ``'12062026111837'``) as IST."""
    s = clean(value)
    if not s or not s.isdigit() or len(s) != 14:
        return None
    try:
        return _dt.datetime(int(s[4:8]), int(s[2:4]), int(s[0:2]),
                            int(s[8:10]), int(s[10:12]), int(s[12:14]), tzinfo=IST)
    except ValueError:
        return None


def norm_intish(value: Any) -> Optional[str]:
    """Return a code-like string with any Excel ``.0`` float suffix stripped
    (``2210.0`` -> ``'2210'``). Non-numeric strings pass through unchanged."""
    s = clean(value)
    if s is None:
        return None
    try:
        f = float(s)
    except (ValueError, TypeError):
        return s
    return str(int(f)) if f.is_integer() else s


# ------------------------------------------------------------------------- weights
# The dataset uses a single primary gross-weight column per file, but its name AND
# unit are unreliable: e.g. BMCT labels a KG value 'GrossWeightInMT' (19880) while
# APMT's 'GrossWeightInMT' is genuine MT (20.95). A laden container's gross is
# ~2-40 tonnes, so we infer the unit by MAGNITUDE (robust to the mislabelled
# columns): a value below the threshold is tonnes, above it is kilograms.
_WEIGHT_HEADERS: tuple[str, ...] = (
    "grossweightinkgs", "grosswgt", "vgm", "grossweightinmt", "vgmweightinmt",
    "grossweight", "weight",
)
_MT_KG_THRESHOLD = 200.0  # no container exceeds ~40 MT; no laden box is under ~200 KG


def resolve_weight(present_headers: dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
    """Pick the gross-weight column and return ``(kg, inferred_uom)``. The unit is
    inferred from the value's magnitude (tonnes vs kilograms), NOT the column name,
    because several terminals mislabel a KG column as ``...InMT``."""
    for norm in _WEIGHT_HEADERS:
        if norm in present_headers:
            val = to_num(present_headers[norm])
            if val is None or val <= 0:
                continue
            if val < _MT_KG_THRESHOLD:
                return round(val * 1000.0, 2), "MT"
            return round(val, 2), "KG"
    return None, None
