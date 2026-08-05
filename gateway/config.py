"""Service configuration for the API gateway.

Reads from the process environment (compose / .env.local), falling back to PoC
defaults so the gateway runs out of the box. Mirrors the convention used by the
other services (``SimConfig.from_env()`` etc.).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from jnpa_shared.config import get_settings


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class GatewayConfig:
    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Upstream services (reachable on the jnpa network) ---
    anpr_ai_url: str = "http://anpr:8301"
    vahan_sim_url: str = "http://vahan-sim:8201"
    vahan_live_url: str = "http://vahan-live:8202"
    truck_api_url: str = "http://truck-sim:8240"
    congestion_url: str = "http://congestion:8311"
    anomaly_url: str = "http://anomaly:8321"
    scenarios_url: str = "http://scenarios:8400"
    ulip_url: str = ""  # ULIP relay; empty -> mock relay used (SECONDARY)

    # --- Appendix-C capability services (proxied with synthetic fallback) ---
    empty_container_url: str = "http://empty-container:8330"
    carbon_url: str = "http://carbon:8340"
    gate_data_url: str = "http://gate-data:8350"
    identity_url: str = "http://identity:8360"
    parking_url: str = "http://parking:8370"
    # EIR / gate-slip OCR (Tesseract). Validated against the four real WhatsApp
    # gate slips; /api/ocr/document routes image uploads here for a REAL field
    # read and falls back to the local extractor when it is unreachable.
    eir_ocr_url: str = "http://eir-ocr:8210"

    # --- Infra ---
    postgres_dsn: str = ""
    redis_url: str = ""
    kafka_brokers: str = ""
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883

    # --- Credentials that drive fallback decisions ---
    surepass_api_token: str = ""
    ulip_api_key: str = ""

    # --- Upstream call budget ---
    upstream_timeout_s: float = 2.0          # >2 s lag => not LIVE for ANPR
    anpr_lag_threshold_s: float = 2.0

    # --- Cache TTLs (seconds) ---
    cache_ttl_vahan_s: int = 12 * 3600       # 12 h (spec)
    cache_ttl_anpr_s: int = 60               # last 60 s of frames (spec)
    cache_ttl_traffic_s: int = 90
    cache_ttl_default_s: int = 300
    cache_ttl_weather_s: int = 600           # Open-Meteo CACHED fallback rung

    # --- Open-Meteo Weather + Marine (/api/weather) ---
    # Free public APIs — no account, no API key. Empty -> the client's public
    # defaults (api.open-meteo.com / marine-api.open-meteo.com); set to point at
    # a proxy / self-hosted instance. NO hardcoded vendor URL in business code.
    open_meteo_weather_url: str = ""
    open_meteo_marine_url: str = ""

    # --- OpenWeatherMap (/api/weather openweather block) ---
    # BACKEND-ONLY credential: read from the environment, sent only to
    # api.openweathermap.org — never exposed to the frontend (no VITE_ var, no
    # browser call). Empty key -> provider disabled and the weather surface
    # behaves exactly as the Open-Meteo-only build. URL empty -> the client's
    # official default (api.openweathermap.org/data/2.5/weather); set to point
    # at a proxy. NO hardcoded vendor URL in business code.
    openweather_api_key: str = ""
    openweather_url: str = ""

    # --- TomTom Traffic (/api/traffic/current) ---
    # BACKEND-ONLY credential: read from the environment, sent only to
    # api.tomtom.com — never exposed to the frontend (no VITE_ var, no browser
    # call). Empty key -> provider disabled and /api/traffic/current degrades
    # through its CACHED/DATABASE/SYNTHETIC rungs. URLs empty -> the client's
    # official defaults (traffic flowSegmentData v4 / incidentDetails v5 /
    # routing v1); set to point at a proxy. NO hardcoded vendor URL in
    # business code.
    tomtom_api_key: str = ""
    tomtom_flow_url: str = ""
    tomtom_incidents_url: str = ""
    tomtom_routing_url: str = ""
    cache_ttl_tomtom_s: int = 120            # TomTom CACHED fallback rung

    # --- Bhuvan WMS (ISRO/NRSC geospatial layer, /api/bhuvan) ---
    # OGC WMS map service — NO API key required. The gateway is control-plane
    # only: it validates availability (GetCapabilities) and serves the layer
    # configuration; the browser renders the WMS tiles directly on the ArcGIS
    # map. Empty URL/layer -> the client's official defaults
    # (bhuvan-vec1.nrsc.gov.in/bhuvan/wms, layer "india3"); set to point at a
    # proxy or a different Bhuvan layer. BHUVAN_ENABLED=false hides the layer
    # from the frontend without touching code.
    bhuvan_wms_url: str = ""
    bhuvan_layer: str = ""
    bhuvan_enabled: bool = True

    # --- ULIP Logistics Intelligence (/api/logistics/*) ---
    # BACKEND-ONLY credentials: read from the environment, sent only to the
    # ULIP platform (DPIIT) — never exposed to the frontend (no VITE_ var, no
    # browser call). Auth: either ULIP_CLIENT_ID + ULIP_CLIENT_SECRET (POST
    # /user/login token flow) or the pre-issued static ULIP_API_KEY above
    # (shared with the trucking-app relay). No credential -> LIVE rung
    # disabled and /api/logistics/* degrades through its CACHED/DATABASE/
    # FALLBACK rungs. URL empty -> the client's official default
    # (www.ulip.dpiit.gov.in/ulip/v1.0.0); set to point at staging or a
    # proxy. NO hardcoded vendor URL in business code.
    ulip_api_url: str = ""
    ulip_client_id: str = ""
    ulip_client_secret: str = ""
    cache_ttl_ulip_s: int = 300              # ULIP CACHED fallback rung

    # --- Provisional vehicle flow ---
    provisional_window_h: int = 24           # 24-hour cure window (spec)

    # --- PWA login eligibility gate ---
    # When true, POST /api/auth/device-token only mints a DRIVER token if the
    # entered Vehicle ID is assigned to an ACTIVE driver in core.driver_identity. Default
    # false for migration safety (the existing truck-sim/ULIP gate is unchanged);
    # set REQUIRE_DRIVER_PROFILE=true in production to enforce the assignment.
    require_driver_profile: bool = False

    # --- Elevated-scrutiny gate boom delay (trucking-app TERTIARY) ---
    gate_boom_delay_s: int = 5

    # --- Decision ring buffer ---
    decision_ring_size: int = 1000

    # --- WebSocket sampling ---
    truck_position_sample: int = 50          # 1-in-50 truck positions on /api/ws

    # --- Automatic congestion alerting (UC-3 audit R4/R7) ---
    # When a segment's predicted congestion probability crosses this threshold the
    # traffic path auto-raises a TRAFFIC_CONGESTION alert (core.alert +
    # core.notification) and fans it out over WS + WebPush/FCM. Set to 1.0 (or
    # above) to disable auto-alerting without touching code.
    congestion_alert_threshold: float = 0.80

    # --- WebPush (trucking-app PWA re-route notifications) ---
    # VAPID keys generated by `make vapid-keys`. When the private key is absent
    # the push endpoints degrade gracefully (subscriptions are accepted but no
    # browser push is delivered) and the PWA falls back to the WS reroute frame
    # / in-app polling, so the demo never hard-depends on a configured key.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:ops@jnpa-uc3.example"

    # --- Firebase Cloud Messaging (THIRD push transport, added alongside
    # WebPush + WebSocket). The service-account JSON is loaded from a path OUTSIDE
    # the repo (never committed). When unset, FCM is disabled and delivery falls
    # back to WebPush / WS exactly as before — the demo never hard-depends on it.
    firebase_project_id: str = ""
    firebase_service_account_path: str = ""

    # --- Observability ---
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        shared = get_settings()
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8000),
            anpr_ai_url=os.environ.get("GATEWAY_ANPR_URL", "http://anpr:8301"),
            vahan_sim_url=os.environ.get("GATEWAY_VAHAN_SIM_URL", "http://vahan-sim:8201"),
            vahan_live_url=os.environ.get("GATEWAY_VAHAN_LIVE_URL", "http://vahan-live:8202"),
            truck_api_url=os.environ.get("GATEWAY_TRUCK_URL", "http://truck-sim:8240"),
            congestion_url=os.environ.get("GATEWAY_CONGESTION_URL", "http://congestion:8311"),
            anomaly_url=os.environ.get("GATEWAY_ANOMALY_URL", "http://anomaly:8321"),
            scenarios_url=os.environ.get("GATEWAY_SCENARIOS_URL", "http://scenarios:8400"),
            ulip_url=os.environ.get("GATEWAY_ULIP_URL", ""),
            empty_container_url=os.environ.get("GATEWAY_EMPTY_CONTAINER_URL", "http://empty-container:8330"),
            carbon_url=os.environ.get("GATEWAY_CARBON_URL", "http://carbon:8340"),
            gate_data_url=os.environ.get("GATEWAY_GATE_DATA_URL", "http://gate-data:8350"),
            identity_url=os.environ.get("GATEWAY_IDENTITY_URL", "http://identity:8360"),
            parking_url=os.environ.get("GATEWAY_PARKING_URL", "http://parking:8370"),
            eir_ocr_url=os.environ.get("GATEWAY_EIR_OCR_URL", "http://eir-ocr:8210"),
            postgres_dsn=os.environ.get("POSTGRES_DSN", shared.postgres_dsn),
            redis_url=os.environ.get("REDIS_URL", shared.redis_url),
            kafka_brokers=os.environ.get("KAFKA_BROKERS", shared.kafka_brokers),
            mqtt_host=os.environ.get("MQTT_HOST", shared.mqtt_host),
            mqtt_port=_as_int(os.environ.get("MQTT_PORT"), shared.mqtt_port),
            surepass_api_token=os.environ.get("SUREPASS_API_TOKEN", shared.surepass_api_token),
            ulip_api_key=os.environ.get("ULIP_API_KEY", shared.ulip_api_key),
            upstream_timeout_s=_as_float(os.environ.get("GATEWAY_UPSTREAM_TIMEOUT_S"), 2.0),
            anpr_lag_threshold_s=_as_float(os.environ.get("GATEWAY_ANPR_LAG_S"), 2.0),
            cache_ttl_vahan_s=_as_int(os.environ.get("GATEWAY_CACHE_TTL_VAHAN_S"), 12 * 3600),
            cache_ttl_anpr_s=_as_int(os.environ.get("GATEWAY_CACHE_TTL_ANPR_S"), 60),
            cache_ttl_traffic_s=_as_int(os.environ.get("GATEWAY_CACHE_TTL_TRAFFIC_S"), 90),
            cache_ttl_weather_s=_as_int(os.environ.get("GATEWAY_CACHE_TTL_WEATHER_S"), 600),
            open_meteo_weather_url=os.environ.get("OPEN_METEO_WEATHER_URL", "").strip(),
            open_meteo_marine_url=os.environ.get("OPEN_METEO_MARINE_URL", "").strip(),
            openweather_api_key=os.environ.get(
                "OPENWEATHER_API_KEY", shared.openweather_api_key).strip(),
            openweather_url=os.environ.get("OPENWEATHER_URL", "").strip(),
            tomtom_api_key=os.environ.get(
                "TOMTOM_API_KEY", shared.tomtom_api_key).strip(),
            tomtom_flow_url=os.environ.get("TOMTOM_FLOW_URL", "").strip(),
            tomtom_incidents_url=os.environ.get("TOMTOM_INCIDENTS_URL", "").strip(),
            tomtom_routing_url=os.environ.get("TOMTOM_ROUTING_URL", "").strip(),
            cache_ttl_tomtom_s=_as_int(os.environ.get("GATEWAY_CACHE_TTL_TOMTOM_S"), 120),
            bhuvan_wms_url=os.environ.get("BHUVAN_WMS_URL", "").strip(),
            bhuvan_layer=os.environ.get("BHUVAN_LAYER", "").strip(),
            bhuvan_enabled=_as_bool(os.environ.get("BHUVAN_ENABLED"), True),
            ulip_api_url=os.environ.get("ULIP_API_URL", "").strip(),
            ulip_client_id=os.environ.get("ULIP_CLIENT_ID", "").strip(),
            ulip_client_secret=os.environ.get("ULIP_CLIENT_SECRET", "").strip(),
            cache_ttl_ulip_s=_as_int(os.environ.get("GATEWAY_CACHE_TTL_ULIP_S"), 300),
            provisional_window_h=_as_int(os.environ.get("GATEWAY_PROVISIONAL_WINDOW_H"), 24),
            require_driver_profile=_as_bool(os.environ.get("REQUIRE_DRIVER_PROFILE"), False),
            gate_boom_delay_s=_as_int(os.environ.get("GATEWAY_GATE_BOOM_DELAY_S"), 5),
            decision_ring_size=_as_int(os.environ.get("GATEWAY_DECISION_RING_SIZE"), 1000),
            truck_position_sample=_as_int(os.environ.get("GATEWAY_TRUCK_SAMPLE"), 50),
            congestion_alert_threshold=_as_float(os.environ.get("CONGESTION_ALERT_THRESHOLD"), 0.80),
            vapid_public_key=os.environ.get("VAPID_PUBLIC_KEY", ""),
            vapid_private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
            vapid_subject=os.environ.get("VAPID_SUBJECT", "mailto:ops@jnpa-uc3.example"),
            firebase_project_id=os.environ.get("FIREBASE_PROJECT_ID", ""),
            firebase_service_account_path=os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", ""),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )

    @property
    def openweather_enabled(self) -> bool:
        """True if an OpenWeatherMap API key is configured (enables the
        openweather block on /api/weather/current)."""
        return bool(self.openweather_api_key.strip())

    @property
    def tomtom_enabled(self) -> bool:
        """True if a TomTom API key is configured (enables the LIVE rung on
        /api/traffic/current)."""
        return bool(self.tomtom_api_key.strip())

    @property
    def ulip_logistics_enabled(self) -> bool:
        """True if a ULIP credential is configured (enables the LIVE rung on
        /api/logistics/*): either the login pair or a static key."""
        return bool(self.ulip_api_key.strip()
                    or (self.ulip_client_id.strip()
                        and self.ulip_client_secret.strip()))

    @property
    def surepass_enabled(self) -> bool:
        """True if a live Surepass token is configured (drives LIVE_PRIMARY)."""
        return bool(self.surepass_api_token.strip())

    @property
    def firebase_enabled(self) -> bool:
        """True if an FCM service-account credential source is configured.

        Only reflects that a source is *named*; actual readiness (valid key +
        firebase-admin installed) is confirmed lazily by ``firebase.init_firebase``.
        """
        return bool(
            self.firebase_service_account_path.strip()
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            or os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
        )
