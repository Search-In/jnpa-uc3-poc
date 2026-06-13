"""Map Surepass response payloads into the shared JNPA schemas.

Surepass wraps results in ``{"data": {...}, "status_code": 200, ...}``. Field
names vary across their RC/DL/FASTag products; these mappers are tolerant —
they accept any of the known aliases and degrade missing fields to ``None``
rather than raising, so a partial upstream payload still yields a usable record.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Optional

from jnpa_shared.schemas import (
    BlacklistStatus,
    FastagPing,
    FastagStatus,
    SarathiRecord,
    VahanRecord,
    mask_owner_name,
    normalize_plate,
)


def _first(d: Mapping[str, Any], *keys: str) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "NA", "NaN"):
            return d[k]
    return None


def _date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _blacklist(value: Any) -> BlacklistStatus:
    s = str(value or "").strip().upper()
    # Surepass uses NA / null / "No" for clear; anything truthy-negative = blacklisted.
    if s in {"BLACKLIST", "BLACKLISTED", "YES", "TRUE", "1"}:
        return BlacklistStatus.BLACKLISTED
    return BlacklistStatus.CLEAR


def map_rc(payload: Mapping[str, Any]) -> VahanRecord:
    d = payload.get("data") or payload
    rc_number = normalize_plate(str(_first(d, "rc_number", "registration_number", "license_plate") or ""))
    owner = _first(d, "owner_name", "ownerName")
    return VahanRecord(
        rc_number=rc_number,
        plate=rc_number,
        owner_name_masked=mask_owner_name(str(owner)) if owner else None,
        vehicle_class=_first(d, "vehicle_class", "vehicle_class_description"),
        fuel_type=_first(d, "fuel_type"),
        fitness_valid_to=_date(_first(d, "fit_up_to", "fitness_upto", "fitness_valid_to")),
        puc_valid_to=_date(_first(d, "pucc_upto", "puc_valid_to", "pucc_valid_upto")),
        insurance_valid_to=_date(_first(d, "insurance_upto", "insurance_valid_to")),
        registration_date=_date(_first(d, "registration_date", "reg_date")),
        state=_first(d, "state", "registered_at"),
        rto_code=_first(d, "rto_code", "rto"),
        blacklist_status=_blacklist(_first(d, "blacklist_status", "rc_status")),
    )


def map_dl(payload: Mapping[str, Any]) -> SarathiRecord:
    d = payload.get("data") or payload
    name = _first(d, "name", "holder_name")
    classes = _first(d, "cov", "vehicle_classes", "class_of_vehicle") or []
    if isinstance(classes, str):
        classes = [c.strip() for c in classes.replace(",", " ").split() if c.strip()]
    elif isinstance(classes, list):
        classes = [
            str(c.get("cov", c)) if isinstance(c, dict) else str(c) for c in classes
        ]
    return SarathiRecord(
        dl_number=str(_first(d, "dl_number", "license_number", "id_number") or "").strip().upper(),
        holder_name_masked=mask_owner_name(str(name)) if name else None,
        date_of_issue=_date(_first(d, "doi", "date_of_issue", "issue_date")),
        valid_to=_date(_first(d, "doe", "valid_upto", "valid_to", "expiry_date")),
        vehicle_classes=classes,
        state=_first(d, "state"),
        rto_code=_first(d, "rto_code", "rto"),
        blacklist_status=_blacklist(_first(d, "blacklist_status")),
    )


def _fastag_status(value: Any, balance: Optional[float]) -> FastagStatus:
    s = str(value or "").strip().upper()
    if "BLACKLIST" in s or "EXCEPTION" in s:
        return FastagStatus.BLACKLISTED
    if "INACTIVE" in s or "CLOSED" in s:
        return FastagStatus.INACTIVE
    if balance is not None and balance < 100:
        return FastagStatus.LOW_BALANCE
    return FastagStatus.ACTIVE


def map_fastag(payload: Mapping[str, Any], plate: str) -> FastagPing:
    d = payload.get("data") or payload
    bal_raw = _first(d, "tag_balance", "balance", "available_balance")
    balance: Optional[float] = None
    if bal_raw is not None:
        try:
            balance = float(str(bal_raw).replace(",", ""))
        except (TypeError, ValueError):
            balance = None
    return FastagPing(
        plate=normalize_plate(plate),
        tag_id=_first(d, "tag_id", "tid", "epc"),
        reader_id="surepass",
        bank=_first(d, "issuer_bank", "bank_name", "bank"),
        balance=balance,
        status=_fastag_status(_first(d, "tag_status", "status", "vehicle_status"), balance),
    )
