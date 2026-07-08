"""Gate/customs source providers — SIM | LIVE adapter (Phase 2).

Each of the four gate source systems (e-Seal, Form-13, Weighbridge, ICEGATE) is
fronted by a per-source mode: ``sim`` (the deterministic seed corpus, the PoC
default) or ``live`` (a real vendor/EDI endpoint). The mode + endpoint are read
from the environment PER SOURCE, so real endpoints plug in one at a time with no
redesign:

    GATE_ESEAL_MODE=live        GATE_ESEAL_URL=https://eseal.vendor/api
    GATE_FORM13_MODE=live       GATE_FORM13_URL=https://form13.jnpa/api
    GATE_ICEGATE_MODE=live      GATE_ICEGATE_URL=https://icegate.gov.in/edi
    GATE_WEIGHBRIDGE_MODE=live  GATE_WEIGHBRIDGE_URL=http://weighbridge-plc/api

In LIVE mode a configured source is fetched over HTTP and the full
request/response is logged to ``jnpa.api_audit_log`` (the deepest hop the gateway
cannot see). If a source is set LIVE but its URL is absent, it degrades to SIM
for that source (logged), so the service always serves.

The seam is intentionally thin: `capture_source()` returns a normalized
``CaptureRecord`` regardless of provider, and the app persists it to
``jnpa.gate_captures`` tagged with the actual ``source_mode`` used.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple

from jnpa_shared.logging import get_logger

from . import persistence
from . import icegate_sim

log = get_logger("gate_data.providers")

SOURCES = ("ESEAL", "FORM13", "WEIGHBRIDGE", "ICEGATE")


def source_mode(source: str) -> str:
    """Resolved mode for a source: 'live' only if MODE=live AND a URL is set."""
    mode = os.environ.get(f"GATE_{source}_MODE", "sim").strip().lower()
    if mode == "live" and not source_url(source):
        return "sim"  # live requested but unconfigured -> safe SIM fallback
    return "live" if mode == "live" else "sim"


def source_url(source: str) -> str:
    return os.environ.get(f"GATE_{source}_URL", "").strip()


def providers_status() -> Dict[str, Dict[str, Any]]:
    """Per-source mode + configured flag (for /healthz + the dashboard badge)."""
    return {
        s: {
            "mode": source_mode(s),
            "requested": os.environ.get(f"GATE_{s}_MODE", "sim").strip().lower(),
            "url_configured": bool(source_url(s)),
        }
        for s in SOURCES
    }


def _seed_source_dict(source: str, rec) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    """Normalize one seeded source record -> (payload, status, captured_at).

    Every source gets a deterministic non-null captured_at so the idempotency key
    (container, type, captured_at) dedups on re-boot — Postgres treats NULL as
    DISTINCT in a UNIQUE index, so a null timestamp would duplicate each run.
    Form-13 / ICEGATE carry no native timestamp, so they inherit the e-seal
    capture time (the gate-pass instant for that container).
    """
    anchor = rec.eseal.captured_at  # deterministic per container
    if source == "ESEAL":
        d = asdict(rec.eseal)
        return d, d.get("status"), d.get("captured_at")
    if source == "FORM13":
        d = asdict(rec.form13)
        return d, "REGISTERED", anchor
    if source == "WEIGHBRIDGE":
        d = asdict(rec.weighbridge)
        return d, "WEIGHED", d.get("captured_at")
    # ICEGATE
    d = icegate_sim.icegate_message(rec.icegate)
    return d, rec.icegate.leo_status, anchor


async def _fetch_live(source: str, container_no: str, dsn: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fetch one source from its LIVE endpoint and audit-log the call.

    Returns the response JSON (dict) or None on any failure (caller falls back to
    SIM). httpx is imported lazily so the SIM-only image needn't ship it.
    """
    url = source_url(source)
    if not url:
        return None
    endpoint = f"{url.rstrip('/')}/{container_no}"
    try:
        import httpx
    except Exception:  # noqa: BLE001
        log.warning("live_httpx_unavailable", source=source)
        return None
    t0 = time.perf_counter()
    req = {"container_no": container_no}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(endpoint)
        latency = (time.perf_counter() - t0) * 1000.0
        body: Any
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"_raw": resp.text[:4096]}
        await persistence.log_api_audit(
            service_name=f"gate-{source.lower()}", endpoint=f"GET {endpoint}",
            method="GET", request_payload=req, response_payload=body,
            status_code=resp.status_code, latency_ms=latency,
            error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
            transaction_id=container_no, dsn=dsn,
        )
        return body if resp.status_code < 400 and isinstance(body, dict) else None
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - t0) * 1000.0
        await persistence.log_api_audit(
            service_name=f"gate-{source.lower()}", endpoint=f"GET {endpoint}",
            method="GET", request_payload=req, response_payload={}, status_code=None,
            latency_ms=latency, error=f"{type(exc).__name__}: {exc}",
            transaction_id=container_no, dsn=dsn,
        )
        log.warning("live_fetch_failed", source=source, error=str(exc))
        return None


async def capture_source(
    source: str, container_no: str, seed_rec, *, dsn: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[str], Optional[str], str]:
    """Return (payload, status, captured_at, mode_used) for one source.

    LIVE (configured) fetches the vendor endpoint (audit-logged); anything else
    uses the deterministic seed record. Always returns a usable capture.
    """
    mode = source_mode(source)
    if mode == "live":
        body = await _fetch_live(source, container_no, dsn)
        if body is not None:
            return body, body.get("status"), body.get("captured_at"), "live"
        # configured-live fetch failed -> degrade to SIM for this capture.
        log.info("live_degraded_to_sim", source=source, container=container_no)
    payload, status, captured_at = _seed_source_dict(source, seed_rec)
    return payload, status, captured_at, "sim"


__all__ = ["SOURCES", "source_mode", "source_url", "providers_status", "capture_source"]
