"""Bhuvan WMS integration tests (no DB / no network required).

Covers the required scenarios:
  * WMS health success           (GetCapabilities parsed, layers surfaced)
  * WMS unavailable              (network error / 5xx retried, typed error)
  * invalid layer                (typed BhuvanLayerNotFound, not a crash)
  * timeout handling             (client retries then raises BhuvanTimeout)
plus the capabilities parser (1.1.1 + 1.3.0 + ServiceExceptionReport), the
gateway config wiring (BHUVAN_* env vars) and the router degradation posture
(same pattern as tests/test_openaq.py — provider down NEVER breaks /api/bhuvan).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from integrations.bhuvan import (
    BhuvanClient,
    BhuvanHTTPError,
    BhuvanInvalidResponse,
    BhuvanLayerNotFound,
    BhuvanTimeout,
    BhuvanUnavailable,
    parse_capabilities,
)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- canned payloads
CAPS_111 = """<?xml version="1.0" encoding="UTF-8"?>
<WMT_MS_Capabilities version="1.1.1">
  <Service>
    <Name>OGC:WMS</Name>
    <Title>Bhuvan Web Map Service</Title>
  </Service>
  <Capability>
    <Layer>
      <Title>Bhuvan Layers</Title>
      <Layer queryable="1">
        <Name>india3</Name>
        <Title>India Base Mosaic</Title>
      </Layer>
      <Layer>
        <Name>lulc:MH_LULC50K_1112</Name>
        <Title>Maharashtra LULC 50K</Title>
      </Layer>
    </Layer>
  </Capability>
</WMT_MS_Capabilities>
"""

CAPS_130 = """<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Service><Title>Bhuvan WMS 1.3.0</Title></Service>
  <Capability>
    <Layer>
      <Layer queryable="1">
        <Name>india3</Name>
        <Title>India Base Mosaic</Title>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

EXCEPTION_REPORT = """<?xml version="1.0"?>
<ServiceExceptionReport version="1.1.1">
  <ServiceException>msWMSLoadGetMapParams(): unknown request</ServiceException>
</ServiceExceptionReport>
"""


def _client_with(handler, **kwargs) -> BhuvanClient:
    transport = httpx.MockTransport(handler)
    return BhuvanClient(http_client=httpx.AsyncClient(transport=transport),
                        retries=2, backoff_s=0.0, **kwargs)


def _caps_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["service"] == "WMS"
    assert request.url.params["request"] == "GetCapabilities"
    return httpx.Response(200, text=CAPS_111)


# ----------------------------------------------------------------- parser
def test_parse_capabilities_111():
    caps = parse_capabilities(CAPS_111)
    assert caps.version == "1.1.1"
    assert caps.service_title == "Bhuvan Web Map Service"
    # Only NAMED layers are requestable; the unnamed group layer is skipped.
    assert [l.name for l in caps.layers] == ["india3", "lulc:MH_LULC50K_1112"]
    assert caps.layers[0].queryable is True
    assert caps.find_layer("INDIA3").title == "India Base Mosaic"
    assert caps.layers[0].as_api_dict() == {
        "name": "india3", "title": "India Base Mosaic", "type": "WMS"}


def test_parse_capabilities_130_namespaced():
    caps = parse_capabilities(CAPS_130)
    assert caps.version == "1.3.0"
    assert [l.name for l in caps.layers] == ["india3"]


def test_parse_capabilities_rejects_non_xml():
    with pytest.raises(BhuvanInvalidResponse):
        parse_capabilities("<html>gateway splash page</html>")
    with pytest.raises(BhuvanInvalidResponse):
        parse_capabilities("not xml at all {}")


def test_parse_capabilities_rejects_service_exception():
    with pytest.raises(BhuvanInvalidResponse) as exc:
        parse_capabilities(EXCEPTION_REPORT)
    assert "ServiceExceptionReport" in str(exc.value)


# ----------------------------------------------------------------- client: happy
def test_client_health_success():
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        return _caps_handler(request)

    client = _client_with(handler)
    caps = _run(client.check_availability())
    assert seen["n"] == 1
    assert caps.find_layer("india3") is not None
    assert client.configured is True


def test_client_validate_layer_success_and_invalid():
    client = _client_with(lambda req: httpx.Response(200, text=CAPS_111))
    layer = _run(client.validate_layer("india3"))
    assert layer.title == "India Base Mosaic"
    # invalid layer -> typed error, not a crash
    with pytest.raises(BhuvanLayerNotFound):
        _run(client.validate_layer("no_such_layer"))


# ----------------------------------------------------------------- client: sad
def test_client_unavailable_network_error_is_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(BhuvanUnavailable):
        _run(_client_with(handler).fetch_capabilities())
    assert attempts["n"] == 3          # first try + 2 retries


def test_client_5xx_is_retried_then_typed():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="maintenance")

    with pytest.raises(BhuvanHTTPError) as exc:
        _run(_client_with(handler).fetch_capabilities())
    assert exc.value.status_code == 503
    assert attempts["n"] == 3


def test_client_4xx_fails_fast_no_retry():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(403, text="blocked")

    with pytest.raises(BhuvanHTTPError) as exc:
        _run(_client_with(handler).fetch_capabilities())
    assert exc.value.status_code == 403
    assert attempts["n"] == 1


def test_client_timeout_is_retried_then_typed():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(BhuvanTimeout):
        _run(_client_with(handler).fetch_capabilities())
    assert attempts["n"] == 3


# ----------------------------------------------------------------- env wiring
def test_client_env_configuration(monkeypatch):
    monkeypatch.setenv("BHUVAN_WMS_URL", "https://proxy.example/bhuvan/wms")
    monkeypatch.setenv("BHUVAN_LAYER", "lulc:MH_LULC50K_1112")
    monkeypatch.setenv("BHUVAN_TIMEOUT_S", "2.5")
    monkeypatch.setenv("BHUVAN_RETRIES", "0")
    client = BhuvanClient()
    assert client.wms_url == "https://proxy.example/bhuvan/wms"
    assert client.default_layer == "lulc:MH_LULC50K_1112"
    assert client.timeout_s == 2.5
    assert client.retries == 0


def test_client_defaults_without_env(monkeypatch):
    for var in ("BHUVAN_WMS_URL", "BHUVAN_LAYER", "BHUVAN_TIMEOUT_S", "BHUVAN_RETRIES"):
        monkeypatch.delenv(var, raising=False)
    client = BhuvanClient()
    assert client.wms_url == "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms"
    assert client.default_layer == "india3"
    assert client.configured is True


def test_gateway_config_reads_bhuvan_env(monkeypatch):
    from gateway.config import GatewayConfig

    monkeypatch.setenv("BHUVAN_WMS_URL", "https://proxy.example/wms")
    monkeypatch.setenv("BHUVAN_LAYER", "india3")
    monkeypatch.setenv("BHUVAN_ENABLED", "false")
    cfg = GatewayConfig.from_env()
    assert cfg.bhuvan_wms_url == "https://proxy.example/wms"
    assert cfg.bhuvan_layer == "india3"
    assert cfg.bhuvan_enabled is False

    monkeypatch.delenv("BHUVAN_ENABLED")
    assert GatewayConfig.from_env().bhuvan_enabled is True   # default on


# ----------------------------------------------------------------- router posture
class _StubRequest:
    """Minimal Request stand-in: only .app.state.gw.cfg is read by the router."""

    def __init__(self, cfg=None):
        class _App:
            pass
        self.app = _App()
        self.app.state = _App()
        if cfg is not None:
            gw = _App()
            gw.cfg = cfg
            self.app.state.gw = gw


def test_router_health_available_and_layers_live():
    from gateway.routers import bhuvan as bhuvan_router

    client = _client_with(lambda req: httpx.Response(200, text=CAPS_111))
    req = _StubRequest()

    health = _run(bhuvan_router.bhuvan_health(req, client=client))
    assert health["system"] == "BHUVAN_WMS"
    assert health["provider"] == "ISRO_NRSC"
    assert health["configured"] is True
    assert health["status"] == "AVAILABLE"
    assert health["default_layer_advertised"] is True

    layers = _run(bhuvan_router.bhuvan_layers(req, limit=50, client=client))
    assert layers["provider"] == "BHUVAN"
    assert layers["source"] == "LIVE"
    # configured default layer is listed first
    assert layers["layers"][0] == {
        "name": "india3", "title": "India Base Mosaic", "type": "WMS"}
    assert all(l["type"] == "WMS" for l in layers["layers"])


def test_router_degrades_when_wms_unavailable():
    """Provider down -> health UNAVAILABLE + layers fall back to the
    CONFIGURED default entry; neither surface ever raises."""
    from gateway.routers import bhuvan as bhuvan_router

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client_with(down)
    req = _StubRequest()

    health = _run(bhuvan_router.bhuvan_health(req, client=client))
    assert health["status"] == "UNAVAILABLE"
    assert health["configured"] is True

    layers = _run(bhuvan_router.bhuvan_layers(req, limit=50, client=client))
    assert layers["source"] == "CONFIGURED"
    assert layers["layers"] == [
        {"name": "india3", "title": "india3", "type": "WMS"}]


def test_router_layers_advertises_proxy_url():
    """The frontend must be pointed at the same-origin relay — Bhuvan sends no
    CORS headers, so the browser can never call the raw wms_url."""
    from gateway.routers import bhuvan as bhuvan_router

    client = _client_with(lambda req: httpx.Response(200, text=CAPS_111))
    req = _StubRequest()
    layers = _run(bhuvan_router.bhuvan_layers(req, limit=50, client=client))
    assert layers["proxy_url"] == "/api/bhuvan/wms"


class _RelayRequest(_StubRequest):
    """Stub with the query_params the /wms relay reads."""

    def __init__(self, params, cfg=None):
        super().__init__(cfg=cfg)
        self.query_params = params


def test_wms_relay_forwards_getmap():
    from gateway.routers import bhuvan as bhuvan_router

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, content=b"\x89PNGfake",
                              headers={"content-type": "image/png"})

    client = _client_with(handler)
    req = _RelayRequest({
        "SERVICE": "WMS", "REQUEST": "GetMap", "LAYERS": "india3",
        "BBOX": "0,0,1,1", "WIDTH": "256", "HEIGHT": "256",
        "FORMAT": "image/png", "evil_param": "1",
    })
    resp = _run(bhuvan_router.bhuvan_wms(req, client=client))
    assert resp.status_code == 200
    assert resp.media_type == "image/png"
    assert resp.body == b"\x89PNGfake"
    assert seen["LAYERS"] == "india3"
    assert "evil_param" not in seen          # non-whitelisted params dropped


def test_wms_relay_rejects_bad_requests():
    from gateway.routers import bhuvan as bhuvan_router

    client = _client_with(lambda req: httpx.Response(200, content=b"x"))
    # unsupported request type (GetFeatureInfo) and missing request type
    for params in ({"request": "GetFeatureInfo"}, {"service": "WMS"}):
        resp = _run(bhuvan_router.bhuvan_wms(_RelayRequest(params), client=client))
        assert resp.status_code == 400
    # oversized image dimension
    resp = _run(bhuvan_router.bhuvan_wms(
        _RelayRequest({"request": "GetMap", "width": "9999", "height": "256"}),
        client=client))
    assert resp.status_code == 400


def test_wms_relay_maps_getmap_failures():
    from gateway.config import GatewayConfig
    from gateway.routers import bhuvan as bhuvan_router

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    resp = _run(bhuvan_router.bhuvan_wms(
        _RelayRequest({"request": "GetMap", "width": "256"}),
        client=_client_with(timeout)))
    assert resp.status_code == 504           # provider timeout -> gateway timeout

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    resp = _run(bhuvan_router.bhuvan_wms(
        _RelayRequest({"request": "GetMap"}), client=_client_with(down)))
    assert resp.status_code == 502           # provider unreachable -> bad gateway

    # disabled -> 503, upstream never called
    resp = _run(bhuvan_router.bhuvan_wms(
        _RelayRequest({"request": "GetMap"}, cfg=GatewayConfig(bhuvan_enabled=False)),
        client=_client_with(down)))
    assert resp.status_code == 503


def test_wms_relay_capabilities_synthetic_fallback():
    """NRSC's GetCapabilities routinely hangs even while GetMap serves, and
    the ArcGIS WMSLayer cannot load() without a capabilities answer — so the
    relay degrades GetCapabilities to a SYNTHETIC minimal document (200)
    advertising the configured layer, never a 5xx."""
    from gateway.routers import bhuvan as bhuvan_router

    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("capabilities hang", request=request)

    resp = _run(bhuvan_router.bhuvan_wms(
        _RelayRequest({"service": "WMS", "request": "GetCapabilities"}),
        client=_client_with(hang)))
    assert resp.status_code == 200
    assert resp.headers["x-bhuvan-source"] == "SYNTHETIC"
    # The synthetic document must be a valid WMS capabilities answer that our
    # own parser (and therefore ArcGIS's) accepts, naming the configured layer.
    caps = parse_capabilities(resp.body.decode())
    assert caps.version == "1.1.1"
    assert caps.find_layer("india3") is not None

    # A live (non-failing) capabilities pass-through stays verbatim.
    resp = _run(bhuvan_router.bhuvan_wms(
        _RelayRequest({"request": "GetCapabilities"}),
        client=_client_with(lambda req: httpx.Response(
            200, text=CAPS_111,
            headers={"content-type": "application/vnd.ogc.wms_xml"}))))
    assert resp.status_code == 200
    assert "x-bhuvan-source" not in resp.headers
    assert resp.body.decode() == CAPS_111


def test_router_disabled_via_config():
    from gateway.config import GatewayConfig
    from gateway.routers import bhuvan as bhuvan_router

    cfg = GatewayConfig(bhuvan_enabled=False)
    client = _client_with(lambda req: httpx.Response(200, text=CAPS_111))
    req = _StubRequest(cfg=cfg)

    health = _run(bhuvan_router.bhuvan_health(req, client=client))
    assert health["status"] == "DISABLED"
    layers = _run(bhuvan_router.bhuvan_layers(req, limit=50, client=client))
    assert layers["source"] == "DISABLED"
    assert layers["layers"] == []
