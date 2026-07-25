"""ULIP FASTag mappers — the strict contract boundary.

Each mapper takes a raw vendor JSON payload and returns a single envelope:

    {
      "status": "success" | "failed",
      "dto":    <pydantic DTO | list of DTOs>,   # present on success
      "db":     <dict | list of dicts>,          # DB-ready, exact column names
      "reason": "...",                            # present on failure
      "unmapped_fields": [...],                   # vendor fields we don't model
    }

Guarantees (non-negotiable):

* **No field drop.** Unknown vendor keys are surfaced (``unmapped_fields``) and
  already logged by ``_FastagBase._log_unmapped``; nothing is silently dropped.
* **No silent default substitution.** Unrecognised ``tag_status`` values pass
  through unchanged and are logged, not coerced to a default.
* **Money is Decimal only** (enforced by the DTO validators) — no float here.
* **Timestamps are timezone-aware UTC** (normalised below).
* **Never crashes the pipeline.** Any exception is caught and returned as
  ``{"status": "failed", "reason": ...}``.

The service layer (Step 3) consumes ``db`` directly — it contains no mapping
logic of its own.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from pydantic import BaseModel

from jnpa_shared.fastag import (
    FastagBalanceResponse,
    FastagTransactionBatch,
    FastagTransactionItem,
    TollEnrouteResponse,
)
from jnpa_shared.logging import get_logger

log = get_logger("services.fastag.mappers")

# tag_status normalisation. Keys are upper-cased/underscore-stripped vendor
# variants -> canonical presentation value. An unknown status is NOT mapped to a
# default (that would be a silent substitution); it passes through and is logged.
_TAG_STATUS_MAP = {
    "ACTIVE": "Activated",
    "ACTIVATED": "Activated",
    "A": "Activated",
    "LOWBALANCE": "LowBalance",
    "LOW_BALANCE": "LowBalance",
    "L": "LowBalance",
    "BLACKLISTED": "Blocked",
    "BLOCKED": "Blocked",
    "B": "Blocked",
    "EXCEPTION": "Blocked",
}


def _emit(api: str, client_id: Optional[str], status: str, unmapped: list[str]) -> None:
    """Emit the required per-request observability line."""
    log.info(
        "fastag.mapper",
        module="fastag", stage="mapper", api=api,
        client_id=client_id, status=status, unmapped_fields=unmapped,
    )


def _collect_unmapped(model: BaseModel) -> list[str]:
    """Every unmapped vendor key on ``model`` and its nested DTO lists."""
    found: set[str] = set(getattr(model, "__pydantic_extra__", None) or {})
    for value in model.__dict__.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, BaseModel):
                    found.update(_collect_unmapped(item))
    return sorted(found)


def _normalize_tag_status(raw: Optional[str], *, client_id: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    key = str(raw).strip().upper().replace(" ", "").replace("-", "")
    key_us = key.replace("_", "")
    mapped = _TAG_STATUS_MAP.get(key) or _TAG_STATUS_MAP.get(key_us)
    if mapped is None:
        # Passthrough, but make the unrecognised value visible (not silent).
        log.warning(
            "fastag.tag_status.unrecognized", module="fastag", stage="mapper",
            api="balance", client_id=client_id, value=repr(raw),
        )
        return str(raw)
    return mapped


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise to timezone-aware UTC. A naive value is treated as UTC (logged
    once by the caller context) rather than dropped."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _split_geocode(
    geocode: Optional[str], *, client_id: Optional[str]
) -> tuple[Optional[float], Optional[float]]:
    """Split a "lat,lng" geocode into floats. Malformed -> (None, None) + log."""
    if not geocode:
        return None, None
    parts = [p.strip() for p in str(geocode).split(",")]
    if len(parts) != 2:
        log.warning("fastag.geocode.malformed", module="fastag", stage="mapper",
                    api="transaction", client_id=client_id, value=repr(geocode))
        return None, None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        log.warning("fastag.geocode.malformed", module="fastag", stage="mapper",
                    api="transaction", client_id=client_id, value=repr(geocode))
        return None, None


def _fail(api: str, client_id: Optional[str], reason: str) -> dict[str, Any]:
    log.error("fastag.mapper.failed", module="fastag", stage="mapper",
              api=api, client_id=client_id, reason=reason)
    _emit(api, client_id, "failed", [])
    return {"status": "failed", "reason": reason, "unmapped_fields": []}


def _unwrap(raw: Any) -> Mapping[str, Any]:
    """ULIP commonly nests the payload under ``data`` / ``response``."""
    if isinstance(raw, Mapping):
        for key in ("data", "response", "result"):
            inner = raw.get(key)
            if isinstance(inner, Mapping):
                return inner
        return raw
    raise TypeError(f"expected object payload, got {type(raw).__name__}")


# ---------------------------------------------------------------------------
# A) Toll Enroute
# ---------------------------------------------------------------------------
def map_toll_enroute(raw: Any, *, client_id: Optional[str] = None) -> dict[str, Any]:
    """ULIP JSON -> TollEnrouteResponse + DB-ready dict for ``core.toll_enroute``.

    Preserves the full ``toll_plaza_details`` array (no flattening); ``cost``/
    ``distance`` are Decimal; plaza ``lat``/``lng`` are parsed floats.
    """
    try:
        data = _unwrap(raw)
        dto = TollEnrouteResponse.model_validate(data)
        if client_id and not dto.client_id:
            dto.client_id = client_id
        unmapped = _collect_unmapped(dto)

        plazas = [
            {
                "name": p.name,
                # Decimal preserved as string inside JSONB (no float, no rounding).
                "cost": None if p.cost is None else str(p.cost),
                "lat": p.lat,
                "lng": p.lng,
            }
            for p in dto.toll_plaza_details
        ]
        db = {
            "client_id": dto.client_id,
            "source_state": dto.source_state,
            "source_name": dto.source_name,
            "destination_state": dto.destination_state,
            "destination_name": dto.destination_name,
            "vehicle_type": dto.vehicle_type,
            "duration": dto.duration,
            "distance": dto.distance,                 # Decimal -> numeric(10,2)
            "toll_plaza_details": plazas,             # -> jsonb
        }
        _emit("enroute", client_id, "success", unmapped)
        return {"status": "success", "dto": dto, "db": db, "unmapped_fields": unmapped}
    except Exception as exc:  # never crash the pipeline
        return _fail("enroute", client_id, f"{type(exc).__name__}: {exc!s}")


# ---------------------------------------------------------------------------
# B) RC -> FASTag Balance
# ---------------------------------------------------------------------------
def map_fastag_balance(raw: Any, *, client_id: Optional[str] = None) -> dict[str, Any]:
    """ULIP JSON -> FastagBalanceResponse + DB-ready dict for ``core.fastag_balance``.

    Money fields are Decimal; ``tag_status`` is normalised (ACTIVE->Activated,
    LOW_BALANCE->LowBalance, BLACKLISTED->Blocked); ``provider_code`` string-safe.
    """
    try:
        data = _unwrap(raw)
        dto = FastagBalanceResponse.model_validate(data)
        unmapped = _collect_unmapped(dto)

        dto.tag_status = _normalize_tag_status(dto.tag_status, client_id=client_id)
        provider_code = None if dto.provider_code is None else str(dto.provider_code)
        dto.provider_code = provider_code

        db = {
            "rc_number": dto.rc_number,
            "tag_id": dto.tag_id,
            "provider_name": dto.provider_name,
            "provider_code": provider_code,           # always string-safe
            "customer_name": dto.customer_name,
            "available_recharge_limit": dto.available_recharge_limit,  # Decimal
            "available_balance": dto.available_balance,                # Decimal
            "tag_status": dto.tag_status,             # normalised
            "vehicle_class": dto.vehicle_class,
            "vehicle_class_desc": dto.vehicle_class_desc,
            "model_name": dto.model_name,
        }
        _emit("balance", client_id, "success", unmapped)
        return {"status": "success", "dto": dto, "db": db, "unmapped_fields": unmapped}
    except Exception as exc:
        return _fail("balance", client_id, f"{type(exc).__name__}: {exc!s}")


# ---------------------------------------------------------------------------
# C) RC -> FASTag Transaction (batch critical path)
# ---------------------------------------------------------------------------
def _map_txn_item(item: FastagTransactionItem, *, client_id: Optional[str]) -> dict[str, Any]:
    lat, lng = _split_geocode(item.toll_plaza_geocode, client_id=client_id)
    item.geo_lat, item.geo_lng = lat, lng
    return {
        "tag_id": item.tag_id,
        "rc_number": item.rc_number,
        "seq_no": None if item.seq_no is None else str(item.seq_no),  # never int
        "transaction_date_time": _to_utc(item.transaction_date_time),  # tz-aware UTC
        "lane_direction": item.lane_direction,
        "toll_plaza_name": item.toll_plaza_name,
        "toll_plaza_geocode": item.toll_plaza_geocode,   # raw preserved in DB
        "vehicle_type": item.vehicle_type,
        "bank_name": item.bank_name,     # batch-level, propagated onto the row
        "status": item.status,           # batch-level, propagated onto the row
    }


def map_fastag_transactions(raw: Any, *, client_id: Optional[str] = None) -> dict[str, Any]:
    """ULIP JSON -> FastagTransactionBatch + list of DB-ready dicts.

    Every transaction is preserved (no array-data loss). ``seq_no`` stays a
    string (no int coercion); ``transaction_date_time`` is normalised to UTC;
    ``toll_plaza_geocode`` is split into ``geo_lat``/``geo_lng`` on the DTO while
    the raw string is what persists.
    """
    try:
        data = _unwrap(raw)
        dto = FastagTransactionBatch.model_validate(data)
        unmapped = _collect_unmapped(dto)
        rows = [_map_txn_item(item, client_id=client_id) for item in dto.transactions]
        _emit("transaction", client_id, "success", unmapped)
        return {"status": "success", "dto": dto, "db": rows, "unmapped_fields": unmapped}
    except Exception as exc:
        return _fail("transaction", client_id, f"{type(exc).__name__}: {exc!s}")


__all__ = [
    "map_toll_enroute",
    "map_fastag_balance",
    "map_fastag_transactions",
]
