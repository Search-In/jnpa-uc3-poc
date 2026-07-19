"""Shared parsing helpers + the ``ParsedMessage`` envelope every parser returns.

Pure functions only. The date/number coercers are DELIBERATELY lenient: the
official customer files are the source of truth, so a malformed field yields
``None`` (recorded as-is) rather than raising — a single bad optional value must
never abort the import of an otherwise valid 2 700-container manifest. Structural
problems (not XML, missing payload root) DO raise :class:`CustomsParseError`.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Optional

# JNPA / ICEGATE timestamps carry no timezone; they are wall-clock IST. We stamp
# them Asia/Kolkata (UTC+5:30) so timestamptz columns store the correct instant —
# identical convention to scripts/import_cfs_ecy_codeco.py.
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


class CustomsParseError(Exception):
    """Raised only for STRUCTURAL failures (not valid XML/xlsx, missing payload
    root, unreadable file) — never for a single malformed field value."""


@dataclass
class ParsedMessage:
    """The uniform output of every customs parser.

    ``message`` holds the message-level envelope columns for jnpa.customs_messages
    (message_type, module, control_number, sender/receiver, sent_ts, primary_ref).
    ``payload`` holds the module-specific nested structure the repository persists
    (e.g. IGM: ``{"vessels": [...]}``); ``record_count`` is the number of leaf rows
    (the unit the import stats count). Parsers never touch the DB."""

    message: dict[str, Any]
    payload: dict[str, Any] = field(default_factory=dict)
    record_count: int = 0


# --------------------------------------------------------------------------- text
def clean(value: Optional[str]) -> Optional[str]:
    """Trim surrounding whitespace; collapse internal runs; ``None`` if empty.

    Customer XML pads many fields (e.g. ``'AAHCM8479K     '``) and wraps addresses
    across lines. We normalise whitespace so joins/uniqueness behave, but preserve
    the value's content exactly otherwise."""
    if value is None:
        return None
    s = " ".join(value.split())
    return s or None


# ------------------------------------------------------------------------- numbers
def to_int(value: Optional[str]) -> Optional[int]:
    s = clean(value)
    if not s:
        return None
    try:
        return int(float(s))  # tolerate '80', '80.0'
    except (ValueError, TypeError):
        return None


def to_num(value: Optional[str]) -> Optional[float]:
    s = clean(value)
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- dates
def parse_ddmmyyyy(value: Optional[str]) -> Optional[_dt.date]:
    """Parse the ICEGATE ``DDMMYYYY`` date (e.g. ``'22052026'`` -> 2026-05-22).

    Returns ``None`` for blank/malformed input or an impossible calendar date."""
    s = clean(value)
    if not s or not s.isdigit() or len(s) != 8:
        return None
    try:
        return _dt.date(int(s[4:8]), int(s[2:4]), int(s[0:2]))
    except ValueError:
        return None


def parse_ddmmyyyy_time(value: Optional[str]) -> Optional[_dt.datetime]:
    """Parse ``DDMMYYYY:HH:MM`` (e.g. ``'09062026:18:00'``) as an IST instant."""
    s = clean(value)
    if not s or ":" not in s:
        return None
    date_part, _, time_part = s.partition(":")
    d = parse_ddmmyyyy(date_part)
    if d is None:
        return None
    hh, mm = 0, 0
    bits = time_part.split(":")
    if len(bits) >= 1 and bits[0].isdigit():
        hh = int(bits[0])
    if len(bits) >= 2 and bits[1].isdigit():
        mm = int(bits[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return _dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


def parse_iso_date(value: Optional[str]) -> Optional[_dt.date]:
    """Parse an ISO ``YYYY-MM-DD`` date (RMS 'Processing End Date'). Lenient -> None."""
    s = clean(value)
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def coerce_cell_date(value: Any) -> Optional[_dt.date]:
    """Coerce an ``.xlsx`` cell into a date. openpyxl hands back a ``datetime`` for
    date-typed cells but a plain ``str`` for text cells, so LEO ('13/04/2026') and
    Shipping Bill (a real datetime) both flow through here. Accepts
    ``datetime``/``date`` objects and ``DD/MM/YYYY`` / ``YYYY-MM-DD`` strings."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = clean(str(value))
    if not s:
        return None
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                d, m, y = (int(p) for p in (parts[0], parts[1], parts[2][:4]))
                return _dt.date(y, m, d)
            except ValueError:
                return None
    return parse_iso_date(s)


def parse_sent(date_s: Optional[str], time_s: Optional[str]) -> Optional[_dt.datetime]:
    """Combine header ``SentDate`` (DDMMYYYY) + ``SentTime`` (HHMM) into an IST instant."""
    d = parse_ddmmyyyy(date_s)
    if d is None:
        return None
    t = clean(time_s) or "0000"
    t = t.zfill(4)
    try:
        hh, mm = int(t[0:2]), int(t[2:4])
    except ValueError:
        hh, mm = 0, 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        hh, mm = 0, 0
    return _dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# ----------------------------------------------------------------------- XML utils
def parse_document_header(header_el: Any) -> dict[str, Any]:
    """Extract the common EDI ``<DocumentHeader>`` envelope into message columns.

    ``header_el`` is an ``xml.etree.ElementTree.Element`` (or ``None``)."""
    def _t(tag: str) -> Optional[str]:
        if header_el is None:
            return None
        return clean(header_el.findtext(tag))

    return {
        "control_number": _t("ControlNumber"),
        "sender_id": _t("SenderId"),
        "receiver_id": _t("ReceiverId"),
        "message_id_code": _t("MessageId"),
        "sent_ts": parse_sent(_t("SentDate"), _t("SentTime")),
    }
