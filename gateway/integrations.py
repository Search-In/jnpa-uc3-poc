"""External-system integration layer (PDP / LDB / RMS-TAS / NVR / Weather).

A single seam that every enterprise-system adapter goes through so the LIVE-vs-MOCK
posture is explicit and auditable — never a silent hardcode. Mirrors the existing
FASTag/ULIP pattern (real client, demo fallback, health flag):

  * If the system's base URL (+ optional api key) is configured in the environment
    the adapter performs a REAL HTTP call.
  * Otherwise it returns a deterministic MOCK payload, clearly tagged
    ``source="MOCK"``, and a health endpoint reports ``configured=false`` so the
    external dependency is visible, not pretended-away.
  * Every call (live or mock) is logged to jnpa.integration_lookups with its
    source + latency for evidence.

Config env vars (all optional; unset => MOCK):
    PDP_BASE_URL / PDP_API_KEY
    LDB_BASE_URL / LDB_API_KEY
    RMS_TAS_BASE_URL / RMS_TAS_API_KEY
    NVR_BASE_URL / NVR_API_KEY
    WEATHER_BASE_URL / WEATHER_API_KEY
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from .logging import get_logger

log = get_logger("gateway.integrations")


@dataclass
class SystemConfig:
    name: str
    base_url: str
    api_key: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


def system_config(name: str) -> SystemConfig:
    """Read a system's LIVE config from the environment (unset => MOCK)."""
    prefix = name.upper().replace("-", "_")
    return SystemConfig(
        name=name,
        base_url=os.environ.get(f"{prefix}_BASE_URL", "").strip(),
        api_key=os.environ.get(f"{prefix}_API_KEY", "").strip(),
    )


def health(name: str) -> Dict[str, Any]:
    cfg = system_config(name)
    return {"system": name, "configured": cfg.configured,
            "mode": "LIVE" if cfg.configured else "MOCK",
            "base_url_set": bool(cfg.base_url), "api_key_set": bool(cfg.api_key)}


async def _audit(dsn: Optional[str], *, system: str, op: str, ref: Optional[str],
                 request: Dict[str, Any], response: Dict[str, Any], source: str,
                 latency_ms: int) -> None:
    if not dsn:
        return
    import json as _json
    try:
        from jnpa_shared.db import execute
        await execute(
            """INSERT INTO jnpa.integration_lookups
                 (system, op, ref, request, response, source, latency_ms)
               VALUES (:sys, :op, :ref, CAST(:req AS jsonb), CAST(:resp AS jsonb), :src, :lat)""",
            {"sys": system, "op": op, "ref": ref,
             "req": _json.dumps(request)[:8000], "resp": _json.dumps(response)[:8000],
             "src": source, "lat": latency_ms},
            dsn=dsn)
    except Exception as exc:  # noqa: BLE001 - audit is best-effort
        log.debug("integration_audit_failed", system=system, error=str(exc))


async def call(
    *,
    system: str,
    op: str,
    ref: Optional[str],
    request: Dict[str, Any],
    live_path: Optional[str],
    mock_fn: Callable[[], Dict[str, Any]],
    dsn: Optional[str] = None,
    method: str = "GET",
    http_client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Perform one adapter call. Returns ``{"source": "LIVE"|"MOCK"|"ERROR", "data": {...}}``.

    ``live_path`` is appended to the system base URL for the real call; pass None to
    force the mock branch. ``mock_fn`` builds the deterministic fallback payload.
    """
    cfg = system_config(system)
    t0 = time.perf_counter()

    if cfg.configured and live_path:
        url = cfg.base_url.rstrip("/") + live_path
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        client = http_client or httpx.AsyncClient(timeout=8.0)
        owns = http_client is None
        try:
            if method.upper() == "POST":
                resp = await client.post(url, json=request, headers=headers)
            else:
                resp = await client.get(url, params=request, headers=headers)
            latency = int((time.perf_counter() - t0) * 1000)
            if resp.status_code == 200:
                data = resp.json()
                await _audit(dsn, system=system, op=op, ref=ref, request=request,
                             response=data if isinstance(data, dict) else {"data": data},
                             source="LIVE", latency_ms=latency)
                return {"source": "LIVE", "data": data}
            log.warning("integration_live_non200", system=system, status=resp.status_code)
        except Exception as exc:  # noqa: BLE001 - live down => fall back to mock
            log.warning("integration_live_failed", system=system, error=str(exc))
        finally:
            if owns:
                await client.aclose()

    # MOCK branch (unconfigured or live failed).
    data = mock_fn()
    latency = int((time.perf_counter() - t0) * 1000)
    await _audit(dsn, system=system, op=op, ref=ref, request=request,
                 response=data if isinstance(data, dict) else {"data": data},
                 source="MOCK", latency_ms=latency)
    return {"source": "MOCK", "data": data}
