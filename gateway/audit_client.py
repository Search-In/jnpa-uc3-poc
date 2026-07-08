"""API audit middleware — an httpx client that logs every outbound call.

``AuditingAsyncClient`` is a drop-in ``httpx.AsyncClient`` used by ``GatewayState``.
It transparently records EVERY external API request/response to
``jnpa.api_audit_log`` (via gateway.audit.log_api_audit) with:

    request payload · response payload · status code · latency (ms) · error

This is the "common middleware" for the integration audit trail: because every
upstream hop (Vahan/Sarathi/FASTag via vahan-live, ULIP relay, gate-data for
e-Seal/Form-13/ICEGATE/Weighbridge, parking, carbon, empty-container) is proxied
through this one client, wiring it here gives automatic, uniform coverage — no
per-router changes. Services that make their OWN egress hop to a government
endpoint (e.g. vahan-live -> Surepass) reuse the same ``log_api_audit`` helper so
the deepest hop is captured too once real endpoints are provisioned.

Guarantees:
* **Never changes request behaviour.** Auditing is fire-and-forget; a DB outage
  cannot slow or break a proxied call. On a transport exception the error is
  logged to the audit table and the original exception is re-raised unchanged.
* **Low noise.** Health/metrics/observability probes are skipped by default.
* **Bounded payloads.** Bodies are truncated so a large upload/response can't
  bloat the audit row.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from . import audit
from .logging import get_logger

log = get_logger("gateway.audit_client")

# Cap stored bodies (characters) so the audit row stays lean.
_MAX_BODY = 8192

# Paths that are pure infra/observability noise — not "external API calls".
_SKIP_SUFFIXES = ("/healthz", "/health", "/metrics", "/ready", "/livez", "/readyz")

# Map an upstream host (docker service name / RDS-style host) to a logical
# integration/service name for the audit row. Falls back to the hostname.
_HOST_SERVICE = {
    "vahan-live": "vahan",
    "vahan-sim": "vahan",
    "anpr": "anpr",
    "congestion": "congestion",
    "anomaly": "anomaly",
    "truck-sim": "trucking",
    "scenarios": "scenarios",
    "empty-container": "empty-container",
    "carbon": "carbon",
    "gate-data": "gate-data",
    "identity": "identity",
    "parking": "parking",
}


def _service_for(url: httpx.URL, header_override: Optional[str]) -> str:
    if header_override:
        return header_override
    host = url.host or ""
    if host in _HOST_SERVICE:
        return _HOST_SERVICE[host]
    # ulip / surepass / here / osrm etc. — derive a stable name from the host.
    return host or "unknown"


def _skip(url: httpx.URL) -> bool:
    path = url.path or ""
    return any(path.endswith(sfx) for sfx in _SKIP_SUFFIXES)


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_BODY else text[:_MAX_BODY] + "…[truncated]"


def _decode_body(raw: Optional[bytes]) -> Any:
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {"_bytes": len(raw)}
    text = _truncate(text)
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {"_raw": text}


class AuditingAsyncClient(httpx.AsyncClient):
    """httpx.AsyncClient that persists every request/response to api_audit_log."""

    def __init__(self, *args: Any, audit_dsn: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._audit_dsn = audit_dsn

    async def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        if _skip(request.url):
            return await super().send(request, *args, **kwargs)

        service = _service_for(request.url, request.headers.get("X-Audit-Service"))
        txn = request.headers.get("X-Correlation-ID") or request.headers.get("X-Request-ID")
        endpoint = f"{request.method} {urlsplit(str(request.url)).path or '/'}"
        req_body = _decode_body(request.content)
        started = time.perf_counter()
        try:
            response = await super().send(request, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — transport error (timeout/DNS/refused)
            latency_ms = (time.perf_counter() - started) * 1000.0
            audit.spawn(
                audit.log_api_audit(
                    service_name=service, endpoint=endpoint, method=request.method,
                    request_payload=req_body, response_payload={}, status_code=None,
                    latency_ms=latency_ms, error=f"{type(exc).__name__}: {exc}",
                    transaction_id=txn, dsn=self._audit_dsn,
                )
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        # Read the body without consuming the stream for the caller.
        try:
            await response.aread()
            resp_body = _decode_body(response.content)
        except Exception:  # noqa: BLE001
            resp_body = {}
        audit.spawn(
            audit.log_api_audit(
                service_name=service, endpoint=endpoint, method=request.method,
                request_payload=req_body, response_payload=resp_body,
                status_code=response.status_code, latency_ms=latency_ms,
                error=None if response.status_code < 400 else f"HTTP {response.status_code}",
                transaction_id=txn, dsn=self._audit_dsn,
            )
        )
        return response


__all__ = ["AuditingAsyncClient"]
