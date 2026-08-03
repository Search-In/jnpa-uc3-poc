"""UC-III lifecycle bus tests (Phase 6).

The contract that matters operationally: publishing a milestone must NEVER be
able to fail the underlying mutation, and it must reach the WS hub even when no
Kafka broker exists (the demo/dev profile).
"""
from __future__ import annotations

import pytest

from services import lifecycle_bus


@pytest.fixture(autouse=True)
def _clean():
    lifecycle_bus.reset_for_tests()
    yield
    lifecycle_bus.reset_for_tests()


@pytest.mark.asyncio
async def test_publish_without_broker_still_broadcasts_to_ws(monkeypatch):
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    seen: list[tuple[str, dict]] = []

    async def fake_ws(channel, payload):
        seen.append((channel, payload))

    lifecycle_bus.set_ws_broadcaster(fake_ws)
    sent = await lifecycle_bus.publish("cargo.released", {"container_number": "MRKU5014206"})

    assert sent == {"kafka": False, "ws": True}
    assert seen[0][0] == "uc3_lifecycle"
    assert seen[0][1]["event"] == "cargo.released"
    assert seen[0][1]["container_number"] == "MRKU5014206"


@pytest.mark.asyncio
async def test_no_producer_is_created_when_no_broker_is_configured(monkeypatch):
    """Guards the regression where every unit test spun up a retrying producer."""
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)

    def explode(*_a, **_kw):  # pragma: no cover — must never be reached
        raise AssertionError("producer must not be constructed without a broker")

    monkeypatch.setattr("jnpa_shared.kafka_io.get_producer", explode)
    assert await lifecycle_bus.publish("job.assigned", {"job_id": 1}) == {"kafka": False, "ws": False}


@pytest.mark.asyncio
async def test_ws_failure_is_swallowed(monkeypatch):
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)

    async def broken_ws(channel, payload):
        raise RuntimeError("hub down")

    lifecycle_bus.set_ws_broadcaster(broken_ws)
    # Must not raise: a broadcast failure cannot fail a container release.
    assert await lifecycle_bus.publish("cargo.released", {"container_number": "X"}) == {
        "kafka": False, "ws": False}


@pytest.mark.asyncio
async def test_bus_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("KAFKA_BROKERS", "kafka:9092")
    monkeypatch.setenv("UC3_BUS_ENABLED", "0")

    def explode(*_a, **_kw):  # pragma: no cover
        raise AssertionError("producer must not be constructed when disabled")

    monkeypatch.setattr("jnpa_shared.kafka_io.get_producer", explode)
    assert (await lifecycle_bus.publish("job.assigned", {"job_id": 2}))["kafka"] is False
