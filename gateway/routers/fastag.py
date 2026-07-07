"""/api/fastag — authenticated REST surface over the FASTag ULIP stack.

This router is a THIN integration seam. It performs no business logic of its own;
it only:

  1. authenticates/authorises (via the global gateway auth middleware + RBAC),
  2. validates the request (Pydantic models below — rejected before the service),
  3. generates / propagates the ``X-Correlation-ID`` (== ``client_id``),
  4. sequences the three already-built layers for each call:

        ULIP client (transport)  ->  mapper (contract)  ->  FastagService (orchestration)

  5. maps the client/service result to a clean HTTP status (never a stack trace).

All persistence, dedup/idempotency, Decimal/timestamp handling and vendor-field
logging live in :mod:`services.fastag` — the service remains the single
orchestration point.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from time import perf_counter
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from jnpa_shared.schemas import is_valid_plate, normalize_plate

from services.fastag import (
    FastagService,
    UlipClientError,
    UlipFastagClient,
    map_fastag_balance,
    map_fastag_transactions,
    map_toll_enroute,
)

from ..logging import get_logger

log = get_logger("gateway.fastag")

router = APIRouter(prefix="/api/fastag", tags=["fastag"])

CORRELATION_HEADER = "X-Correlation-ID"

# Toll vehicle classes we accept for an enroute request. Case-insensitive; kept
# deliberately permissive but non-empty so an obviously bogus value is rejected
# at the gateway rather than sent to the vendor.
VEHICLE_TYPES: frozenset[str] = frozenset(
    {"CAR", "LMV", "LGV", "HGV", "TRUCK", "BUS", "MAV", "MMV", "2W", "3W"}
)


# --------------------------------------------------------------------------- deps
# Singleton lifecycle: the client and service are created lazily on first request
# and cached at module scope for the process lifetime — so the httpx connection
# pool and the SQLAlchemy async engine are built once and reused across requests
# (never per-request). They hold no per-request state, so sharing is safe. Both are
# dependency-injected so tests can override them (a MockTransport client + a
# throwaway DSN) via ``app.dependency_overrides``. There is no explicit teardown:
# the pools are closed by process exit / the gateway lifespan's ``state.aclose()``.
_client: Optional[UlipFastagClient] = None
_service: Optional[FastagService] = None


def get_client() -> UlipFastagClient:
    global _client
    if _client is None:
        # 10s timeout + 2 retries come from the client's own defaults / env.
        _client = UlipFastagClient.from_env()
    return _client


def get_service(request: Request) -> FastagService:
    global _service
    if _service is None:
        dsn = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = FastagService(dsn=getattr(dsn, "postgres_dsn", None) or None)
    return _service


# --------------------------------------------------------------------- validation
def _clean_rc(value: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError("rc_number is required")
    norm = normalize_plate(value)
    if not is_valid_plate(norm):
        raise ValueError("invalid rc_number format")
    return norm


class BalanceRequest(BaseModel):
    rc_number: str = Field(..., description="Vehicle registration / RC number")

    model_config = {"json_schema_extra": {"example": {"rc_number": "MH12XX1234"}}}

    @field_validator("rc_number")
    @classmethod
    def _v_rc(cls, v: str) -> str:
        return _clean_rc(v)


class TransactionsRequest(BaseModel):
    rc_number: str = Field(..., description="Vehicle registration / RC number")

    model_config = {"json_schema_extra": {"example": {"rc_number": "MH12XX1234"}}}

    @field_validator("rc_number")
    @classmethod
    def _v_rc(cls, v: str) -> str:
        return _clean_rc(v)


class TollEnrouteRequest(BaseModel):
    source_state: str = Field(..., min_length=1)
    source_name: str = Field(..., min_length=1)
    destination_state: str = Field(..., min_length=1)
    destination_name: str = Field(..., min_length=1)
    vehicle_type: str = Field(..., min_length=1)

    model_config = {
        "json_schema_extra": {
            "example": {
                "source_state": "Maharashtra", "source_name": "Nhava Sheva",
                "destination_state": "Maharashtra", "destination_name": "Pune",
                "vehicle_type": "TRUCK",
            }
        }
    }

    @field_validator("source_state", "source_name", "destination_state",
                     "destination_name", "vehicle_type")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("must not be empty")
        return str(v).strip()

    @field_validator("vehicle_type")
    @classmethod
    def _v_vehicle(cls, v: str) -> str:
        if str(v).strip().upper() not in VEHICLE_TYPES:
            raise ValueError(f"invalid vehicle_type; expected one of {sorted(VEHICLE_TYPES)}")
        return str(v).strip().upper()


# ----------------------------------------------------------------- response models
class BalanceResult(BaseModel):
    rc_number: Optional[str] = None
    tag_id: Optional[str] = None
    available_balance: Optional[str] = None
    tag_status: Optional[str] = None
    updated: bool = True
    correlation_id: str
    # Full snapshot fields the mapper already produced (surfaced for the UI).
    provider_name: Optional[str] = None
    provider_code: Optional[str] = None
    customer_name: Optional[str] = None
    available_recharge_limit: Optional[str] = None
    vehicle_class: Optional[str] = None
    vehicle_class_desc: Optional[str] = None
    model_name: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {"rc_number": "MH12XX1234", "tag_id": "34161FA8...",
                        "available_balance": "509.00", "tag_status": "Activated",
                        "provider_name": "idfc_first_bank", "provider_code": "IDFC88000PATXM",
                        "customer_name": "SURAJE", "available_recharge_limit": "9491.00",
                        "vehicle_class": "4", "vehicle_class_desc": "Car / Jeep / Van",
                        "model_name": None, "updated": True, "correlation_id": "b6f1..."}
        }
    }


class TollPlazaOut(BaseModel):
    name: Optional[str] = None
    cost: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class TollEnrouteResult(BaseModel):
    id: str
    source: Optional[str] = None
    destination: Optional[str] = None
    distance: Optional[str] = None
    duration: Optional[str] = None
    plaza_count: int = 0
    toll_plaza_details: list[TollPlazaOut] = Field(default_factory=list)
    correlation_id: str


class TransactionRow(BaseModel):
    seq_no: Optional[str] = None
    transaction_date_time: Optional[datetime] = None
    toll_plaza_name: Optional[str] = None
    toll_plaza_geocode: Optional[str] = None
    vehicle_type: Optional[str] = None
    lane_direction: Optional[str] = None
    bank_name: Optional[str] = None
    status: Optional[str] = None


class TransactionsResult(BaseModel):
    inserted_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    total: int = 0
    correlation_id: str
    transactions: list[TransactionRow] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "example": {"inserted_count": 8, "skipped_count": 2,
                        "failed_count": 0, "total": 10, "correlation_id": "b6f1...",
                        "transactions": []}
        }
    }


_ERROR_RESPONSES = {
    400: {"description": "Validation error"},
    401: {"description": "Missing / invalid bearer token"},
    403: {"description": "Role not permitted"},
    409: {"description": "Conflict (duplicate)"},
    500: {"description": "Internal error"},
    502: {"description": "ULIP upstream error"},
    504: {"description": "ULIP timeout"},
}


# --------------------------------------------------------------------- helpers
def _correlation_id(request: Request) -> str:
    return request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())


def _ms(t0: float) -> float:
    return round((perf_counter() - t0) * 1000, 1)


def _dstr(v: object) -> Optional[str]:
    """Decimal/number -> string for the JSON response (preserves precision)."""
    return None if v is None else str(v)


def _log_gateway(endpoint: str, method: str, status: str, t0: float, client_id: str) -> None:
    log.info("fastag.gateway", module="fastag", stage="gateway", endpoint=endpoint,
             method=method, status=status, latency_ms=_ms(t0), client_id=client_id)


# ULIP failure category -> HTTP status (see UlipClientError.category).
_ULIP_STATUS = {"timeout": 504, "unavailable": 502, "http_error": 502,
                "bad_response": 502, "config": 500}
# Service FAILED reason -> HTTP status.
_SERVICE_STATUS = {"validation_error": 400, "conflict": 409, "db_error": 500}


def _fail(endpoint: str, method: str, t0: float, cid: str, http_status: int,
          error: str, detail: Optional[str] = None) -> "HTTPException":
    """Log the gateway line and build a clean (stack-trace-free) HTTPException."""
    _log_gateway(endpoint, method, "FAILED", t0, cid)
    body: dict = {"error": error, "correlation_id": cid}
    if detail:
        body["detail"] = detail
    return HTTPException(status_code=http_status, detail=body,
                         headers={CORRELATION_HEADER: cid})


async def _run(endpoint, method, request, response, *, fetch, mapper, persist):
    """Shared pipeline: client(fetch) -> mapper -> service(persist) -> result dict.

    Raises HTTPException on any upstream/mapper/service failure with the correct
    status. Returns the service SUCCESS envelope on the happy path.
    """
    cid = _correlation_id(request)
    response.headers[CORRELATION_HEADER] = cid
    t0 = perf_counter()

    # 1) transport
    try:
        raw = await fetch(cid)
    except UlipClientError as exc:
        http_status = _ULIP_STATUS.get(exc.category, 502)
        raise _fail(endpoint, method, t0, cid, http_status,
                    "ulip_error", f"{exc.category}")

    # 2) mapper (vendor-contract). A failed envelope == malformed vendor data.
    mapped = mapper(raw, client_id=cid)
    if mapped.get("status") != "success":
        raise _fail(endpoint, method, t0, cid, 502, "ulip_error",
                    f"mapper: {mapped.get('reason')}")

    # 3) service (single orchestration point). Map its status envelope to HTTP.
    result = await persist(mapped, cid)
    if result.get("status") != "SUCCESS":
        reason = result.get("reason", "db_error")
        raise _fail(endpoint, method, t0, cid, _SERVICE_STATUS.get(reason, 500),
                    "service_error", reason)

    _log_gateway(endpoint, method, "SUCCESS", t0, cid)
    # Also return the mapper envelope so endpoints can surface the already-mapped
    # detail (full balance snapshot / transaction rows / plaza array) in the
    # response — no extra fetch, no business-logic change.
    return result, cid, mapped


# --------------------------------------------------------------------- endpoints
@router.post("/balance", response_model=BalanceResult, responses=_ERROR_RESPONSES,
             summary="RC -> FASTag balance (fetch, persist snapshot)")
async def balance(
    body: BalanceRequest, request: Request, response: Response,
    client: UlipFastagClient = Depends(get_client),
    service: FastagService = Depends(get_service),
) -> BalanceResult:
    result, cid, mapped = await _run(
        "/api/fastag/balance", "POST", request, response,
        fetch=lambda cid: client.balance(body.rc_number, client_id=cid),
        mapper=map_fastag_balance,
        persist=lambda m, cid: service.process_balance(m, client_id=cid),
    )
    db = mapped.get("db") or {}
    return BalanceResult(
        rc_number=db.get("rc_number"), tag_id=db.get("tag_id"),
        available_balance=_dstr(db.get("available_balance")),
        tag_status=db.get("tag_status"),
        provider_name=db.get("provider_name"), provider_code=db.get("provider_code"),
        customer_name=db.get("customer_name"),
        available_recharge_limit=_dstr(db.get("available_recharge_limit")),
        vehicle_class=db.get("vehicle_class"), vehicle_class_desc=db.get("vehicle_class_desc"),
        model_name=db.get("model_name"),
        updated=bool(result.get("updated", True)), correlation_id=cid,
    )


@router.post("/toll-enroute", response_model=TollEnrouteResult, responses=_ERROR_RESPONSES,
             summary="Toll plazas enroute (fetch, persist route + plaza JSONB)")
async def toll_enroute(
    body: TollEnrouteRequest, request: Request, response: Response,
    client: UlipFastagClient = Depends(get_client),
    service: FastagService = Depends(get_service),
) -> TollEnrouteResult:
    payload = {
        "clientId": _correlation_id(request),
        "sourceState": body.source_state, "sourceName": body.source_name,
        "destinationState": body.destination_state, "destinationName": body.destination_name,
        "vehicleType": body.vehicle_type,
    }
    result, cid, mapped = await _run(
        "/api/fastag/toll-enroute", "POST", request, response,
        fetch=lambda cid: client.toll_enroute(payload, client_id=cid),
        mapper=map_toll_enroute,
        persist=lambda m, cid: service.process_toll_enroute(m, client_id=cid),
    )
    db = mapped.get("db") or {}
    plazas = [
        TollPlazaOut(name=p.get("name"), cost=p.get("cost"),
                     lat=p.get("lat"), lng=p.get("lng"))
        for p in (db.get("toll_plaza_details") or [])
    ]
    return TollEnrouteResult(
        id=result.get("id"), source=result.get("source"),
        destination=result.get("destination"), distance=_dstr(db.get("distance")),
        duration=db.get("duration"),
        plaza_count=int(result.get("plaza_count", 0)),
        toll_plaza_details=plazas, correlation_id=cid,
    )


@router.post("/transactions", response_model=TransactionsResult, responses=_ERROR_RESPONSES,
             summary="RC -> FASTag transactions (fetch, dedup-persist batch)")
async def transactions(
    body: TransactionsRequest, request: Request, response: Response,
    client: UlipFastagClient = Depends(get_client),
    service: FastagService = Depends(get_service),
) -> TransactionsResult:
    result, cid, mapped = await _run(
        "/api/fastag/transactions", "POST", request, response,
        fetch=lambda cid: client.transactions(body.rc_number, client_id=cid),
        mapper=map_fastag_transactions,
        persist=lambda m, cid: service.process_transactions(m, client_id=cid),
    )
    rows = [
        TransactionRow(
            seq_no=r.get("seq_no"), transaction_date_time=r.get("transaction_date_time"),
            toll_plaza_name=r.get("toll_plaza_name"), toll_plaza_geocode=r.get("toll_plaza_geocode"),
            vehicle_type=r.get("vehicle_type"), lane_direction=r.get("lane_direction"),
            bank_name=r.get("bank_name"), status=r.get("status"),
        )
        for r in (mapped.get("db") or [])
    ]
    return TransactionsResult(
        inserted_count=int(result.get("inserted_count", 0)),
        skipped_count=int(result.get("skipped_count", 0)),
        failed_count=int(result.get("failed_count", 0)),
        total=int(result.get("total", 0)), correlation_id=cid,
        transactions=rows,
    )


@router.get("/health", summary="FASTag module health (vendor config + DB reachability)")
async def health(
    service: FastagService = Depends(get_service),
    client: UlipFastagClient = Depends(get_client),
) -> dict:
    """Lightweight readiness probe for the FASTag module.

    Reports whether the ULIP vendor base URL is configured and whether the three
    ``jnpa.fastag_*`` tables are reachable. Does not call the vendor (no cost, no
    side effects). ``status`` is ``ok`` only when the DB is reachable, all tables
    exist, and the vendor URL is configured.
    """
    ulip_configured = bool(getattr(client, "_base_url", ""))
    db_status = "ok"
    tables: dict[str, bool] = {}
    try:
        from sqlalchemy import text
        from jnpa_shared.db import get_engine

        async with get_engine(getattr(service, "_dsn", None)).connect() as conn:
            for t in ("fastag_balance", "fastag_transactions", "toll_enroute"):
                r = await conn.execute(text("SELECT to_regclass(:t)"), {"t": f"jnpa.{t}"})
                tables[t] = r.scalar() is not None
    except Exception as exc:  # noqa: BLE001 — health must never raise
        db_status = "unreachable"
        log.warning("fastag.health.db_error", module="fastag", stage="gateway",
                    error=f"{type(exc).__name__}: {exc!s}")
    ok = db_status == "ok" and bool(tables) and all(tables.values()) and ulip_configured
    return {
        "module": "fastag", "status": "ok" if ok else "degraded",
        "ulip_configured": ulip_configured, "db": db_status, "tables": tables,
    }
