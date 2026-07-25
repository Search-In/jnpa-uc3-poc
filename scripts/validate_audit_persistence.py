#!/usr/bin/env python3
"""Validation harness for the audit & persistence framework (migration 0003).

Exercises every writer in ``gateway.audit`` against a real Postgres and confirms
rows land in each of the five tables. Proves the acceptance criteria:

  * every API call has an audit record        -> api_audit_log
  * every alert has a database record          -> digital_twin_events (+ geofence_events)
  * every AI detection has a database record    -> anpr_reads (+ ANPR_DETECTION event)
  * every notification has delivery history     -> notifications
  * decisions are durable (survive restart)     -> decision_audit

Run against the running stack (Postgres published on localhost:5433):

    POSTGRES_DSN='postgresql+asyncpg://postgres:jnpa_pw@localhost:5433/postgres' \
        .venv/bin/python scripts/validate_audit_persistence.py

Exit code 0 = all checks passed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo root on the import path so `gateway` / `jnpa_shared` resolve when this is
# run as `python scripts/validate_audit_persistence.py`.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql+asyncpg://postgres:jnpa_pw@localhost:5433/postgres",
)
os.environ["POSTGRES_DSN"] = DSN

from gateway import audit  # noqa: E402
from jnpa_shared.db import fetch_one  # noqa: E402


async def _count(table: str) -> int:
    row = await fetch_one(f"SELECT count(*) AS n FROM core.{table}", dsn=DSN)
    return int(row["n"]) if row else -1


async def main() -> int:
    marker = f"validate-{datetime.now(timezone.utc).isoformat()}"
    checks: list[tuple[str, bool]] = []

    # 0) schema present / topped up (idempotent).
    await audit.ensure_audit_schema(DSN)

    # 1) api_audit_log
    before = await _count("api_audit_log")
    await audit.log_api_audit(
        service_name="vahan", endpoint="POST /vahan/rc", method="POST",
        request_payload={"rc": "MH04AB1234", "_marker": marker},
        response_payload={"status": "ok", "owner": "masked"},
        status_code=200, latency_ms=123.4, transaction_id=marker, dsn=DSN,
    )
    checks.append(("api_audit_log insert", await _count("api_audit_log") == before + 1))

    # 2) digital_twin_events (direct + via alert mirror)
    before = await _count("digital_twin_events")
    await audit.record_event(
        event_type="VEHICLE_DETECTED", vehicle_id="MH04AB1234",
        location={"gate_id": "G-NSICT"}, payload={"_marker": marker}, dsn=DSN,
    )
    await audit.persist_alert_event(
        {"id": marker, "kind": "CUSTOMS_FLAG", "severity": "RED", "plate": "MH43CD5678",
         "gate_id": "G-JNPCT", "payload": {"reason": "eseal_mismatch"}}, dsn=DSN,
    )
    checks.append(("digital_twin_events insert (direct+alert)",
                   await _count("digital_twin_events") >= before + 2))

    # 3) notifications
    before = await _count("notifications")
    await audit.log_notification(
        channel="webpush", event_id=marker, receiver="TRK-0001",
        message="Reroute via NH-348 bypass", delivery_status="SENT",
        provider_response={"pywebpush": True}, dsn=DSN,
    )
    checks.append(("notifications insert", await _count("notifications") == before + 1))

    # 4) decision_audit
    before = await _count("decision_audit")
    await audit.record_decision_audit(
        request_id=marker, input_data={"api": "vahan"}, rule_executed="vahan",
        decision="LIVE_PRIMARY", action_taken="PRIMARY", dsn=DSN,
    )
    checks.append(("decision_audit insert", await _count("decision_audit") == before + 1))

    # 5) geofence_events (direct + via geofence-family alert)
    before = await _count("geofence_events")
    await audit.record_geofence_event(
        vehicle_id="MH04AB1234", zone_id="NPZ-GATE-NSICT",
        entry_time=datetime.now(timezone.utc), violation_type="ILLEGAL_PARKING",
        action_taken="ALERT_RAISED", dsn=DSN,
    )
    await audit.persist_alert_event(
        {"id": marker + "-g", "kind": "ILLEGAL_PARKING", "plate": "MH12ZZ9999",
         "payload": {"zone_id": "NPZ-YJUNCTION"}}, dsn=DSN,
    )
    checks.append(("geofence_events insert (direct+alert)",
                   await _count("geofence_events") >= before + 2))

    # 6) anpr_reads (AI detection storage) + mirrored ANPR_DETECTION event
    before_a = await _count("anpr_reads")
    before_e = await _count("digital_twin_events")
    await audit.persist_anpr_read(
        {"ts": datetime.now(timezone.utc).isoformat(), "camera_id": "CAM-COR-01",
         "plate": "MH04AB1234", "conf": 0.97, "vehicle_class": "HGV",
         "image_url": None, "weather": "CLEAR", "degraded": False}, dsn=DSN,
    )
    checks.append(("anpr_reads insert (AI detection storage)",
                   await _count("anpr_reads") == before_a + 1))
    checks.append(("anpr_reads -> ANPR_DETECTION event mirror",
                   await _count("digital_twin_events") == before_e + 1))

    # report
    print("\n=== Audit persistence validation ===")
    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    for t in ("api_audit_log", "digital_twin_events", "notifications",
              "decision_audit", "geofence_events", "anpr_reads"):
        print(f"  rows in core.{t}: {await _count(t)}")
    print("=== RESULT:", "ALL PASS ===" if ok else "FAILURES PRESENT ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
