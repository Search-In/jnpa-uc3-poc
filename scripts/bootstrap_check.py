#!/usr/bin/env python3
"""End-to-end bootstrap self-test for the JNPA UC-III PoC.

Runs from the *host* against the docker-compose stack and exercises the full
"hello-trace" path:

  1. Read .env.local (fail loudly if missing).
  2. Connect to Postgres and verify jnpa.gates has 4 rows.
  3. Publish one AnprRead JSON message to topic "anpr.reads".
  4. Consume it back with a fresh consumer group.
  5. Write the same record into jnpa.anpr_reads and read it back.
  6. Cache and read a key in Redis.
  7. Publish one MQTT message to rfid/readers/R-01 and confirm a subscriber
     receives it.

Prints a pass/fail table and exits 0 only if every check passes, else 1.

Because the script runs on the host (not inside the jnpa network), the
docker service hostnames are rewritten to localhost + the published host
ports BEFORE jnpa_shared.config is imported.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

# --------------------------------------------------------------------------
# Step 1 (part A): locate and load .env.local, then rewrite connection targets
# to their host-published equivalents. This must happen before importing the
# shared config (which caches Settings at import time).
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env.local"

# Make the shared package importable without an editable install.
SHARED_DIR = REPO_ROOT / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))


def _fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"\n✗ FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


if not ENV_FILE.is_file():
    _fail(
        f".env.local not found at {ENV_FILE}\n"
        f"  Run:  cp .env.local.example .env.local"
    )

# Load .env.local into os.environ.
from dotenv import dotenv_values  # noqa: E402

_env = dotenv_values(str(ENV_FILE))
for k, v in _env.items():
    if v is not None:
        os.environ.setdefault(k, v)

# Host-facing overrides: container hostnames -> localhost:<published port>.
# These take precedence over whatever .env.local set, so we overwrite.
# Postgres is published on 5433 (not 5432) to avoid clashing with a local
# Postgres that may already own host 5432.
HOST = os.environ.get("BOOTSTRAP_HOST", "localhost")
PG_HOST_PORT = os.environ.get("PG_HOST_PORT", "5433")
os.environ["POSTGRES_DSN"] = (
    f"postgresql+asyncpg://postgres:"
    f"{os.environ.get('POSTGRES_PASSWORD', 'jnpa_pw')}@{HOST}:{PG_HOST_PORT}/postgres"
)
os.environ["REDIS_URL"] = f"redis://{HOST}:6379/0"
os.environ["KAFKA_BROKERS"] = f"{HOST}:29092"   # EXTERNAL listener
os.environ["MQTT_BROKER"] = f"{HOST}:1883"

import asyncio  # noqa: E402

from jnpa_shared import db, redis_io  # noqa: E402
from jnpa_shared import kafka_io  # noqa: E402
from jnpa_shared.config import get_settings  # noqa: E402
from jnpa_shared.schemas import AnprRead, VehicleClass  # noqa: E402

import paho.mqtt.client as mqtt  # noqa: E402


# --------------------------------------------------------------------------
# Result accounting
# --------------------------------------------------------------------------
class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))

    @property
    def all_ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)

    def table(self) -> str:
        width = max((len(n) for n, _, _ in self.rows), default=10)
        lines = ["", "=" * (width + 24), f"{'CHECK'.ljust(width)}   RESULT  DETAIL", "-" * (width + 24)]
        for name, ok, detail in self.rows:
            lines.append(f"{name.ljust(width)}   {'PASS' if ok else 'FAIL':<6}  {detail}")
        lines.append("=" * (width + 24))
        return "\n".join(lines)


R = Results()

# A unique marker so the consume/read-back checks find exactly our record.
RUN_ID = uuid.uuid4().hex[:12]
TEST_PLATE = f"MHTEST{RUN_ID[:4].upper()}"
TEST_CAMERA = "CAM-NSICT-OVW"
TOPIC = "anpr.reads"


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------
def check_env() -> None:
    s = get_settings()
    detail = f"corridor='{s.corridor_name}', kafka={s.kafka_brokers}"
    R.record("1. env (.env.local loaded)", True, detail)


async def check_postgres_gates() -> None:
    try:
        rows = await db.fetch_all("SELECT count(*) AS n FROM jnpa.gates")
        n = int(rows[0]["n"]) if rows else -1
        R.record("2. postgres gates == 4", n == 4, f"found {n} rows")
    except Exception as exc:  # noqa: BLE001
        R.record("2. postgres gates == 4", False, repr(exc))


def _build_anpr() -> AnprRead:
    return AnprRead(
        camera_id=TEST_CAMERA,
        plate=TEST_PLATE,
        conf=0.97,
        vehicle_class=VehicleClass.HGV,
        image_url=f"s3://anpr-snapshots/{RUN_ID}.jpg",
        weather="clear",
        degraded=False,
    )


def check_kafka_roundtrip() -> AnprRead | None:
    """Produce one AnprRead and consume it back with a fresh group."""
    record = _build_anpr()
    try:
        producer = kafka_io.get_producer()
        kafka_io.produce(producer, TOPIC, record, key=record.plate, flush=True)
    except Exception as exc:  # noqa: BLE001
        R.record("3. kafka produce", False, repr(exc))
        R.record("4. kafka consume (fresh group)", False, "skipped (produce failed)")
        return None
    R.record("3. kafka produce", True, f"topic={TOPIC} plate={record.plate}")

    group = f"bootstrap-check-{RUN_ID}"
    got: dict | None = None

    def handler(value: dict) -> None:
        nonlocal got
        if value.get("plate") == TEST_PLATE:
            got = value

    try:
        # Fresh group reads from earliest; scan the topic (draining any prior
        # messages too) until our unique plate appears or polling idles out.
        kafka_io.consume(
            TOPIC,
            group,
            handler,
            poll_idle_limit=15,
            timeout=1.0,
            stop_when=lambda: got is not None,
        )
    except Exception as exc:  # noqa: BLE001
        R.record("4. kafka consume (fresh group)", False, repr(exc))
        return record

    R.record(
        "4. kafka consume (fresh group)",
        got is not None and got.get("plate") == TEST_PLATE,
        f"group={group}",
    )
    return record


async def check_postgres_write_read(record: AnprRead | None) -> None:
    if record is None:
        record = _build_anpr()
    try:
        await db.insert_row(
            "jnpa.anpr_reads",
            {
                "ts": record.ts,
                "camera_id": record.camera_id,
                "plate": record.plate,
                "conf": record.conf,
                "vehicle_class": record.vehicle_class.value,
                "image_url": record.image_url,
                "weather": record.weather,
                "degraded": record.degraded,
            },
        )
        row = await db.fetch_one(
            "SELECT plate, conf FROM jnpa.anpr_reads WHERE plate = :p ORDER BY ts DESC LIMIT 1",
            {"p": TEST_PLATE},
        )
        ok = bool(row and row["plate"] == TEST_PLATE)
        R.record("5. postgres write+read anpr_reads", ok, f"plate={row['plate'] if row else None}")
    except Exception as exc:  # noqa: BLE001
        R.record("5. postgres write+read anpr_reads", False, repr(exc))


async def check_redis() -> None:
    key = f"bootstrap:{RUN_ID}"
    payload = {"plate": TEST_PLATE, "run_id": RUN_ID}
    try:
        await redis_io.cache_set(key, payload, ttl=60)
        got = await redis_io.cache_get(key)
        ok = got == payload
        R.record("6. redis cache set+get", ok, f"key={key}")
    except Exception as exc:  # noqa: BLE001
        R.record("6. redis cache set+get", False, repr(exc))


def check_mqtt() -> None:
    s = get_settings()
    topic = "rfid/readers/R-01"
    received: list[str] = []
    payload = f"tag-{RUN_ID}"

    sub = mqtt.Client(client_id=f"bootstrap-sub-{RUN_ID}", protocol=mqtt.MQTTv311)
    pub = mqtt.Client(client_id=f"bootstrap-pub-{RUN_ID}", protocol=mqtt.MQTTv311)

    def on_message(_client, _userdata, msg) -> None:
        received.append(msg.payload.decode("utf-8"))

    sub.on_message = on_message
    try:
        sub.connect(s.mqtt_host, s.mqtt_port, keepalive=10)
        sub.subscribe(topic, qos=1)
        sub.loop_start()
        time.sleep(0.5)  # let subscription register

        pub.connect(s.mqtt_host, s.mqtt_port, keepalive=10)
        pub.loop_start()
        info = pub.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=5)

        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.1)

        ok = payload in received
        R.record("7. mqtt publish+subscribe", ok, f"topic={topic}")
    except Exception as exc:  # noqa: BLE001
        R.record("7. mqtt publish+subscribe", False, repr(exc))
    finally:
        for c in (pub, sub):
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
async def _async_part(record: AnprRead | None) -> None:
    await check_postgres_gates()
    await check_postgres_write_read(record)
    await check_redis()
    await db.dispose_all()
    await redis_io.close()


def main() -> int:
    print(f"JNPA UC-III bootstrap self-test (run_id={RUN_ID}, host={HOST})\n")

    check_env()
    record = check_kafka_roundtrip()
    asyncio.run(_async_part(record))
    check_mqtt()

    print(R.table())

    if R.all_ok:
        print("\nBOOTSTRAP OK")
        return 0
    failed = [n for n, ok, _ in R.rows if not ok]
    print(f"\nBOOTSTRAP FAILED — {len(failed)} check(s) failed: {', '.join(failed)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
