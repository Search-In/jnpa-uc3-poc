"""Tests for the UC-III simulator-fidelity layer (Phases A & B).

Locks in three properties the demo prompt requires, all without infrastructure:

* **Faithful** — every backbone event can be wrapped in a CloudEvents 1.0
  envelope tagged ``sourcesystem=SIM`` with a ``rawref``, and consumers can
  unwrap it transparently (back-compat with bare payloads).
* **Deterministic** — one global ``SEED`` derives stable per-component seeds,
  and the per-condition OCR draw replays identically.
* **Controllable realism** — OCR confidence is ≥95% in CLEAR and degrades in
  FOG/NIGHT, so low-confidence flagging is demonstrable.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"),):
    if p not in sys.path:
        sys.path.insert(0, p)

from jnpa_shared import cloudevents as ce  # noqa: E402
from jnpa_shared import schemas as s  # noqa: E402
from jnpa_shared.config import Settings  # noqa: E402


# --------------------------------------------------------------------------- A
def test_cloudevents_roundtrip_and_extensions():
    gt = s.GateTransaction(gate_id="G-NSICT", direction="IN", vehicle_no="MH04AB1234")
    env = ce.wrap(
        gt,
        event_type="jnpa.gate.transaction",
        source_system="SIM",
        raw_ref="gate://G-NSICT",
        event_id="fixed-id",
        time_iso="2026-06-17T00:00:00+00:00",
    )
    assert ce.is_cloudevent(env)
    assert env["specversion"] == "1.0"
    assert ce.source_system_of(env) == "SIM"
    assert ce.raw_ref_of(env) == "gate://G-NSICT"
    inner = ce.unwrap(env)
    assert inner["gate_id"] == "G-NSICT" and inner["direction"] == "IN"


def test_cloudevents_unwrap_is_backcompat_for_bare_payload():
    # A pre-CloudEvents consumer calling unwrap() on a bare dict gets it back.
    assert ce.unwrap({"plate": "MH04AB1234"}) == {"plate": "MH04AB1234"}
    assert ce.source_system_of({"plate": "x"}) is None
    assert ce.raw_ref_of({"plate": "x"}) is None


def test_derive_seed_is_stable_and_distinct_per_component():
    cfg = Settings(seed=1337)
    assert cfg.derive_seed("truck") == cfg.derive_seed("truck")
    assert cfg.derive_seed("truck") != cfg.derive_seed("rfid")
    # Different global seed -> different stream.
    assert Settings(seed=1).derive_seed("truck") != Settings(seed=2).derive_seed("truck")


def test_offline_implies_mock():
    assert Settings(data_mode="mock").is_offline is True
    assert Settings(data_mode="live", offline=True).is_offline is True
    assert Settings(data_mode="live", offline=False).is_offline is False


# --------------------------------------------------------------------------- B
def test_ocr_confidence_clear_meets_95pct_and_fog_degrades():
    rng = random.Random(42)
    clear = [s.ocr_confidence_for_condition("CLEAR", rng) for _ in range(3000)]
    rng = random.Random(42)
    fog = [s.ocr_confidence_for_condition("FOG", rng) for _ in range(3000)]
    rng = random.Random(42)
    night = [s.ocr_confidence_for_condition("NIGHT", rng) for _ in range(3000)]

    mean_clear = sum(clear) / len(clear)
    mean_fog = sum(fog) / len(fog)
    mean_night = sum(night) / len(night)

    assert mean_clear >= 0.95, mean_clear
    assert mean_fog < mean_clear
    assert mean_night < mean_clear
    # Low-confidence flagging is actually exercised in poor conditions.
    assert any(c < s.OCR_LOW_CONF_THRESHOLD for c in fog)


def test_ocr_confidence_is_deterministic_under_seed():
    a = [s.ocr_confidence_for_condition("FOG", random.Random(7)) for _ in range(1)]
    b = [s.ocr_confidence_for_condition("FOG", random.Random(7)) for _ in range(1)]
    assert a == b


def test_anpr_read_low_confidence_flag_and_default_condition():
    a = s.AnprRead(camera_id="C1", plate="MH04AB1234", conf=0.85)
    assert a.condition == s.Condition.CLEAR
    assert a.low_confidence is True
    b = s.AnprRead(camera_id="C1", plate="MH04AB1234", conf=0.97)
    assert b.low_confidence is False


def test_telemetry_source_fallback_chain_values():
    assert {x.value for x in s.TelemetrySource} == {"APP_GPS", "ULIP_RELAY", "WEB_CHECKIN"}
    tt = s.TruckTelemetry(device_id="D1", lat=18.9, lon=72.9, source="WEB_CHECKIN")
    assert tt.source == s.TelemetrySource.WEB_CHECKIN


def test_new_event_types_construct_and_serialize():
    ps = s.ParkingState(facility_id="CPP", capacity=500, occupied=380)
    assert ps.available_slots == 120
    fv = s.FaceVerification(driver_id="SYN-001", gate_id="G1", match_score=0.93)
    assert fv.synthetic is True  # DPDP: synthetic faces only
    gv = s.GeofenceViolation(vehicle_no="MH04AB1234", zone_id="Z1",
                             type="NO_PARKING", duration_s=42.0)
    assert gv.type == s.GeofenceType.NO_PARKING
    ft = s.FastTagTxn(tag_id="T1", plaza_id="PLZ-348-1", amount=120.0, balance=880.0)
    assert ft.amount == 120.0
    # all JSON-serialisable for the backbone
    for m in (ps, fv, gv, ft):
        assert isinstance(m.model_dump(mode="json"), dict)
