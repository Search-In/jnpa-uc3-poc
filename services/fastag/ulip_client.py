"""ULIP FASTag HTTP client — PURE TRANSPORT.

Single async abstraction over the three FASTag ULIP calls (Toll Enroute,
RC->Balance, RC->Transaction). This layer only speaks HTTP: it builds the
request, propagates the correlation id, retries transient failures with
exponential backoff, logs the full request/response, and returns the parsed
JSON. It performs **no** field transformation — that is the mapper's job.

Failures raise :class:`UlipClientError`; the caller (mapper/service) decides how
to degrade. Nothing here ever mutates or reshapes the vendor payload.
"""
from __future__ import annotations

import asyncio
import os
import json
import random
import time
from typing import Any, Mapping, Optional

import httpx

from jnpa_shared.logging import get_logger

from .demo_provider import demo_balance, demo_toll_enroute, demo_transactions

log = get_logger("services.fastag.ulip_client")

DEFAULT_TIMEOUT_S = 10.0
# retries = additional attempts after the first, so 2 -> up to 3 total tries.
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_BASE_S = 0.5
# Retry only on transient conditions; 4xx (except 429) won't change on replay.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Correlation id travels as a header so the vendor/proxy can echo it in logs.
CORRELATION_HEADER = "X-Correlation-ID"


class UlipClientError(Exception):
    """Transport-level failure talking to ULIP (after retries are exhausted).

    ``category`` classifies the failure so callers can map it to the right HTTP
    status without pattern-matching the reason string:

      * ``"timeout"``     — the vendor did not respond in time  -> 504
      * ``"unavailable"`` — connection refused / 5xx exhaustion  -> 502
      * ``"http_error"``  — vendor returned a non-retryable 4xx  -> 502
      * ``"bad_response"``— 200 with an unparseable body         -> 502
      * ``"config"``      — no base URL configured (our fault)   -> 500
    """

    def __init__(self, reason: str, *, api: str, category: str = "unavailable",
                 status: Optional[int] = None, client_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.api = api
        self.category = category
        self.status = status
        self.client_id = client_id


class UlipFastagClient:
    """Async transport for the three FASTag ULIP endpoints.

    Paths are overridable (constructor or ``FASTAG_ULIP_*_PATH`` env) because the
    exact vendor route prefix varies by ULIP deployment.
    """

    ENROUTE_PATH = "/fastag/toll-enroute"
    BALANCE_PATH = "/fastag/balance"
    TRANSACTION_PATH = "/fastag/transactions"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        client: Optional[httpx.AsyncClient] = None,
        enroute_path: Optional[str] = None,
        balance_path: Optional[str] = None,
        transaction_path: Optional[str] = None,
        auth_scheme: str = "bearer",
        auth_header: str = "X-API-Key",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_s)
        self._retries = max(0, retries)
        self._backoff_base_s = backoff_base_s
        # Auth scheme is env-driven, NOT assumed: the authorised provider may use a
        # bearer token, a custom API-key header, or gateway-level (none) auth.
        self._auth_scheme = (auth_scheme or "bearer").strip().lower()
        self._auth_header = auth_header or "X-API-Key"
        # An injected client (tests / shared pool) is not owned/closed by us.
        self._client = client
        self._owns_client = client is None
        self.ENROUTE_PATH = enroute_path or self.ENROUTE_PATH
        self.BALANCE_PATH = balance_path or self.BALANCE_PATH
        self.TRANSACTION_PATH = transaction_path or self.TRANSACTION_PATH

    @classmethod
    def from_env(cls, *, client: Optional[httpx.AsyncClient] = None) -> "UlipFastagClient":
        """Build from ``FASTAG_ULIP_URL`` / ``ULIP_API_KEY`` (+ optional path/tuning/auth envs)."""
        return cls(
            base_url=os.environ.get("FASTAG_ULIP_URL", os.environ.get("GATEWAY_ULIP_URL", "")),
            api_key=os.environ.get("ULIP_API_KEY", ""),
            timeout_s=float(os.environ.get("FASTAG_ULIP_TIMEOUT_S", DEFAULT_TIMEOUT_S)),
            retries=int(os.environ.get("FASTAG_ULIP_RETRIES", DEFAULT_RETRIES)),
            client=client,
            enroute_path=os.environ.get("FASTAG_ULIP_ENROUTE_PATH") or None,
            balance_path=os.environ.get("FASTAG_ULIP_BALANCE_PATH") or None,
            transaction_path=os.environ.get("FASTAG_ULIP_TRANSACTION_PATH") or None,
            auth_scheme=os.environ.get("FASTAG_ULIP_AUTH_SCHEME", "bearer"),
            auth_header=os.environ.get("FASTAG_ULIP_AUTH_HEADER", "X-API-Key"),
        )

    # -- lifecycle -----------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "UlipFastagClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self, client_id: str) -> dict[str, str]:
        headers = {"Accept": "application/json", CORRELATION_HEADER: client_id}
        # Apply the configured auth scheme. "none" leaves the request unauthenticated
        # (e.g. when a fronting gateway injects credentials); the key is never logged.
        if self._api_key and self._auth_scheme != "none":
            if self._auth_scheme == "apikey":
                headers[self._auth_header] = self._api_key
            else:  # default: bearer
                headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # -- core transport ------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        api: str,
        client_id: str,
        json: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Issue one ULIP call with retry+backoff; return parsed JSON or raise.

        Never transforms the body. Logs every attempt (request + response) with
        the correlation id so a call can be traced end-to-end.
        """
        if not self._base_url and os.environ.get("FASTAG_DEMO_MODE", "").lower() == "true":
            # Demo mode: no external ULIP dependency. Every FASTag API has a
            # deterministic demo payload (vendor-shaped) so the full pipeline
            # (client -> mapper -> service persist) exercises end-to-end.
            if api == "balance":
                return demo_balance(json.get("rcNumber") if json else "")
            if api in ("transactions", "transaction"):
                return demo_transactions(json.get("rcNumber") if json else "")
            if api in ("enroute", "toll-enroute", "toll_enroute"):
                return demo_toll_enroute(dict(json) if json else None)

        if not self._base_url:
            raise UlipClientError("no ULIP base_url configured", api=api,
                                  category="config", client_id=client_id)

        url = f"{self._base_url}{path}"
        client = self._get_client()
        attempts = self._retries + 1
        last_reason = "unknown"
        last_status: Optional[int] = None
        last_exc: Optional[httpx.HTTPError] = None

        for attempt in range(1, attempts + 1):
            log.info(
                "fastag.ulip.request", module="fastag", stage="client", api=api,
                client_id=client_id, method=method, url=url, attempt=attempt,
                max_attempts=attempts,
            )
            t0 = time.perf_counter()
            try:
                resp = await client.request(
                    method, url, json=json, params=params,
                    headers=self._headers(client_id),
                )
            except httpx.HTTPError as exc:
                last_reason = f"transport_error: {type(exc).__name__}: {exc!s}"
                last_status = None
                last_exc = exc
                log.warning(
                    "fastag.ulip.response", module="fastag", stage="client", api=api,
                    client_id=client_id, attempt=attempt, status="error",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 1), error=str(exc),
                )
            else:
                log.info(
                    "fastag.ulip.response", module="fastag", stage="client", api=api,
                    client_id=client_id, attempt=attempt, status=resp.status_code,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                    bytes=len(resp.content),
                )
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise UlipClientError(
                            f"non-json response: {exc!s}", api=api,
                            category="bad_response", status=resp.status_code,
                            client_id=client_id,
                        )
                last_status = resp.status_code
                last_reason = f"http_{resp.status_code}"
                if resp.status_code not in _RETRYABLE_STATUS:
                    raise UlipClientError(
                        last_reason, api=api, category="http_error",
                        status=resp.status_code, client_id=client_id,
                    )

            # Backoff before the next attempt (skip after the final one). Add up to
            # 25% jitter to avoid synchronised retry storms under concurrent load.
            if attempt < attempts:
                base_delay = self._backoff_base_s * (2 ** (attempt - 1))
                await asyncio.sleep(base_delay + random.uniform(0, base_delay * 0.25))

        # Classify the exhausted failure: a timeout is distinct from a plain
        # connection failure / 5xx run so the gateway can return 504 vs 502.
        category = "timeout" if isinstance(last_exc, httpx.TimeoutException) else "unavailable"
        raise UlipClientError(
            f"exhausted {attempts} attempts ({last_reason})",
            api=api, category=category, status=last_status, client_id=client_id,
        )

    # -- public API (one method per ULIP FASTag call) ------------------------
    async def toll_enroute(self, payload: Mapping[str, Any], *, client_id: str) -> Any:
        """POST Toll Enroute. ``payload`` carries source/destination/vehicle_type."""
        return await self._request(
            "POST", self.ENROUTE_PATH, api="enroute", client_id=client_id, json=payload,
        )

    async def balance(self, rc_number: str, *, client_id: str) -> Any:
        """POST RC -> FASTag Balance for a single registration number."""
        return await self._request(
            "POST", self.BALANCE_PATH, api="balance", client_id=client_id,
            json={"rcNumber": rc_number},
        )

    async def transactions(
        self, rc_number: str, *, client_id: str,
        from_date: Optional[str] = None, to_date: Optional[str] = None,
    ) -> Any:
        """POST RC -> FASTag Transaction. Optional date window is passed through."""
        body: dict[str, Any] = {"rcNumber": rc_number}
        if from_date is not None:
            body["fromDate"] = from_date
        if to_date is not None:
            body["toDate"] = to_date
        return await self._request(
            "POST", self.TRANSACTION_PATH, api="transaction", client_id=client_id, json=body,
        )


__all__ = ["UlipFastagClient", "UlipClientError", "CORRELATION_HEADER"]
