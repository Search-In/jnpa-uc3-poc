"""ULIP VAHAN / SARATHI answers -> the canonical shared records.

Kept apart from :mod:`integrations.ulip.schemas` on purpose: that module is
pure ULIP-shape normalisation with no knowledge of the rest of the system,
while this one binds those flat dicts to :class:`jnpa_shared.schemas.VahanRecord`
and :class:`~jnpa_shared.schemas.SarathiRecord` — the shapes every downstream
consumer (``core.vehicle_rc`` writeback, /api/vahan responses, the Vehicle- and
Driver-Intelligence dashboards) already expects.

This is the ULIP counterpart of ``ingest/vahan_live/mappers.py``, which mapped
Surepass into exactly these records before Surepass was retired. Producing the
same records is what lets ULIP slot in as the LIVE_PRIMARY rung without any
downstream change.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Mapping, Optional

from jnpa_shared.schemas import (
    BlacklistStatus,
    SarathiRecord,
    VahanRecord,
    mask_owner_name,
    normalize_plate,
)

# VAHAN mixes these across its JSON and XML variants; SARATHI adds dd-mm-yyyy.
_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")


def parse_date(value: Any) -> Optional[date]:
    """Upstream date string -> date. Unparseable / empty -> None.

    Never guesses: a value that matches no known format degrades the single
    field to None rather than corrupting a validity window that the gate uses
    to admit or refuse a vehicle.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # VAHAN's rcStatusAsOn carries a trailing clock ("03-Feb-2026 05:02:62604")
    # whose seconds field is not even valid — take the date part only.
    text = text.split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _blacklist(value: Any) -> BlacklistStatus:
    """VAHAN leaves rcBlacklistStatus empty for a clear vehicle."""
    text = str(value or "").strip().upper()
    if text in {"BLACKLIST", "BLACKLISTED", "YES", "TRUE", "1"}:
        return BlacklistStatus.BLACKLISTED
    return BlacklistStatus.CLEAR


def _already_masked(name: str) -> bool:
    """True when VAHAN has already masked the value for us.

    VAHAN masks rc_owner_name by default for every user, emitting
    ``R***L K***R``. Running :func:`mask_owner_name` over that would produce
    ``R*********`` — masking the mask, destroying the little signal the field
    still carries. Detect it and pass through untouched.
    """
    return "*" in name


def rc_to_record(fields: Mapping[str, Any]) -> Optional[VahanRecord]:
    """A normalised ULIP RC dict (see ``normalize_rc``) -> VahanRecord."""
    if not fields:
        return None
    plate = normalize_plate(str(fields.get("rc_number") or ""))
    if not plate:
        return None
    owner = fields.get("owner_name")
    owner_text = str(owner).strip() if owner else ""
    return VahanRecord(
        rc_number=plate,
        plate=plate,
        owner_name_masked=(owner_text if _already_masked(owner_text)
                           else mask_owner_name(owner_text)) or None,
        vehicle_class=fields.get("vehicle_class"),
        fuel_type=fields.get("fuel_type"),
        fitness_valid_to=parse_date(fields.get("fitness_valid_to")),
        puc_valid_to=parse_date(fields.get("puc_valid_to")),
        insurance_valid_to=parse_date(fields.get("insurance_valid_to")),
        registration_date=parse_date(fields.get("registration_date")),
        state=fields.get("state"),
        rto_code=(str(fields["rto_code"]) if fields.get("rto_code") is not None
                  else None),
        blacklist_status=_blacklist(fields.get("blacklist_status")),
    )


# SARATHI/02 reports status as free text ("Active.", "Suspended") rather than a
# blacklist flag; anything that is not clearly active is treated as blacklisted
# so a suspended licence can never be admitted by omission.
_DL_ACTIVE_MARKERS = ("ACTIVE", "VALID")


def dl_to_record(dl_number: str, fields: Mapping[str, Any]) -> Optional[SarathiRecord]:
    """A normalised ULIP DL dict (see ``normalize_dl``) -> SarathiRecord."""
    if not fields:
        return None
    holder = fields.get("holder_name")
    holder_text = str(holder).strip() if holder else ""
    status = str(fields.get("dl_status") or "").strip().upper()
    # The transport validity is the one that matters for a port-corridor
    # driver; fall back to the non-transport date when it is absent.
    valid_to = (parse_date(fields.get("transport_valid_to"))
                or parse_date(fields.get("non_transport_valid_to")))
    return SarathiRecord(
        dl_number=str(dl_number).strip().upper(),
        holder_name_masked=(holder_text if _already_masked(holder_text)
                            else mask_owner_name(holder_text)) or None,
        date_of_issue=None,   # SARATHI/02 does not publish an issue date
        valid_to=valid_to,
        vehicle_classes=[str(c) for c in (fields.get("vehicle_classes") or [])],
        state=None,
        rto_code=None,
        blacklist_status=(
            BlacklistStatus.CLEAR
            if any(marker in status for marker in _DL_ACTIVE_MARKERS)
            else BlacklistStatus.BLACKLISTED
        ),
    )


def rc_payload(record: VahanRecord) -> Dict[str, Any]:
    """VahanRecord -> the JSON body the /api/vahan rungs exchange.

    ``mode="json"`` so dates serialise to ISO strings, matching exactly what
    the vahan-sim upstream returns — the orchestrator caches and envelopes both
    rungs identically and must not be able to tell them apart by shape.
    """
    return record.model_dump(mode="json")


__all__ = ["rc_to_record", "dl_to_record", "rc_payload", "parse_date"]
