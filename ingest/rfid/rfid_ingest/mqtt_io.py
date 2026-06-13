"""paho-mqtt v2 client factory with resilient auto-reconnect/backoff.

Both the emulator (publisher) and consumer (subscriber) build their client here
so the reconnect behaviour is identical and broker-restart resilient:

  * ``reconnect_delay_set`` gives exponential backoff between 1 s and 30 s.
  * ``loop_start()`` runs the network loop on a background thread that keeps
    retrying the connection forever — a broker restart self-heals with no
    intervention.
  * ``connect_async`` means the very first connect also retries (the broker may
    not be up yet when the container starts).
"""
from __future__ import annotations

from typing import Callable, Optional

import paho.mqtt.client as mqtt

from jnpa_shared.logging import get_logger

from .config import RfidConfig

log = get_logger("rfid_ingest.mqtt")


def build_client(
    cfg: RfidConfig,
    client_id: str,
    *,
    on_connect: Optional[Callable] = None,
    on_disconnect: Optional[Callable] = None,
    on_message: Optional[Callable] = None,
) -> mqtt.Client:
    """Create a configured (not-yet-connected) paho v2 client with backoff."""
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
    )
    # Exponential reconnect backoff, capped. paho doubles the delay on each
    # failed attempt up to max_delay, then holds there.
    client.reconnect_delay_set(
        min_delay=int(cfg.mqtt_reconnect_min_s),
        max_delay=int(cfg.mqtt_reconnect_max_s),
    )

    def _default_on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or getattr(reason_code, "is_failure", False) is False:
            log.info("mqtt_connected", client_id=client_id)
        else:
            log.warning("mqtt_connect_failed", client_id=client_id, reason=str(reason_code))

    def _default_on_disconnect(c, userdata, *args):
        # paho v2 passes (flags, reason_code, properties); be tolerant of arity.
        reason = args[1] if len(args) >= 2 else (args[0] if args else "unknown")
        log.warning("mqtt_disconnected", client_id=client_id, reason=str(reason))

    client.on_connect = on_connect or _default_on_connect
    client.on_disconnect = on_disconnect or _default_on_disconnect
    if on_message is not None:
        client.on_message = on_message

    return client


def start(cfg: RfidConfig, client: mqtt.Client) -> None:
    """Begin the async connect + background network loop (retries forever)."""
    # connect_async + loop_start: the first connection is retried too, so the
    # service tolerates the broker not being up yet at container start.
    client.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=cfg.mqtt_keepalive)
    client.loop_start()
    log.info("mqtt_loop_started", host=cfg.mqtt_host, port=cfg.mqtt_port)


def stop(client: mqtt.Client) -> None:
    try:
        client.loop_stop()
        client.disconnect()
    except Exception as exc:  # noqa: BLE001
        log.warning("mqtt_stop_error", error=str(exc))
