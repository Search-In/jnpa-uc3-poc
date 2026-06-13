"""Offline unit tests for the jnpa_shared package.

These run without any running infrastructure (`make test`) so they validate the
shared contracts — schemas, corridor geometry, config loading, and the JSON
codecs used on the Kafka path. The live end-to-end checks live in
`scripts/bootstrap_check.py`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the shared package importable without an editable install.
REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import pytest  # noqa: E402

from jnpa_shared import corridor  # noqa: E402
from jnpa_shared import kafka_io  # noqa: E402
from jnpa_shared.config import Settings  # noqa: E402
from jnpa_shared.schemas import (  # noqa: E402
    Alert,
    AnprRead,
    RfidRead,
    Scenario,
    TrafficSnapshot,
    TruckTelemetry,
    VahanRecord,
    VehicleClass,
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
def test_anpr_read_roundtrips_through_json():
    read = AnprRead(camera_id="CAM-NSICT-OVW", plate="MH04AB1234", conf=0.91,
                    vehicle_class=VehicleClass.HGV)
    raw = kafka_io.encode_value(read)
    decoded = kafka_io.decode_value(raw)
    assert decoded["plate"] == "MH04AB1234"
    assert decoded["vehicle_class"] == "HGV"
    # Re-parse to confirm the wire form is a valid model again.
    again = AnprRead(**decoded)
    assert again.plate == read.plate
    assert again.conf == pytest.approx(0.91)


def test_anpr_conf_bounds_enforced():
    with pytest.raises(Exception):
        AnprRead(camera_id="C", plate="P", conf=1.5)


def test_all_event_models_serialize():
    models = [
        AnprRead(camera_id="C", plate="P", conf=0.5),
        VahanRecord(plate="MH04AB1234", rc_type="HGV"),
        RfidRead(reader_id="R-01", tag_id="T-1", rssi=-55.0),
        TruckTelemetry(device_id="D-1", lat=18.9, lon=72.9, speed_kmh=40),
        TrafficSnapshot(segment_id="SEG-00", speed_kmh=22.0, jam_factor=4.0, source="here"),
        Alert(kind="overspeed", severity="warning", gate_id="G-NSICT"),
        Scenario(id="S-1", name="surge"),
    ]
    for m in models:
        raw = kafka_io.encode_value(m)
        assert kafka_io.decode_value(raw)  # decodes to a non-empty dict


def test_utc_timestamps_are_timezone_aware():
    read = AnprRead(camera_id="C", plate="P", conf=0.5)
    assert read.ts.tzinfo is not None
    assert read.ts.utcoffset() == timezone.utc.utcoffset(datetime.now(timezone.utc))


# --------------------------------------------------------------------------
# Corridor geometry
# --------------------------------------------------------------------------
def test_corridor_has_24_waypoints():
    assert len(corridor.WAYPOINTS) == 24


def test_corridor_endpoints_match_spec():
    assert corridor.WAYPOINTS[0] == (18.9489, 72.9492)   # JNPA Gate-1
    assert corridor.WAYPOINTS[-1] == (18.78, 73.08)      # Karal Phata


def test_segments_are_reasonable_length():
    assert len(corridor.segments) >= 1
    for seg in corridor.segments:
        # Segments target the 1.5–2 km band; allow slack for the final stub.
        assert 0.4 <= seg.length_km <= 2.6, f"{seg.id} = {seg.length_km} km"


def test_nearest_segment_returns_a_segment():
    seg = corridor.nearest_segment(18.86, 73.01)
    assert seg is not None
    assert seg.id.startswith("SEG-")


def test_total_corridor_length_plausible():
    # Straight-line port→Karal is ~22–24 km; the polyline is a bit longer.
    total = corridor.total_length_km()
    assert 20.0 <= total <= 35.0, total


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def test_settings_defaults_and_helpers(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER", "mosquitto:1883")
    monkeypatch.setenv("KAFKA_BROKERS", "kafka:9092,kafka2:9092")
    s = Settings()
    assert s.mqtt_host == "mosquitto"
    assert s.mqtt_port == 1883
    assert s.kafka_first_broker == "kafka:9092"
    assert s.port_lat == pytest.approx(18.9489)
