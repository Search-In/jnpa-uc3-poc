# jnpa-shared

Shared library for the JNPA Digital Twin — Use Case III PoC.

Provides:

- `config` — pydantic-settings `Settings` loaded from `.env.local`.
- `corridor` — NH-348 polyline (JNPA → Karal Phata) + `nearest_segment()`.
- `schemas` — pydantic v2 event models (ANPR, Vahan, FASTag, RFID, telemetry, traffic, alerts, scenarios).
- `kafka_io` — confluent-kafka producer/consumer helpers (JSON, snappy).
- `db` — SQLAlchemy 2.0 async engine factory + tiny CRUD helpers.
- `redis_io` — async Redis client with TTL cache helpers.
- `logging` — structlog JSON logging with a `trace_id`.

Install editable from any service:

```bash
pip install -e ../shared
```
