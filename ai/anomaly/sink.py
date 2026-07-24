"""Alert sink + recent-alert query.

Every alert the engine raises is:
  1. written to ``core.alert`` (the operational alert table), and
  2. published to the Kafka ``alerts`` topic (the wire contract; see
     ``jnpa_shared.schemas.TOPIC_ALERTS``).

``GET /alerts/recent`` reads back from ``core.alert``. Both paths are
best-effort and independently fault-tolerant: a Kafka outage must not stop the
DB write, and vice-versa, so an alert is never silently lost on a single-sink
failure (it is logged and the other sink still receives it).

DB access uses psycopg (libpq DSN), matching ai/congestion's write path, so the
service has no hard SQLAlchemy dependency for the hot alert path.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from jnpa_shared import kafka_io
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import Alert

from .config import AnomalyConfig

log = get_logger("anomaly.sink")


class AlertSink:
    """Writes alerts to Postgres + Kafka and reads recent alerts back."""

    def __init__(self, cfg: AnomalyConfig) -> None:
        self.cfg = cfg
        self._producer = None

    # -- producer lifecycle -------------------------------------------------
    def start(self) -> None:
        try:
            self._producer = kafka_io.get_producer({"client.id": "anomaly"})
        except Exception as exc:  # noqa: BLE001
            log.warning("kafka_producer_unavailable", error=str(exc))
            self._producer = None

    def close(self) -> None:
        if self._producer is not None:
            try:
                self._producer.flush(5)
            except Exception:  # noqa: BLE001
                pass
            self._producer = None

    # -- emit ---------------------------------------------------------------
    def emit(self, alert: Alert) -> None:
        """Persist + publish one alert (each sink independently best-effort)."""
        self._write_db(alert)
        self._publish(alert)

    def _publish(self, alert: Alert) -> None:
        if self._producer is None:
            return
        try:
            kafka_io.produce(
                self._producer,
                self.cfg.alerts_topic,
                alert,
                key=alert.kind,
                flush=False,
                event_type=f"jnpa.alert.{alert.kind}",
                source_system="SIM",
                raw_ref=f"alert://{alert.id}",
            )
            self._producer.poll(0)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert_publish_failed", kind=alert.kind, error=str(exc))

    def _write_db(self, alert: Alert) -> None:
        try:
            import psycopg
            from psycopg.types.json import Json
        except Exception:  # noqa: BLE001
            return
        try:
            with psycopg.connect(self.cfg.postgres_dsn_libpq, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO core.alert (id, ts, kind, severity, gate_id, plate, payload, ack)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (id) DO NOTHING",
                        (
                            str(alert.id),
                            alert.ts,
                            alert.kind,
                            alert.severity,
                            alert.gate_id,
                            alert.plate,
                            Json(alert.payload),
                            alert.ack,
                        ),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("alert_db_write_failed", kind=alert.kind, error=str(exc))

    # -- read ---------------------------------------------------------------
    def recent(self, since: datetime, kinds: Optional[List[str]] = None,
               limit: int = 1000) -> List[Alert]:
        """Return alerts with ts >= ``since`` (most recent first)."""
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception:  # noqa: BLE001
            return []
        sql = (
            "SELECT id, ts, kind, severity, gate_id, plate, payload, ack"
            " FROM core.alert WHERE ts >= %s"
        )
        params: list = [since]
        if kinds:
            sql += " AND kind = ANY(%s)"
            params.append(kinds)
        sql += " ORDER BY ts DESC LIMIT %s"
        params.append(limit)
        try:
            with psycopg.connect(self.cfg.postgres_dsn_libpq, connect_timeout=3) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("alert_recent_query_failed", error=str(exc))
            return []
        return [
            Alert(
                id=r["id"],
                ts=r["ts"],
                kind=r["kind"],
                severity=r["severity"] or "info",
                gate_id=r["gate_id"],
                plate=r["plate"],
                payload=r["payload"] or {},
                ack=r["ack"] or False,
            )
            for r in rows
        ]


__all__ = ["AlertSink"]
