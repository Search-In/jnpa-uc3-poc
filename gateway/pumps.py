"""Background event pumps that feed the /api/ws fan-out.

Three independent producers tail the platform's buses and push frames onto the
WebSocket hub:

* ``kafka_alerts_pump``  -> Kafka ``alerts``            -> type=alert
* ``kafka_traffic_pump`` -> Kafka ``traffic.snapshots`` -> type=traffic
* ``mqtt_truck_pump``    -> MQTT  ``trucks/+/telemetry`` -> type=truck_position
                            (sampled 1-in-N for bandwidth; spec: 1 in 50)

The Kafka pumps run the (blocking) confluent consumer in a worker thread and
bounce each decoded message back onto the event loop with
``run_coroutine_threadsafe``. The MQTT pump uses the async ``aiomqtt`` client.

Everything is best-effort: a missing broker / library logs once and the pump
exits quietly — the gateway's HTTP surface stays up regardless.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from jnpa_shared import kafka_io

from .logging import get_logger
from .state import GatewayState

log = get_logger("gateway.pumps")


# ---------------------------------------------------------------------------
# Kafka pumps (blocking consumer in a thread -> loop)
# ---------------------------------------------------------------------------
class KafkaPump:
    """Runs ``kafka_io.consume`` in a daemon thread, forwarding to a WS type."""

    def __init__(
        self,
        state: GatewayState,
        loop: asyncio.AbstractEventLoop,
        topic: str,
        ws_type: str,
        group: str,
    ) -> None:
        self.state = state
        self.loop = loop
        self.topic = topic
        self.ws_type = ws_type
        self.group = group
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"pump-{self.ws_type}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _handle(self, value: Any) -> None:
        # Bounce onto the event loop; ignore if the loop is shutting down.
        try:
            asyncio.run_coroutine_threadsafe(
                self.state.ws.broadcast(self.ws_type, value), self.loop
            )
        except RuntimeError:  # loop closed
            pass

    def _run(self) -> None:
        try:
            kafka_io.consume(
                self.topic, self.group, self._handle,
                timeout=1.0, stop_when=self._stop.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - broker absent / transient
            log.warning("kafka_pump_exit", topic=self.topic, error=str(exc))


# ---------------------------------------------------------------------------
# MQTT truck-position pump (async aiomqtt) — sampled 1-in-N
# ---------------------------------------------------------------------------
async def mqtt_truck_pump(state: GatewayState, stop: asyncio.Event) -> None:
    """Tail ``trucks/+/telemetry`` and forward a 1-in-N sample as truck_position."""
    cfg = state.cfg
    sample = max(1, cfg.truck_position_sample)
    try:
        import aiomqtt
    except Exception as exc:  # pragma: no cover - lib absent
        log.warning("mqtt_pump_disabled", reason="aiomqtt_unavailable", error=str(exc))
        return

    counter = 0
    while not stop.is_set():
        try:
            async with aiomqtt.Client(hostname=cfg.mqtt_host, port=cfg.mqtt_port) as client:
                await client.subscribe("trucks/+/telemetry", qos=0)
                log.info("mqtt_pump_subscribed", topic="trucks/+/telemetry", sample=sample)
                async for message in client.messages:
                    if stop.is_set():
                        break
                    counter += 1
                    if counter % sample != 0:
                        continue
                    payload = _decode_mqtt(message.payload)
                    if payload is not None:
                        await state.ws.broadcast("truck_position", payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - broker down / reconnect
            if stop.is_set():
                break
            log.warning("mqtt_pump_retry", error=str(exc))
            await asyncio.sleep(3.0)


def _decode_mqtt(raw: bytes) -> Any:
    import json
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


__all__ = ["KafkaPump", "mqtt_truck_pump"]
