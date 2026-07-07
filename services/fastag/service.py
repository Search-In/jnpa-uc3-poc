"""FASTag service orchestration layer (Step 3) — the consistency brain.

Consumes MAPPER output (:mod:`services.fastag.mappers`) and persists it to Postgres
with the correct idempotency strategy per API. This is the only layer that writes
to the DB and (optionally) emits events.

Idempotency / dedup strategy (per the spec):

  * transactions -> batch INSERT ... ON CONFLICT (seq_no) DO NOTHING   (idempotent;
                    seq_no is the vendor idempotency key + a UNIQUE constraint)
  * balance      -> UPSERT on rc_number (latest snapshot always wins; one row/RC)
  * toll_enroute -> plain INSERT (historical/analytical route data; no dedup)

Hard boundaries (non-negotiable):
  * NEVER calls ULIP directly        -> that is services.fastag.ulip_client
  * NEVER parses/transforms raw JSON -> that is services.fastag.mappers
  * Consumes the mapper's ``db`` payload verbatim (exact column names).

Guarantees:
  * ONE transaction per request — rollback on any failure, NO partial writes.
  * Structured observability line per call (operation, status, counts, latency).
  * Best-effort event emission (Kafka) — never blocks or fails the DB write.
  * Structured error envelope on failure: ``{"status": "FAILED", "reason": ...}``.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from time import perf_counter
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import TOPIC_FASTAG_TXN

log = get_logger("services.fastag.service")

# Event topics (best-effort; only used when a Kafka producer is supplied).
_TOPIC_BALANCE = "fastag.balance"
_TOPIC_ENROUTE = "fastag.enroute"

# B) Balance — UPSERT on rc_number (latest snapshot always wins).
_UPSERT_BALANCE = """
INSERT INTO jnpa.fastag_balance
    (rc_number, tag_id, provider_name, provider_code, customer_name,
     available_recharge_limit, available_balance, tag_status, vehicle_class,
     vehicle_class_desc, model_name, updated_at)
VALUES
    (:rc_number, :tag_id, :provider_name, :provider_code, :customer_name,
     :available_recharge_limit, :available_balance, :tag_status, :vehicle_class,
     :vehicle_class_desc, :model_name, now())
ON CONFLICT (rc_number) DO UPDATE SET
    tag_id = EXCLUDED.tag_id,
    provider_name = EXCLUDED.provider_name,
    provider_code = EXCLUDED.provider_code,
    customer_name = EXCLUDED.customer_name,
    available_recharge_limit = EXCLUDED.available_recharge_limit,
    available_balance = EXCLUDED.available_balance,
    tag_status = EXCLUDED.tag_status,
    vehicle_class = EXCLUDED.vehicle_class,
    vehicle_class_desc = EXCLUDED.vehicle_class_desc,
    model_name = EXCLUDED.model_name,
    updated_at = now()
"""

# C) Toll Enroute — plain INSERT (historical; no dedup). Array preserved as jsonb.
_INSERT_ENROUTE = """
INSERT INTO jnpa.toll_enroute
    (id, client_id, source_state, source_name, destination_state, destination_name,
     vehicle_type, duration, distance, toll_plaza_details)
VALUES
    (CAST(:id AS uuid), :client_id, :source_state, :source_name, :destination_state,
     :destination_name, :vehicle_type, :duration, :distance,
     CAST(:toll_plaza_details AS jsonb))
"""


def _jsonable(value: Any) -> Any:
    """Make a value JSON-safe for the event payload (Decimal -> str)."""
    return str(value) if isinstance(value, Decimal) else value


class FastagService:
    """Deterministic persistence + event orchestration for the FASTag mappers.

    Stateless apart from the DSN + optional Kafka producer, so a single instance
    is safe to share. Every method takes a MAPPER ENVELOPE
    (``{"status": "success", "db": ...}``) — never raw vendor JSON.
    """

    def __init__(self, dsn: Optional[str] = None, producer: Any = None) -> None:
        # ``producer`` is an optional confluent_kafka Producer. When None, event
        # publishing is skipped entirely (events are optional; writes still happen).
        self._dsn = dsn
        self._producer = producer

    # ------------------------------------------------------------------ public
    async def process_balance(
        self, mapped: Mapping[str, Any], *, client_id: Optional[str] = None
    ) -> dict:
        """UPSERT the latest FASTag balance snapshot (one row per rc_number)."""
        t0 = perf_counter()
        bad = self._precheck(mapped, "balance", client_id, t0)
        if bad:
            return bad
        row: dict = dict(mapped["db"])
        try:
            async with get_engine(self._dsn).begin() as conn:
                await conn.execute(text(_UPSERT_BALANCE), row)
        except Exception as exc:  # noqa: BLE001 — one txn, already rolled back
            return self._fail("balance", client_id, exc, t0)

        self._observe("balance", "success", client_id, t0, inserted=1)
        self._publish(
            _TOPIC_BALANCE, key=row.get("rc_number"),
            value={"event": "balance.updated", "rc_number": row.get("rc_number"),
                   "tag_id": row.get("tag_id"),
                   "available_balance": _jsonable(row.get("available_balance")),
                   "tag_status": row.get("tag_status")},
        )
        return {
            "status": "SUCCESS", "operation": "balance",
            "rc_number": row.get("rc_number"), "tag_id": row.get("tag_id"),
            "available_balance": _jsonable(row.get("available_balance")),
            "tag_status": row.get("tag_status"), "updated": True,
            "latency_ms": self._ms(t0),
        }

    async def process_toll_enroute(
        self, mapped: Mapping[str, Any], *, client_id: Optional[str] = None
    ) -> dict:
        """INSERT a toll-enroute route lookup (historical; never deduplicated)."""
        t0 = perf_counter()
        bad = self._precheck(mapped, "enroute", client_id, t0)
        if bad:
            return bad
        row: dict = dict(mapped["db"])
        new_id = str(uuid.uuid4())
        params = {
            "id": new_id,
            "client_id": row.get("client_id"),
            "source_state": row.get("source_state"),
            "source_name": row.get("source_name"),
            "destination_state": row.get("destination_state"),
            "destination_name": row.get("destination_name"),
            "vehicle_type": row.get("vehicle_type"),
            "duration": row.get("duration"),
            "distance": row.get("distance"),  # Decimal -> numeric(10,2)
            # list[dict] -> jsonb (cast below); no array-data loss.
            "toll_plaza_details": json.dumps(row.get("toll_plaza_details") or []),
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                await conn.execute(text(_INSERT_ENROUTE), params)
        except Exception as exc:  # noqa: BLE001
            return self._fail("enroute", client_id, exc, t0)

        plaza_count = len(row.get("toll_plaza_details") or [])
        self._observe("enroute", "success", client_id, t0, inserted=1)
        self._publish(
            _TOPIC_ENROUTE, key=new_id,
            value={"event": "toll.enroute.created", "id": new_id,
                   "client_id": row.get("client_id"),
                   "source": row.get("source_name"),
                   "destination": row.get("destination_name"),
                   "plaza_count": plaza_count},
        )
        return {
            "status": "SUCCESS", "operation": "enroute", "id": new_id,
            "client_id": row.get("client_id"),
            "source": row.get("source_name"), "destination": row.get("destination_name"),
            "distance": _jsonable(row.get("distance")), "plaza_count": plaza_count,
            "latency_ms": self._ms(t0),
        }

    async def process_transactions(
        self, mapped: Mapping[str, Any], *, client_id: Optional[str] = None
    ) -> dict:
        """Batch-INSERT plaza crossings, skipping duplicates by ``seq_no``.

        Idempotent two ways: intra-batch duplicate seq_no are dropped in-process,
        and cross-batch/replayed rows are skipped by the UNIQUE(seq_no) constraint
        via ``ON CONFLICT DO NOTHING``. One statement (batch, not a loop), one
        transaction — a failure rolls the whole batch back (no partial writes).
        """
        t0 = perf_counter()
        bad = self._precheck(mapped, "transaction", client_id, t0, list_payload=True)
        if bad:
            return bad
        rows: list = list(mapped["db"])
        total = len(rows)
        if total == 0:
            self._observe("transaction", "success", client_id, t0)
            return {"status": "SUCCESS", "operation": "transaction",
                    "inserted_count": 0, "skipped_count": 0, "failed_count": 0,
                    "total": 0, "latency_ms": self._ms(t0)}

        # 1) Drop intra-batch duplicate seq_no (keep first). NULL seq_no can't be
        #    deduped (SQL treats NULLs as distinct) so it passes through.
        seen: set = set()
        unique_rows: list = []
        intra_dupes = 0
        for r in rows:
            sq = r.get("seq_no")
            if sq is not None and sq in seen:
                intra_dupes += 1
                continue
            if sq is not None:
                seen.add(sq)
            unique_rows.append(r)

        # 2) Single multi-row INSERT ... ON CONFLICT (seq_no) DO NOTHING RETURNING
        #    seq_no — the returned count is exactly the rows that actually inserted.
        try:
            async with get_engine(self._dsn).begin() as conn:
                inserted = await self._insert_txn_batch(conn, unique_rows)
        except Exception as exc:  # noqa: BLE001 — whole batch rolled back
            return self._fail("transaction", client_id, exc, t0)

        skipped = total - inserted            # intra dupes + replayed (existing) rows
        self._observe("transaction", "success", client_id, t0,
                      inserted=inserted, skipped=skipped)
        self._publish(
            TOPIC_FASTAG_TXN, key=(rows[0].get("rc_number") if rows else None),
            value={"event": "fastag.transaction.ingested",
                   "rc_number": rows[0].get("rc_number") if rows else None,
                   "tag_id": rows[0].get("tag_id") if rows else None,
                   "inserted": inserted, "skipped": skipped, "total": total,
                   "status": "inserted" if inserted else "skipped"},
        )
        return {
            "status": "SUCCESS", "operation": "transaction",
            "inserted_count": inserted, "skipped_count": skipped,
            "failed_count": 0, "total": total, "latency_ms": self._ms(t0),
        }

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _insert_txn_batch(conn, unique_rows: list) -> int:
        """One multi-VALUES insert; returns how many rows were actually inserted."""
        if not unique_rows:
            return 0
        values: list[str] = []
        params: dict = {}
        for i, r in enumerate(unique_rows):
            params[f"id_{i}"] = str(uuid.uuid4())
            params[f"tag_{i}"] = r.get("tag_id")
            params[f"rc_{i}"] = r.get("rc_number")
            params[f"seq_{i}"] = r.get("seq_no")
            params[f"ts_{i}"] = r.get("transaction_date_time")
            params[f"ld_{i}"] = r.get("lane_direction")
            params[f"pn_{i}"] = r.get("toll_plaza_name")
            params[f"gc_{i}"] = r.get("toll_plaza_geocode")
            params[f"vt_{i}"] = r.get("vehicle_type")
            params[f"bn_{i}"] = r.get("bank_name")   # batch-level, from mapper row
            params[f"st_{i}"] = r.get("status")      # batch-level, from mapper row
            values.append(
                f"(CAST(:id_{i} AS uuid), :tag_{i}, :rc_{i}, :seq_{i}, :ts_{i}, "
                f":ld_{i}, :pn_{i}, :gc_{i}, :vt_{i}, :bn_{i}, :st_{i})"
            )
        sql = (
            "INSERT INTO jnpa.fastag_transactions "
            "(id, tag_id, rc_number, seq_no, transaction_date_time, lane_direction, "
            " toll_plaza_name, toll_plaza_geocode, vehicle_type, bank_name, status) VALUES "
            + ", ".join(values)
            + " ON CONFLICT (seq_no) DO NOTHING RETURNING seq_no"
        )
        result = await conn.execute(text(sql), params)
        return len(result.fetchall())

    def _precheck(
        self, mapped: Any, operation: str, client_id: Optional[str], t0: float,
        *, list_payload: bool = False,
    ) -> Optional[dict]:
        """Reject a failed/invalid mapper envelope before touching the DB."""
        if not isinstance(mapped, Mapping) or mapped.get("status") != "success":
            detail = mapped.get("reason") if isinstance(mapped, Mapping) else "invalid_mapper_output"
            return self._reject(operation, client_id, t0, detail)
        if "db" not in mapped:
            return self._reject(operation, client_id, t0, "mapper_missing_db_payload")
        payload = mapped["db"]
        if list_payload and not isinstance(payload, list):
            return self._reject(operation, client_id, t0, "expected_list_payload")
        if not list_payload and not isinstance(payload, Mapping):
            return self._reject(operation, client_id, t0, "expected_object_payload")
        return None

    def _reject(self, operation: str, client_id: Optional[str], t0: float, detail: Any) -> dict:
        self._observe(operation, "failed", client_id, t0)
        log.warning("fastag.service.rejected", module="fastag", stage="service",
                    operation=operation, client_id=client_id,
                    reason="validation_error", detail=detail)
        return {"status": "FAILED", "operation": operation,
                "reason": "validation_error", "detail": detail}

    def _fail(self, operation: str, client_id: Optional[str], exc: Exception, t0: float) -> dict:
        reason = self._classify(exc)
        self._observe(operation, "failed", client_id, t0)
        log.error("fastag.service.failed", module="fastag", stage="service",
                  operation=operation, client_id=client_id, reason=reason,
                  error=f"{type(exc).__name__}: {exc!s}")
        return {"status": "FAILED", "operation": operation, "reason": reason}

    @staticmethod
    def _classify(exc: Exception) -> str:
        """Map a DB exception to the required reason code."""
        name = type(exc).__name__.lower()
        if "integrity" in name or "unique" in name or "conflict" in name:
            return "conflict"
        return "db_error"

    @staticmethod
    def _ms(t0: float) -> float:
        return round((perf_counter() - t0) * 1000, 1)

    def _observe(
        self, operation: str, status: str, client_id: Optional[str], t0: float,
        *, inserted: int = 0, skipped: int = 0, failed: int = 0,
    ) -> None:
        """Mandatory per-call observability line."""
        log.info("fastag.service", module="fastag", stage="service",
                 operation=operation, status=status, inserted=inserted,
                 skipped=skipped, failed=failed, client_id=client_id,
                 latency_ms=self._ms(t0))

    def _publish(self, topic: str, *, key: Optional[str], value: dict) -> None:
        """Best-effort event emission. Never raises, never blocks the write."""
        if self._producer is None:
            return
        try:
            from jnpa_shared.kafka_io import produce

            produce(self._producer, topic, value, key=key,
                    event_type=value.get("event"), source_system="LIVE", flush=False)
        except Exception as exc:  # noqa: BLE001 — events are optional
            log.warning("fastag.event.publish_failed", module="fastag", stage="service",
                        topic=topic, error=str(exc))


__all__ = ["FastagService"]
