"""/api/bhuvan — Bhuvan WMS (ISRO/NRSC) geospatial-layer control plane.

ADDITIVE — same mould as gateway/routers/air_quality.py: a thin router over
:class:`integrations.bhuvan.BhuvanClient`. No API key is required; everything
is env-driven (BHUVAN_WMS_URL / BHUVAN_LAYER / BHUVAN_ENABLED). The gateway
NEVER downloads map imagery — it validates WMS availability and serves the
layer configuration; the browser renders GetMap tiles directly on the ArcGIS
map (web/src/map/BhuvanWmsLayer.ts).

A Bhuvan outage NEVER breaks these surfaces — LIVE (GetCapabilities) →
CONFIGURED (the env-declared default layer):

    GET /api/bhuvan/health  -> WMS availability posture
    GET /api/bhuvan/layers  -> named-layer metadata for the frontend
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from ..logging import get_logger
from ..metrics import REQUESTS

from integrations.bhuvan import BhuvanClient, BhuvanError

log = get_logger("gateway.bhuvan")

router = APIRouter(prefix="/api/bhuvan", tags=["bhuvan"])

# The frontend layer picker only needs a handful of options; Bhuvan advertises
# hundreds of thematic layers, so the /layers answer is capped (override per
# request via ?limit=).
DEFAULT_LAYER_LIMIT = 50

_client: Optional[BhuvanClient] = None


def _cfg(request: Request):
    return getattr(getattr(request.app.state, "gw", None), "cfg", None)


def get_client(request: Request) -> BhuvanClient:
    global _client
    if _client is None:
        cfg = _cfg(request)
        # Empty config values fall through to the client's env/built-in
        # defaults (bhuvan-vec1.nrsc.gov.in, layer "india3").
        _client = BhuvanClient(
            wms_url=getattr(cfg, "bhuvan_wms_url", "") or None,
            default_layer=getattr(cfg, "bhuvan_layer", "") or None,
        )
    return _client


def _enabled(request: Request) -> bool:
    cfg = _cfg(request)
    return bool(getattr(cfg, "bhuvan_enabled", True))


# -------------------------------------------------------------------- health
@router.get("/health", summary="Bhuvan WMS integration posture")
async def bhuvan_health(
    request: Request,
    client: BhuvanClient = Depends(get_client),
) -> Dict[str, Any]:
    """One live GetCapabilities probe. AVAILABLE / UNAVAILABLE / DISABLED —
    never a 5xx: an unreachable provider is a reported state, not an error."""
    base: Dict[str, Any] = {
        "system": "BHUVAN_WMS",
        "provider": "ISRO_NRSC",
        "configured": client.configured,
        "enabled": _enabled(request),
        "api_key_required": False,
        "wms_url": client.wms_url,
        "default_layer": client.default_layer,
        "timeout_s": client.timeout_s,
        "retries": client.retries,
    }
    if not _enabled(request):
        return {**base, "status": "DISABLED"}
    try:
        caps = await client.check_availability()
    except BhuvanError as exc:
        log.warning("bhuvan_health_probe_failed", error=str(exc))
        REQUESTS.labels("bhuvan", "error").inc()
        return {**base, "status": "UNAVAILABLE", "detail": str(exc)}
    REQUESTS.labels("bhuvan", "ok").inc()
    return {
        **base,
        "status": "AVAILABLE",
        "wms_version": caps.version,
        "service_title": caps.service_title,
        "layer_count": len(caps.layers),
        "default_layer_advertised": caps.find_layer(client.default_layer) is not None,
    }


# -------------------------------------------------------------------- layers
@router.get("/layers", summary="Bhuvan WMS layer metadata for the map UI")
async def bhuvan_layers(
    request: Request,
    limit: int = Query(DEFAULT_LAYER_LIMIT, ge=1, le=500),
    client: BhuvanClient = Depends(get_client),
) -> Dict[str, Any]:
    """Named layers from the live GetCapabilities document. When the provider
    is unreachable the answer degrades to the CONFIGURED default layer (the
    frontend can still draw the layer — GetMap may well work even when the
    capabilities endpoint is slow), never a 5xx."""
    base: Dict[str, Any] = {
        "provider": "BHUVAN",
        "enabled": _enabled(request),
        "wms_url": client.wms_url,
        "default_layer": client.default_layer,
    }
    if not _enabled(request):
        return {**base, "source": "DISABLED", "layers": []}
    try:
        caps = await client.fetch_capabilities()
    except BhuvanError as exc:
        log.warning("bhuvan_layers_degraded", error=str(exc))
        REQUESTS.labels("bhuvan", "error").inc()
        return {
            **base,
            "source": "CONFIGURED",
            "detail": str(exc),
            "layers": [{"name": client.default_layer,
                        "title": client.default_layer,
                        "type": "WMS"}],
        }
    REQUESTS.labels("bhuvan", "ok").inc()
    # The configured default layer is always listed first when advertised.
    ordered = sorted(
        caps.layers,
        key=lambda l: (l.name.strip().lower() != client.default_layer.strip().lower(),),
    )
    return {
        **base,
        "source": "LIVE",
        "wms_version": caps.version,
        "total_advertised": len(caps.layers),
        "layers": [l.as_api_dict() for l in ordered[:limit]],
    }
