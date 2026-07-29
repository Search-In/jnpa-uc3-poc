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
    GET /api/bhuvan/wms     -> same-origin WMS relay (GetCapabilities/GetMap)

The /wms relay exists because the Bhuvan server sends NO CORS headers: the
browser's ArcGIS WMSLayer fetch()es both capabilities and map images, so a
direct call always dies with "Failed to fetch". The relay forwards only
whitelisted WMS parameters, only GetCapabilities/GetMap, with clamped image
dimensions and a bounded response size — the gateway never becomes an open
proxy and never serves unbounded imagery.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from ..logging import get_logger
from ..metrics import REQUESTS

from integrations.bhuvan import BhuvanClient, BhuvanError, BhuvanTimeout

log = get_logger("gateway.bhuvan")

router = APIRouter(prefix="/api/bhuvan", tags=["bhuvan"])

# The frontend layer picker only needs a handful of options; Bhuvan advertises
# hundreds of thematic layers, so the /layers answer is capped (override per
# request via ?limit=).
DEFAULT_LAYER_LIMIT = 50

# Same-origin relay path handed to the frontend (see /wms below). The ArcGIS
# WMSLayer points here instead of at nrsc.gov.in so every request stays on the
# dashboard origin — the Bhuvan server itself sends no CORS headers.
WMS_PROXY_PATH = "/api/bhuvan/wms"

# WMS 1.1.1/1.3.0 query parameters the relay will forward (case-insensitive).
# Anything else is dropped, and only GetCapabilities/GetMap request types pass.
ALLOWED_WMS_PARAMS = {
    "service", "request", "version", "layers", "styles", "srs", "crs",
    "bbox", "width", "height", "format", "transparent", "bgcolor",
    "exceptions", "time",
}
ALLOWED_WMS_REQUESTS = {"getcapabilities", "getmap"}
# Hard cap on relayed image dimensions — ArcGIS requests one image per view,
# so a normal screen stays well below this; anything larger is a misuse.
MAX_IMAGE_DIMENSION = 2048

_client: Optional[BhuvanClient] = None
_probe_client: Optional[BhuvanClient] = None


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


def get_probe_client(request: Request) -> BhuvanClient:
    """Single-attempt client for the /layers surface: the map toggle blocks on
    this answer, so when the provider is down we want ONE timeout budget before
    the CONFIGURED fallback (≈5 s), not the health probe's full retry ladder
    (≈16 s). /health keeps the retried client — accuracy over latency there."""
    global _probe_client
    if _probe_client is None:
        cfg = _cfg(request)
        _probe_client = BhuvanClient(
            wms_url=getattr(cfg, "bhuvan_wms_url", "") or None,
            default_layer=getattr(cfg, "bhuvan_layer", "") or None,
            retries=0,
        )
    return _probe_client


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
    client: BhuvanClient = Depends(get_probe_client),
) -> Dict[str, Any]:
    """Named layers from the live GetCapabilities document. When the provider
    is unreachable the answer degrades to the CONFIGURED default layer (the
    frontend can still draw the layer — GetMap may well work even when the
    capabilities endpoint is slow), never a 5xx. ``proxy_url`` is the
    same-origin relay the browser MUST use (Bhuvan sends no CORS headers)."""
    base: Dict[str, Any] = {
        "provider": "BHUVAN",
        "enabled": _enabled(request),
        "wms_url": client.wms_url,
        "proxy_url": WMS_PROXY_PATH,
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


# --------------------------------------------------------------------- relay
def _synthetic_capabilities(client: BhuvanClient) -> str:
    """Minimal WMS 1.1.1 capabilities advertising ONLY the configured layer.

    Served when the upstream GetCapabilities fails — NRSC's capabilities
    endpoint routinely hangs for >30 s even while GetMap works, and the ArcGIS
    WMSLayer refuses to load() without a capabilities document. The gateway
    already knows the layer from BHUVAN_LAYER, so the SYNTHETIC rung keeps the
    map layer loadable; actual imagery then depends only on GetMap.

    Contains exactly what @arcgis/core's parser requires: Service, Capability
    with Request/GetMap (format + DCPType href) and a root Layer with SRS +
    LatLonBoundingBox. The href value is cosmetic — the frontend pins its
    GetMap URL to the same-origin relay regardless (see
    web/src/map/BhuvanWmsLayer.ts loadBhuvanLayer).
    """
    href = xml_escape(client.wms_url, {'"': "&quot;"})
    name = xml_escape(client.default_layer)
    online = ('<OnlineResource xmlns:xlink="http://www.w3.org/1999/xlink" '
              f'xlink:href="{href}"/>')
    # India-wide bounds — generous on purpose; the map is clamped to the JNPA
    # corridor anyway, this only has to CONTAIN it.
    bbox = '<LatLonBoundingBox minx="60.0" miny="1.0" maxx="100.0" maxy="40.0"/>'
    srs = "<SRS>EPSG:4326</SRS><SRS>EPSG:3857</SRS>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<WMT_MS_Capabilities version="1.1.1">'
        "<Service><Name>OGC:WMS</Name>"
        "<Title>Bhuvan WMS (JNPA gateway synthetic fallback)</Title>"
        f"{online}</Service>"
        "<Capability><Request>"
        "<GetCapabilities><Format>application/vnd.ogc.wms_xml</Format>"
        f"<DCPType><HTTP><Get>{online}</Get></HTTP></DCPType></GetCapabilities>"
        "<GetMap><Format>image/png</Format><Format>image/jpeg</Format>"
        f"<DCPType><HTTP><Get>{online}</Get></HTTP></DCPType></GetMap>"
        "</Request>"
        "<Exception><Format>application/vnd.ogc.se_xml</Format></Exception>"
        f"<Layer><Title>Bhuvan (ISRO/NRSC)</Title>{srs}{bbox}"
        f'<Layer queryable="0"><Name>{name}</Name>'
        f"<Title>Bhuvan Satellite Layer</Title>{srs}{bbox}</Layer>"
        "</Layer></Capability></WMT_MS_Capabilities>"
    )


@router.get("/wms", summary="Same-origin Bhuvan WMS relay for the map UI")
async def bhuvan_wms(
    request: Request,
    client: BhuvanClient = Depends(get_client),
) -> Response:
    """Forward ONE whitelisted WMS request to the configured Bhuvan endpoint
    and return its body verbatim (image/png for GetMap, XML for
    GetCapabilities). Exists purely for CORS: the browser's ArcGIS WMSLayer
    cannot call nrsc.gov.in directly. The upstream URL is fixed by
    BHUVAN_WMS_URL — only query parameters pass through, so this can never be
    used as an open proxy."""
    if not _enabled(request):
        return JSONResponse({"detail": "Bhuvan WMS is disabled"}, status_code=503)

    params: Dict[str, str] = {}
    for key, value in request.query_params.items():
        if key.lower() in ALLOWED_WMS_PARAMS:
            params[key] = value
    req_type = next(
        (v for k, v in params.items() if k.lower() == "request"), "").strip().lower()
    if req_type not in ALLOWED_WMS_REQUESTS:
        return JSONResponse(
            {"detail": f"unsupported WMS request type '{req_type or '(none)'}'; "
                       "allowed: GetCapabilities, GetMap"},
            status_code=400)
    for dim in ("width", "height"):
        raw = next((v for k, v in params.items() if k.lower() == dim), None)
        if raw is not None:
            try:
                if int(raw) > MAX_IMAGE_DIMENSION:
                    return JSONResponse(
                        {"detail": f"{dim} exceeds the {MAX_IMAGE_DIMENSION}px relay cap"},
                        status_code=400)
            except ValueError:
                return JSONResponse({"detail": f"invalid {dim} '{raw}'"},
                                    status_code=400)

    try:
        body, content_type = await client.relay_wms(params)
    except BhuvanError as exc:
        REQUESTS.labels("bhuvan", "error").inc()
        # GetCapabilities has a SYNTHETIC rung: NRSC's capabilities endpoint
        # routinely hangs even while GetMap serves, and ArcGIS cannot load()
        # a WMSLayer without a capabilities answer. The configured layer is
        # known, so serve a minimal document instead of failing the load.
        if req_type == "getcapabilities":
            log.warning("bhuvan_capabilities_synthetic", error=str(exc))
            return Response(
                content=_synthetic_capabilities(client),
                media_type="application/vnd.ogc.wms_xml",
                headers={"X-Bhuvan-Source": "SYNTHETIC",
                         "Cache-Control": "no-store"})
        # GetMap has no synthetic substitute — surface a typed gateway error.
        status = 504 if isinstance(exc, BhuvanTimeout) else 502
        return JSONResponse({"detail": str(exc)}, status_code=status)
    REQUESTS.labels("bhuvan", "ok").inc()
    # Short client-side cache keeps pan/zoom re-requests off the NRSC server.
    return Response(content=body, media_type=content_type,
                    headers={"Cache-Control": "public, max-age=300"})
