"""ULIP FASTag DTO layer — the wire contract for the three vendor APIs.

These Pydantic v2 models are the single source of truth between the ULIP vendor
response, the service layer, and the ``jnpa.fastag_*`` Postgres tables. They map
1:1 to those tables (see ``infra/postgres/init.sql``).

Design rules enforced here (non-negotiable, per the foundation spec):

* **Money is never a float.** ``available_balance``, ``available_recharge_limit``
  and per-plaza ``cost``/``distance`` are :class:`~decimal.Decimal`, coerced from
  strings like ``"1,234.50"`` without binary-float rounding.
* **No silent field dropping.** ``extra="allow"`` captures any vendor field we do
  not model; :meth:`_FastagBase._log_unmapped` logs the leftover keys so an
  evolving vendor schema is visible, not swallowed.
* **Parsing failures are logged explicitly.** :func:`_to_decimal` /
  :func:`_to_datetime` log a structured warning on bad input instead of failing
  silently or crashing the whole batch.
* **No array-data loss.** ``toll_plaza_details`` and the transactions array are
  modelled as typed lists, not opaque blobs.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .logging import get_logger

log = get_logger("jnpa_shared.fastag")

# Leading numeric token, so a value carrying a unit ("195.2 km") yields "195.2".
_NUMERIC_PREFIX = re.compile(r"[-+]?[\d,]*\.?\d+")


def _numeric_prefix(value: Any) -> Any:
    """Return the leading numeric portion of ``value`` (drops a trailing unit like
    " km"); returns the value unchanged if it has no numeric prefix."""
    if value is None:
        return None
    m = _NUMERIC_PREFIX.match(str(value).strip())
    return m.group(0) if m else value

# Timestamp formats seen from ULIP/NETC in addition to ISO-8601 (which Pydantic
# parses natively). Tried in order before we give up and log a parse failure.
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

_MONEY_Q = Decimal("0.01")  # quantum for NUMERIC(10,2) financial columns


def _to_decimal(value: Any, *, field: str) -> Optional[Decimal]:
    """Coerce a vendor money value to a 2dp :class:`Decimal`, or ``None``.

    Accepts ``Decimal``/``int``/``float``/``str`` (``"1,234.50"`` → ``1234.50``).
    A malformed value is **logged** (never silently dropped) and returns ``None``
    so one bad field cannot discard an otherwise-valid record.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        dec = value
    else:
        try:
            # str() first so a binary float never poisons the Decimal.
            dec = Decimal(str(value).replace(",", "").strip())
        except (InvalidOperation, ValueError, TypeError):
            # Money is sensitive — log the type only, never the raw amount.
            log.warning("fastag.parse_failure", field=field, kind="decimal",
                        value_type=type(value).__name__)
            return None
    try:
        return dec.quantize(_MONEY_Q)
    except InvalidOperation:
        log.warning("fastag.parse_failure", field=field, kind="decimal_quantize",
                    value_type=type(value).__name__)
        return None


def _to_datetime(value: Any, *, field: str) -> Optional[datetime]:
    """Parse a vendor timestamp to an aware/naive :class:`datetime`, or ``None``.

    Tries ISO-8601 then the NETC formats in :data:`_TS_FORMATS`. A value that
    matches none is logged and returns ``None`` (never raises mid-batch).
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    log.warning("fastag.parse_failure", field=field, kind="datetime", value=repr(value))
    return None


class _FastagBase(BaseModel):
    """Base for all FASTag DTOs.

    ``extra="allow"`` means unknown vendor fields are retained on the instance
    (in ``__pydantic_extra__``) rather than dropped; the after-validator logs
    them so schema drift is observable. ``populate_by_name`` lets callers use
    either the snake_case field name or its camelCase vendor alias.
    """

    # protected_namespaces=() allows the ULIP ``model_name`` field without a
    # Pydantic "model_" namespace-collision warning.
    model_config = ConfigDict(extra="allow", populate_by_name=True, protected_namespaces=())

    @model_validator(mode="after")
    def _log_unmapped(self) -> "_FastagBase":
        extra = self.__pydantic_extra__ or {}
        if extra:
            log.warning(
                "fastag.unmapped_fields",
                model=type(self).__name__,
                fields=sorted(extra.keys()),
            )
        return self


# ---------------------------------------------------------------------------
# 1) Toll Enroute API  ->  jnpa.toll_enroute
# ---------------------------------------------------------------------------
class TollPlazaDetail(_FastagBase):
    """One toll plaza on an enroute route. ``cost`` is money (Decimal); ``lat``/
    ``lng`` are geo-coordinates (float is correct for coordinates)."""

    # Accept the authorised provider's field names (toll_plaza_name/_latitude/
    # _longitude, cost) as well as the earlier camelCase forms.
    name: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("toll_plaza_name", "tollPlazaName", "name")
    )
    cost: Optional[Decimal] = Field(
        default=None, validation_alias=AliasChoices("cost", "fare")
    )
    lat: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("toll_plaza_latitude", "latitude", "lat")
    )
    lng: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("toll_plaza_longitude", "longitude", "lng")
    )

    @field_validator("cost", mode="before")
    @classmethod
    def _cost(cls, v: Any) -> Optional[Decimal]:
        return _to_decimal(v, field="toll_plaza_detail.cost")


class TollEnrouteResponse(_FastagBase):
    """RC/route -> full list of toll plazas + trip metadata. Maps 1:1 to
    ``jnpa.toll_enroute`` (``toll_plaza_details`` persists as JSONB)."""

    client_id: Optional[str] = Field(default=None, validation_alias="clientId")
    source_state: Optional[str] = Field(default=None, validation_alias="sourceState")
    source_name: Optional[str] = Field(default=None, validation_alias="sourceName")
    destination_state: Optional[str] = Field(default=None, validation_alias="destinationState")
    destination_name: Optional[str] = Field(default=None, validation_alias="destinationName")
    vehicle_type: Optional[str] = Field(default=None, validation_alias="vehicleType")
    duration: Optional[str] = None
    distance: Optional[Decimal] = None
    toll_plaza_details: list[TollPlazaDetail] = Field(
        default_factory=list, validation_alias="tollPlazaDetails"
    )

    @field_validator("distance", mode="before")
    @classmethod
    def _distance(cls, v: Any) -> Optional[Decimal]:
        # Provider sends distance with a unit, e.g. "195.2 km" -> parse "195.2".
        return _to_decimal(_numeric_prefix(v), field="toll_enroute.distance")


# ---------------------------------------------------------------------------
# 2) RC -> FASTag Balance API  ->  jnpa.fastag_balance
# ---------------------------------------------------------------------------
class FastagBalanceResponse(_FastagBase):
    """ULIP-compliant RC-keyed balance snapshot. Maps 1:1 to
    ``jnpa.fastag_balance``. ``model_name`` is optional per vendor spec."""

    rc_number: Optional[str] = Field(default=None, validation_alias="rcNumber")
    tag_id: Optional[str] = Field(default=None, validation_alias="tagId")
    provider_name: Optional[str] = Field(default=None, validation_alias="providerName")
    provider_code: Optional[str] = Field(default=None, validation_alias="providerCode")
    customer_name: Optional[str] = Field(default=None, validation_alias="customerName")
    available_recharge_limit: Optional[Decimal] = Field(
        default=None, validation_alias="availableRechargeLimit"
    )
    available_balance: Optional[Decimal] = Field(
        default=None, validation_alias="availableBalance"
    )
    tag_status: Optional[str] = Field(default=None, validation_alias="tagStatus")
    vehicle_class: Optional[str] = Field(default=None, validation_alias="vehicleClass")
    vehicle_class_desc: Optional[str] = Field(default=None, validation_alias="vehicleClassDesc")
    model_name: Optional[str] = Field(default=None, validation_alias="modelName")

    @field_validator("available_recharge_limit", "available_balance", mode="before")
    @classmethod
    def _money(cls, v: Any) -> Optional[Decimal]:
        return _to_decimal(v, field="fastag_balance.money")

    @field_validator("provider_code", mode="before")
    @classmethod
    def _provider_code_str_safe(cls, v: Any) -> Optional[str]:
        # Vendors sometimes send provider_code as an int; keep it string-safe
        # so it never breaks validation or the TEXT column.
        return None if v is None else str(v)


# ---------------------------------------------------------------------------
# 3) RC -> FASTag Transaction API  ->  jnpa.fastag_transactions
# ---------------------------------------------------------------------------
class FastagTransactionItem(_FastagBase):
    """A single plaza crossing. ``seq_no`` is the vendor idempotency key
    (UNIQUE in DB). ``toll_plaza_geocode`` is kept raw ("lat,lng")."""

    id: UUID = Field(default_factory=uuid4)
    tag_id: Optional[str] = Field(default=None, validation_alias="tagId")
    rc_number: Optional[str] = Field(default=None, validation_alias="rcNumber")
    seq_no: Optional[str] = Field(default=None, validation_alias="seqNo")
    transaction_date_time: Optional[datetime] = Field(
        default=None, validation_alias="transactionDateTime"
    )
    lane_direction: Optional[str] = Field(default=None, validation_alias="laneDirection")
    toll_plaza_name: Optional[str] = Field(default=None, validation_alias="tollPlazaName")
    toll_plaza_geocode: Optional[str] = Field(default=None, validation_alias="tollPlazaGeocode")
    vehicle_type: Optional[str] = Field(default=None, validation_alias="vehicleType")

    # Batch-level fields the provider returns once per lookup (bank_name, status);
    # propagated onto each row by the batch so persisted rows are self-describing.
    bank_name: Optional[str] = None
    status: Optional[str] = None

    # Derived, NOT persisted: the mapper splits ``toll_plaza_geocode`` ("lat,lng")
    # into these for downstream consumers. The DB keeps only the raw geocode text,
    # so these are excluded from the DB-ready dict.
    geo_lat: Optional[float] = None
    geo_lng: Optional[float] = None

    @field_validator("seq_no", mode="before")
    @classmethod
    def _seq_no_str_safe(cls, v: Any) -> Optional[str]:
        # seq_no MUST be a string (preserve leading zeros / exact vendor token);
        # if the vendor sends it as a number, stringify rather than reject.
        return None if v is None else str(v)

    @field_validator("transaction_date_time", mode="before")
    @classmethod
    def _txn_ts(cls, v: Any) -> Optional[datetime]:
        return _to_datetime(v, field="fastag_transaction.transaction_date_time")


class FastagTransactionBatch(_FastagBase):
    """The Transaction API returns a batch keyed by RC/tag with a transactions
    array. Every item is preserved (no array-data loss)."""

    rc_number: Optional[str] = Field(default=None, validation_alias="rcNumber")
    tag_id: Optional[str] = Field(default=None, validation_alias="tagId")
    # Provider returns these once at the batch (data) level.
    bank_name: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("bank_name", "bankName")
    )
    status: Optional[str] = Field(default=None, validation_alias=AliasChoices("status"))
    transactions: list[FastagTransactionItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _propagate_keys(self) -> "FastagTransactionBatch":
        """Backfill each item's batch-level fields (rc_number/tag_id/bank_name/
        status) from the envelope so DB rows are self-describing."""
        for item in self.transactions:
            if item.rc_number is None:
                item.rc_number = self.rc_number
            if item.tag_id is None:
                item.tag_id = self.tag_id
            if item.bank_name is None:
                item.bank_name = self.bank_name
            if item.status is None:
                item.status = self.status
        return self


__all__ = [
    "TollPlazaDetail",
    "TollEnrouteResponse",
    "FastagBalanceResponse",
    "FastagTransactionItem",
    "FastagTransactionBatch",
]
