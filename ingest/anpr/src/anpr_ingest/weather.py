"""Weather tagging for ANPR frames.

Pulls OpenWeatherMap "Current Weather Data" for the port coordinates every
``weather_interval_s`` (default 10 min) and derives a coarse condition label:

    fog   if visibility < 1000 m
    rain  if rain volume > 0
    dust  if pm10 > 120 µg/m³ (from OpenAQ fallback)
    clear otherwise

The latest label is cached and returned synchronously by ``current()`` so the
hot frame loop never blocks on the network. Without an API key (or on any
error) the label stays "clear" and the pull is counted as skipped/error.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from jnpa_shared.logging import get_logger

from .config import AnprConfig
from .metrics import WEATHER_PULLS

log = get_logger("anpr_ingest.weather")

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"
OPENAQ_URL = "https://api.openaq.org/v2/latest"

FOG_VISIBILITY_M = 1000
DUST_PM10 = 120.0


class WeatherTagger:
    """Holds the current weather label and refreshes it on a timer."""

    def __init__(self, cfg: AnprConfig) -> None:
        self.cfg = cfg
        self._label = "clear"

    def current(self) -> str:
        """Return the most recently computed weather label (non-blocking)."""
        return self._label

    async def _fetch_pm10(self, client: httpx.AsyncClient) -> Optional[float]:
        """Best-effort PM10 from OpenAQ near the port (no key required)."""
        try:
            resp = await client.get(
                OPENAQ_URL,
                params={
                    "coordinates": f"{self.cfg.port_lat},{self.cfg.port_lon}",
                    "radius": 25000,
                    "parameter": "pm10",
                    "limit": 1,
                },
                timeout=self.cfg.ai_timeout_s + 3,
            )
            if resp.status_code != 200:
                return None
            results = resp.json().get("results", [])
            for r in results:
                for m in r.get("measurements", []):
                    if m.get("parameter") == "pm10":
                        return float(m.get("value"))
        except (httpx.HTTPError, ValueError, KeyError):
            return None
        return None

    async def _refresh_once(self, client: httpx.AsyncClient) -> str:
        """Pull current conditions and compute the label. Updates the cache."""
        if not self.cfg.openweather_api_key:
            WEATHER_PULLS.labels(result="skipped").inc()
            log.info("weather_skipped", reason="no_api_key", label=self._label)
            return self._label
        try:
            resp = await client.get(
                OWM_URL,
                params={
                    "lat": self.cfg.port_lat,
                    "lon": self.cfg.port_lon,
                    "appid": self.cfg.openweather_api_key,
                    "units": "metric",
                },
                timeout=self.cfg.ai_timeout_s + 3,
            )
            resp.raise_for_status()
            data = resp.json()

            visibility = data.get("visibility", 10000)
            rain = (data.get("rain") or {}).get("1h", 0.0)

            label = "clear"
            if visibility is not None and visibility < FOG_VISIBILITY_M:
                label = "fog"
            elif rain and rain > 0:
                label = "rain"
            else:
                pm10 = await self._fetch_pm10(client)
                if pm10 is not None and pm10 > DUST_PM10:
                    label = "dust"

            self._label = label
            WEATHER_PULLS.labels(result="ok").inc()
            log.info("weather_pulled", label=label, visibility=visibility, rain=rain)
            return label
        except httpx.HTTPError as exc:
            WEATHER_PULLS.labels(result="error").inc()
            log.warning("weather_pull_failed", error=str(exc), label=self._label)
            return self._label

    async def run(self, stop: asyncio.Event) -> None:
        """Refresh loop: pull immediately, then every weather_interval_s."""
        async with httpx.AsyncClient() as client:
            while not stop.is_set():
                await self._refresh_once(client)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.weather_interval_s)
                except asyncio.TimeoutError:
                    pass
