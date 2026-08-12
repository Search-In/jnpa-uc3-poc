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

from integrations.ulip import (
    UlipAccessDenied,
    UlipAuthError,
    UlipError,
    UlipHTTPError,
    UlipInvalidRequest,
    UlipNotConfigured,
    UlipTimeout,
    UlipUnavailable,
)
from integrations.ulip import UlipClient

from services.fastag import (
    FastagService,
    map_fastag_tag_status,
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
_client: Optional[UlipClient] = None
_service: Optional[FastagService] = None


def get_client() -> UlipClient:
    """The shared ULIP client — the same instance shape /api/logistics,
    /api/ldb and /api/vahan use, so one login token, one retry budget and one
    redaction policy cover every ULIP call the gateway makes."""
    global _client
    if _client is None:
        _client = UlipClient()
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
    # ULIP grants no wallet-balance API, so this surface can only ever replay a
    # stored snapshot. ``data_available`` false + source NOT_PROVIDED_BY_ULIP is
    # the honest answer for an RC we hold nothing for — never a made-up figure.
    data_available: bool = True
    source: Optional[str] = None

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
    # Which store the returned `transactions` came from: always "RDS" now (the UI
    # displays the persisted core.fastag_transaction history, not just the raw
    # fetch batch). `fetch_source` records where the underlying refresh came from
    # (LIVE = real ULIP vendor, SIM = ULIP simulator). Surfaced as a UI badge.
    source: str = "RDS"
    fetch_source: str = "SIM"
    rc_number: Optional[str] = None
    stored_count: int = 0

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


# Retained for the demo/validation paths that still raise category strings.
_ULIP_STATUS = {"timeout": 504, "unavailable": 502, "http_error": 502,
                "bad_response": 502, "config": 500}


async def _raw(awaitable) -> dict:
    """Shared-client ``UlipEnvelope`` -> the plain dict the mappers consume.

    The mappers are written against raw vendor JSON (they predate the typed
    client and are also fed by recorded fixtures and the demo provider), so the
    envelope is dumped back to a dict rather than leaking a pydantic model into
    the contract layer."""
    envelope = await awaitable
    return envelope.model_dump()


def _ulip_failure(exc: UlipError) -> tuple[int, str]:
    """One shared UlipError -> (HTTP status, category) mapping.

    ``access_denied`` (412) is called out separately from ``auth``: it is the
    source-IP allowlist, so the operator's fix is registering an egress IP with
    NLDSL, not rotating the credential. Both are 502 to the caller — the
    upstream refused us, the request itself was fine.
    """
    if isinstance(exc, UlipInvalidRequest):
        return 400, "invalid_request"
    if isinstance(exc, UlipNotConfigured):
        return 500, "config"
    if isinstance(exc, UlipAccessDenied):
        return 502, "access_denied"
    if isinstance(exc, UlipAuthError):
        return 502, "auth"
    if isinstance(exc, UlipTimeout):
        return 504, "timeout"
    if isinstance(exc, UlipUnavailable):
        return 502, "unavailable"
    if isinstance(exc, UlipHTTPError):
        return (429 if exc.is_rate_limited else 502), "http_error"
    return 502, "bad_response"
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
    except UlipError as exc:
        http_status, category = _ulip_failure(exc)
        raise _fail(endpoint, method, t0, cid, http_status,
                    "ulip_error", category)

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
             summary="RC -> last known FASTag balance snapshot (no live ULIP source)")
async def balance(
    body: BalanceRequest, request: Request, response: Response,
    service: FastagService = Depends(get_service),
) -> BalanceResult:
    """Serve the last persisted balance snapshot for an RC.

    **ULIP grants no wallet-balance API.** FASTAG/01 returns toll crossings and
    FASTAG/02 the tag registry; neither carries a balance
    (ulip-docs/ULIP_FASTAG_Integration_Requirement.pdf §1.3–1.4). So this
    endpoint no longer fetches: it reads back whatever
    ``core.fastag_balance`` already holds and reports
    ``source: NOT_PROVIDED_BY_ULIP`` when there is nothing.

    Inventing a figure here would be worse than an empty answer — a fabricated
    balance drives real decisions at the gate. Same no-fabrication rule the
    logistics surfaces already enforce.
    """
    cid = _correlation_id(request)
    response.headers[CORRELATION_HEADER] = cid
    t0 = perf_counter()
    row = await _read_balance_snapshot(request, body.rc_number)
    _log_gateway("/api/fastag/balance", "POST", "SUCCESS", t0, cid)
    if not row:
        return BalanceResult(
            rc_number=body.rc_number, data_available=False,
            source="NOT_PROVIDED_BY_ULIP", correlation_id=cid, updated=False,
        )
    return BalanceResult(
        rc_number=row.get("rc_number"), tag_id=row.get("tag_id"),
        available_balance=_dstr(row.get("available_balance")),
        tag_status=row.get("tag_status"),
        provider_name=row.get("provider_name"), provider_code=row.get("provider_code"),
        customer_name=row.get("customer_name"),
        available_recharge_limit=_dstr(row.get("available_recharge_limit")),
        vehicle_class=row.get("vehicle_class"),
        vehicle_class_desc=row.get("vehicle_class_desc"),
        model_name=row.get("model_name"),
        data_available=True, source="DATABASE",
        updated=False, correlation_id=cid,
    )


@router.post("/toll-enroute", response_model=TollEnrouteResult, responses=_ERROR_RESPONSES,
             summary="Toll plazas enroute (resolved from the GatiShakti plaza registry)")
async def toll_enroute(
    body: TollEnrouteRequest, request: Request, response: Response,
    client: UlipClient = Depends(get_client),
    service: FastagService = Depends(get_service),
) -> TollEnrouteResult:
    """Toll plazas along a source -> destination route.

    ULIP grants no route-planning API — FASTAG/01 answers per-vehicle crossing
    history, not "plazas between A and B". The plaza set therefore comes from
    the **GatiShakti NHAI registry** (``GATISHAKTI/04``, persisted to
    ``core.gs_toll_plaza`` — see services.gatishakti), which is the only
    granted source that enumerates plazas by geography.
    """
    payload = {
        "clientId": _correlation_id(request),
        "sourceState": body.source_state, "sourceName": body.source_name,
        "destinationState": body.destination_state, "destinationName": body.destination_name,
        "vehicleType": body.vehicle_type,
    }
    result, cid, mapped = await _run(
        "/api/fastag/toll-enroute", "POST", request, response,
        fetch=lambda cid: _enroute_from_registry(request, body, payload),
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
    client: UlipClient = Depends(get_client),
    service: FastagService = Depends(get_service),
) -> TransactionsResult:
    result, cid, mapped = await _run(
        "/api/fastag/transactions", "POST", request, response,
        # FASTAG/01. NLDSL retains only the past 72 hours per VRN, so this live
        # batch is a top-up: the durable history comes from the read-back below,
        # accumulated by services.fastag.poller.
        fetch=lambda cid: _raw(client.fetch_vehicle_movement(body.rc_number)),
        mapper=map_fastag_transactions,
        persist=lambda m, cid: service.process_transactions(m, client_id=cid),
    )
    # Data-consistency fix: after the fetch has been persisted (dedup) into
    # core.fastag_transaction, READ BACK the stored history for this RC and
    # surface THAT — so the UI shows the durable RDS record (all transactions
    # ever fetched for the RC), not just the transient current fetch batch.
    rc = _clean_rc(body.rc_number)
    stored = await _read_transactions_history(request, rc, limit=500)
    rows = [
        TransactionRow(
            seq_no=r.get("seq_no"), transaction_date_time=r.get("transaction_date_time"),
            toll_plaza_name=r.get("toll_plaza_name"), toll_plaza_geocode=r.get("toll_plaza_geocode"),
            vehicle_type=r.get("vehicle_type"), lane_direction=r.get("lane_direction"),
            bank_name=r.get("bank_name"), status=r.get("status"),
        )
        for r in stored
    ]
    # Fall back to the freshly-mapped batch only if the read-back failed (DB down).
    if not rows:
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
        source="RDS" if stored else "LIVE_BATCH",
        fetch_source=_fetch_source(client),
        rc_number=rc, stored_count=len(stored),
    )


@router.get("/transactions/history",
            summary="Stored FASTag transaction history for an RC (RDS, no vendor call)")
async def transactions_history(
    request: Request,
    rc_number: str,
    limit: int = 100,
) -> dict:
    """Read-only view of the persisted core.fastag_transaction for an RC. Makes
    no vendor call (no cost) — pure RDS. Used by the UI to re-display stored
    history on refresh. Always tagged source="RDS"."""
    rc = _clean_rc(rc_number)
    rows = await _read_transactions_history(request, rc, limit=limit)
    return {"source": "RDS", "rc_number": rc, "count": len(rows), "transactions": rows}


class TagRow(BaseModel):
    tag_id: Optional[str] = None
    rc_number: Optional[str] = None
    tid: Optional[str] = None
    vehicle_class: Optional[str] = None
    tag_status: Optional[str] = None
    issue_date: Optional[str] = None
    exc_code: Optional[str] = None
    bank_id: Optional[str] = None
    commercial_vehicle: Optional[str] = None


class TagStatusResult(BaseModel):
    rc_number: Optional[str] = None
    tag_id: Optional[str] = None
    count: int = 0
    tags: list[TagRow] = []
    correlation_id: str


class TagStatusRequest(BaseModel):
    """Exactly one of the two — FASTAG/02 rejects both together (respCode 239)."""

    rc_number: Optional[str] = Field(default=None)
    tag_id: Optional[str] = Field(default=None)

    @field_validator("rc_number")
    @classmethod
    def _v_rc(cls, v: Optional[str]) -> Optional[str]:
        return _clean_rc(v) if v and str(v).strip() else None


@router.post("/tag-status", response_model=TagStatusResult, responses=_ERROR_RESPONSES,
             summary="RC or tag id -> NETC tag registry entries (FASTAG/02)")
async def tag_status(
    body: TagStatusRequest, request: Request, response: Response,
    client: UlipClient = Depends(get_client),
) -> TagStatusResult:
    """Look up the NETC tag registry for a vehicle or a tag id.

    A vehicle legitimately carries SEVERAL tags (re-issues keep the old rows),
    so ``tags`` is a list. Read-through only — there is no tag-registry table,
    so nothing is persisted and the service layer is not involved.
    """
    cid = _correlation_id(request)
    response.headers[CORRELATION_HEADER] = cid
    t0 = perf_counter()
    try:
        raw = await _raw(client.fetch_tag_status(vehicle_number=body.rc_number,
                                                 tag_id=body.tag_id))
    except UlipError as exc:
        http_status, category = _ulip_failure(exc)
        raise _fail("/api/fastag/tag-status", "POST", t0, cid, http_status,
                    "ulip_error", category)
    mapped = map_fastag_tag_status(raw, client_id=cid)
    if mapped.get("status") != "success":
        raise _fail("/api/fastag/tag-status", "POST", t0, cid, 502, "ulip_error",
                    f"mapper: {mapped.get('reason')}")
    tags = [TagRow(**t) for t in (mapped.get("tags") or [])]
    _log_gateway("/api/fastag/tag-status", "POST", "SUCCESS", t0, cid)
    return TagStatusResult(rc_number=body.rc_number, tag_id=body.tag_id,
                           count=len(tags), tags=tags, correlation_id=cid)


def _fetch_source(client: "UlipClient") -> str:
    """LIVE when a ULIP credential is configured, else SIM."""
    return "LIVE" if getattr(client, "configured", False) else "SIM"


async def _enroute_from_registry(request: Request, body: "TollEnrouteRequest",
                                 payload: dict) -> dict:
    """Build a toll-enroute answer from the GatiShakti plaza registry.

    Returns the same shape :func:`map_toll_enroute` already consumes, so the
    mapper, the service and the response model are all untouched. Plazas come
    from ``core.gs_toll_plaza`` (GATISHAKTI/04); ``cost`` is left None because
    no granted API publishes a tariff — a fabricated fare would be read as real
    money.

    An unseeded registry yields an empty plaza list rather than an error: the
    route lookup itself succeeded, there is simply nothing to report yet.
    """
    dsn = getattr(getattr(getattr(request.app.state, "gw", None), "cfg", None),
                  "postgres_dsn", None)
    plazas: list[dict] = []
    if dsn:
        from jnpa_shared.db import fetch_all

        try:
            rows = await fetch_all(
                """
                SELECT name, latitude, longitude
                FROM core.gs_toll_plaza
                -- CAST is load-bearing: asyncpg cannot infer the type of a bare
                -- parameter compared against NULL and raises
                -- AmbiguousParameterError, which the handler below swallowed —
                -- so this returned ZERO plazas for every route while looking
                -- like an unseeded registry. Same defect, same shape, as the
                -- one fixed in services/gatishakti/repository.py.
                WHERE (CAST(:state_name AS text) IS NULL
                       OR state_id = CAST(:state_name AS text))
                ORDER BY name
                LIMIT 200
                """,
                {"state_name": _state_id_for(body.source_state)}, dsn=dsn,
            )
            plazas = [
                {"name": r["name"], "cost": None,
                 "lat": r["latitude"], "lng": r["longitude"]}
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001 — registry not seeded yet
            # WARNING, not debug: an unseeded registry and a broken query are
            # indistinguishable to the caller (both yield an empty list), so
            # the only way to tell them apart is for this to be visible.
            log.warning("toll_enroute_registry_unavailable", error=str(exc))
    return {
        "clientId": payload["clientId"],
        "sourceState": body.source_state, "sourceName": body.source_name,
        "destinationState": body.destination_state,
        "destinationName": body.destination_name,
        "vehicleType": body.vehicle_type,
        "distance": None, "duration": None,
        "tollPlazaDetails": plazas,
    }


# GatiShakti keys its road/plaza APIs by the LGD state code, not a name.
_STATE_IDS = {"MAHARASHTRA": "27", "GUJARAT": "24", "GOA": "30",
              "KARNATAKA": "29", "MADHYA PRADESH": "23", "RAJASTHAN": "08"}


def _state_id_for(state_name: Optional[str]) -> Optional[str]:
    return _STATE_IDS.get(str(state_name or "").strip().upper())


async def _read_balance_snapshot(request: Request, rc: str) -> Optional[dict]:
    """The stored balance snapshot for an RC from core.fastag_balance.

    The only source this surface has — see :func:`balance` for why there is no
    live fetch. A DB outage yields None (reported as data_available: false),
    never a guess."""
    dsn = getattr(getattr(getattr(request.app.state, "gw", None), "cfg", None),
                  "postgres_dsn", None)
    if not dsn:
        return None
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT rc_number, tag_id, provider_name, provider_code, customer_name,
                   available_recharge_limit, available_balance, tag_status,
                   vehicle_class, vehicle_class_desc, model_name, updated_at
            FROM core.fastag_balance
            WHERE rc_number = :rc
            LIMIT 1
            """,
            {"rc": rc}, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("fastag_balance_db_unavailable", error=str(exc))
        return None
    return dict(rows[0]) if rows else None


async def _read_transactions_history(request: Request, rc: str, *, limit: int = 100) -> list[dict]:
    """Fetch persisted transactions for an RC from core.fastag_transaction."""
    dsn = getattr(getattr(getattr(request.app.state, "gw", None), "cfg", None),
                  "postgres_dsn", None)
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT seq_no, transaction_date_time, toll_plaza_name, toll_plaza_geocode,
                   vehicle_type, lane_direction, bank_name, status
            FROM core.fastag_transaction
            WHERE rc_number = :rc
            ORDER BY transaction_date_time DESC NULLS LAST, created_at DESC
            LIMIT :limit
            """,
            {"rc": rc, "limit": max(1, min(int(limit), 1000))},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("fastag_history_db_unavailable", error=str(exc))
        return []
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("transaction_date_time"), datetime):
            d["transaction_date_time"] = d["transaction_date_time"].isoformat()
        out.append(d)
    return out


@router.get("/health", summary="FASTag module health (vendor config + DB reachability)")
async def health(
    service: FastagService = Depends(get_service),
    client: UlipClient = Depends(get_client),
) -> dict:
    """Lightweight readiness probe for the FASTag module.

    Reports whether the ULIP vendor base URL is configured and whether the three
    ``jnpa.fastag_*`` tables are reachable. Does not call the vendor (no cost, no
    side effects). ``status`` is ``ok`` only when the DB is reachable, all tables
    exist, and the vendor URL is configured.
    """
    ulip_configured = bool(getattr(client, "configured", False))
    db_status = "ok"
    tables: dict[str, bool] = {}
    try:
        from sqlalchemy import text
        from jnpa_shared.db import get_engine

        async with get_engine(getattr(service, "_dsn", None)).connect() as conn:
            for t in ("fastag_balance", "fastag_transaction", "toll_enroute"):
                r = await conn.execute(text("SELECT to_regclass(:t)"), {"t": f"core.{t}"})
                tables[t] = r.scalar() is not None
    except Exception as exc:  # noqa: BLE001 — health must never raise
        db_status = "unreachable"
        log.warning("fastag.health.db_error", module="fastag", stage="gateway",
                    error=f"{type(exc).__name__}: {exc!s}")
    # A missing ULIP credential is a mode (demo), not a fault — health reflects
    # whether the module itself works, and "mode" tells the operator which rung.
    ok = db_status == "ok" and bool(tables) and all(tables.values())
    return {
        "module": "fastag", "status": "ok" if ok else "degraded",
        "mode": "live" if ulip_configured else "demo",
        "ulip_configured": ulip_configured, "db": db_status, "tables": tables,
        "auth_mode": getattr(client, "auth_mode", "none"),
        "apis": {"transactions": client.api_path("FASTAG"),
                 "tag_status": client.api_path("FASTAG_TAG")},
        # Surfaced so the operator is not left guessing why these tiles are
        # empty: neither is a fault, both are ULIP grant boundaries.
        "notes": {
            "balance": "NOT_PROVIDED_BY_ULIP — no wallet-balance API is granted",
            "retention": "FASTAG/01 retains 72 h per VRN; history is accumulated "
                         "by services.fastag.poller",
        },
    }
