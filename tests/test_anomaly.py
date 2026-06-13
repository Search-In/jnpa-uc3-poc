"""Tests for the behavioural anomaly detector (UC-III Sub-Criterion 2C).

Layers, mirroring the other AI services' test style:

  * **Pure-logic tests** (always run, no infra): the shared no-park-zone geometry,
    each rule against its synthetic scenario, the engine's exactly-one-alert
    guarantee, route-deviation maths, and the AE feature extraction. None of
    these import kafka/psycopg/cv2/minio, so they run on a bare host.

  * **AE tests** (skipped unless torch is importable): a tiny end-to-end train +
    score showing a looping trajectory scores above the normal-track threshold.

  * **Integration test** (skipped unless the docker stack's anomaly service is up
    on localhost:8321): hits /health and /alerts/recent.

The headline assertion from the spec: the synthetic wrong-way / abandoned /
illegal-park scenarios each produce *exactly one* alert of the correct kind.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT / "ai")):
    if p not in sys.path:
        sys.path.insert(0, p)

from anomaly.config import AnomalyConfig  # noqa: E402
from anomaly.engine import AnomalyEngine, KIND_ANOMALOUS_TRAJECTORY  # noqa: E402
from anomaly.rules import abandoned, parking, route_deviation, wrongway  # noqa: E402
from anomaly.rules import allowed_bearing, CAMERA_ALLOWED_BEARING  # noqa: E402
from anomaly import synthetic  # noqa: E402
from anomaly.types import angular_diff_deg, bearing_deg  # noqa: E402

try:
    import torch  # noqa: F401

    HAVE_TORCH = True
except Exception:  # noqa: BLE001
    HAVE_TORCH = False


def _cfg(**kw) -> AnomalyConfig:
    cfg = AnomalyConfig()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------- zones
def test_no_park_zones_defined():
    from jnpa_shared import corridor

    assert len(corridor.NO_PARK_ZONES) == 6
    ids = [z.id for z in corridor.NO_PARK_ZONES]
    assert len(set(ids)) == 6  # unique


def test_point_in_polygon_and_zone_lookup():
    from jnpa_shared import corridor

    z = corridor.NO_PARK_ZONES[0]
    c = z.centroid
    assert corridor.point_in_polygon(c[0], c[1], z.polygon)
    found = corridor.zone_for_point(c[0], c[1])
    assert found is not None and found.id == z.id
    # A point far outside every zone.
    assert corridor.zone_for_point(0.0, 0.0) is None


# --------------------------------------------------------------------------- geometry
def test_bearing_and_angular_diff():
    # Due-north step -> bearing ~0; due-east step -> ~90.
    assert abs(bearing_deg((0.0, 0.0), (1.0, 0.0)) - 0.0) < 1.0
    assert abs(bearing_deg((0.0, 0.0), (0.0, 1.0)) - 90.0) < 1.0
    assert angular_diff_deg(10.0, 350.0) == 20.0
    assert angular_diff_deg(0.0, 180.0) == 180.0


def test_allowed_bearing_fallback():
    assert allowed_bearing("CAM-COR-01") == CAMERA_ALLOWED_BEARING["CAM-COR-01"]
    assert allowed_bearing("UNKNOWN-CAM") == 135.0
    assert allowed_bearing(None) == 135.0


# --------------------------------------------------------------------------- rules: wrong-way
def test_wrongway_fires_on_against_traffic_track():
    cfg = _cfg()
    track = synthetic.wrongway_track(camera_id="CAM-COR-01")
    alert = wrongway.evaluate(track, cfg)
    assert alert is not None
    assert alert.kind == "WRONG_WAY"
    assert alert.severity == "critical"
    assert alert.payload["max_divergence_deg"] > cfg.wrongway_divergence_deg
    assert alert.payload["sustained_s"] >= cfg.wrongway_hold_s


def test_wrongway_silent_on_with_traffic_track():
    cfg = _cfg()
    # A normal down-corridor track heads with traffic -> no wrong-way.
    track = synthetic.normal_tracks(1, seq_len=20)[0]
    assert wrongway.evaluate(track, cfg) is None


def test_wrongway_needs_sustained_divergence():
    """A divergence shorter than the hold window must not fire."""
    cfg = _cfg(wrongway_hold_s=30.0)  # demand 30 s; the scenario only holds ~11 s
    track = synthetic.wrongway_track()
    assert wrongway.evaluate(track, cfg) is None


# --------------------------------------------------------------------------- rules: abandoned
def test_abandoned_fires_outside_zones():
    cfg = _cfg()
    track = synthetic.abandoned_track()
    alert = abandoned.evaluate(track, cfg)
    assert alert is not None
    assert alert.kind == "ABANDONED"
    assert alert.payload["dwell_s"] >= cfg.abandoned_dwell_s


def test_abandoned_silent_inside_zone():
    """A vehicle parked inside a no-park zone is illegal-parking, not abandoned."""
    cfg = _cfg()
    track = synthetic.illegal_park_track()  # sits inside NPZ-YJUNCTION
    assert abandoned.evaluate(track, cfg) is None


# --------------------------------------------------------------------------- rules: parking
def test_illegal_parking_fires_in_zone_with_escalation():
    cfg = _cfg()
    track = synthetic.illegal_park_track()
    alert = parking.evaluate(track, cfg)
    assert alert is not None
    assert alert.kind == "ILLEGAL_PARKING"
    assert alert.payload["zone_id"].startswith("NPZ-")
    assert alert.payload["escalation"] == parking.ESCALATION_WARNING


def test_parking_escalation_levels():
    cfg = _cfg()
    assert parking._escalation(cfg.parking_warning_s, cfg)[0] == parking.ESCALATION_WARNING
    assert parking._escalation(cfg.parking_critical_s, cfg)[0] == parking.ESCALATION_CRITICAL
    assert parking._escalation(cfg.parking_police_s, cfg)[0] == parking.ESCALATION_POLICE
    # The police level carries critical severity.
    assert parking._escalation(cfg.parking_police_s, cfg)[1] == "critical"


# --------------------------------------------------------------------------- rules: route deviation
def test_route_deviation_offroute():
    cfg = _cfg()
    track, route = synthetic.route_deviation_track()
    alert = route_deviation.evaluate(track, route, cfg)
    assert alert is not None
    assert alert.kind == "ROUTE_DEVIATION"
    assert alert.payload["offroute_m"] > cfg.route_offroute_m
    assert "offroute" in alert.payload["reasons"]


def test_route_deviation_silent_on_route():
    cfg = _cfg()
    _, route = synthetic.route_deviation_track()
    # A truck that actually follows its assigned route does not deviate.
    from anomaly.types import Track, TrackPoint
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    track = Track(track_id="ONROUTE", device_id="T1")
    for i, (lat, lon) in enumerate(route):
        track.add(TrackPoint(ts=t0 + timedelta(seconds=i * 5), lat=lat, lon=lon,
                             speed_kmh=45.0))
    assert route_deviation.evaluate(track, route, cfg) is None


def test_cosine_distance_opposite():
    # Two opposite polylines -> cosine distance near 2.0.
    fwd = [(18.90, 72.97), (18.89, 72.98), (18.88, 72.99)]
    rev = list(reversed(fwd))
    d = route_deviation.cosine_distance(fwd, rev)
    assert d is not None and d > 1.9


# --------------------------------------------------------------------------- engine: exactly one
@pytest.mark.parametrize(
    "builder,expected_kind",
    [
        (synthetic.wrongway_track, "WRONG_WAY"),
        (synthetic.abandoned_track, "ABANDONED"),
        (synthetic.illegal_park_track, "ILLEGAL_PARKING"),
    ],
)
def test_engine_emits_exactly_one_alert(builder, expected_kind):
    """The spec's headline: each synthetic scenario -> exactly one alert of the
    correct kind (no cross-firing across rules)."""
    cfg = _cfg()
    engine = AnomalyEngine(cfg)  # no sink/evidence/ae -> pure detection
    track = builder()
    alerts = engine.evaluate_track(track, emit=False)
    assert len(alerts) == 1, [a.kind for a in alerts]
    assert alerts[0].kind == expected_kind


def test_engine_route_deviation_exactly_one():
    cfg = _cfg()
    engine = AnomalyEngine(cfg)
    track, route = synthetic.route_deviation_track()
    alerts = engine.evaluate_track(track, route=route, emit=False)
    assert len(alerts) == 1
    assert alerts[0].kind == "ROUTE_DEVIATION"


def test_engine_dedup_cooldown():
    """A track that stays anomalous must emit once per cooldown window."""
    cfg = _cfg()
    engine = AnomalyEngine(cfg, cooldown_s=60.0)
    track = synthetic.wrongway_track()
    first = engine.evaluate_track(track, emit=False)
    second = engine.evaluate_track(track, emit=False)  # same track, within cooldown
    assert len(first) == 1
    assert len(second) == 0


# --------------------------------------------------------------------------- AE features
def test_ae_feature_shape():
    from anomaly.autoencoder.features import N_FEATURES, batch_features, track_features

    cfg = _cfg()
    track = synthetic.looping_track()
    feats = track_features(track, cfg.ae_seq_len)
    assert feats.shape == (cfg.ae_seq_len, N_FEATURES)
    batch = batch_features(synthetic.normal_tracks(5, seq_len=cfg.ae_seq_len), cfg.ae_seq_len)
    assert batch.shape == (5, cfg.ae_seq_len, N_FEATURES)


# --------------------------------------------------------------------------- AE model (torch)
@pytest.mark.skipif(not HAVE_TORCH, reason="torch not installed")
def test_ae_train_flags_looping_trajectory(tmp_path):
    """Train on normal tracks; a slow-looping track should score above threshold."""
    from anomaly.autoencoder.features import batch_features, track_features
    from anomaly.autoencoder.model import TrajectoryAutoencoder

    cfg = _cfg(ae_epochs=30, weights_dir=str(tmp_path))
    normals = synthetic.normal_tracks(256, seq_len=cfg.ae_seq_len)
    ae = TrajectoryAutoencoder(cfg)
    metrics = ae.train(batch_features(normals, cfg.ae_seq_len))
    assert metrics["final_loss"] <= metrics["first_loss"]
    assert ae.loaded and ae.threshold > 0.0

    # A clearly-anomalous looping trajectory should exceed the threshold.
    loop_feats = track_features(synthetic.looping_track(), cfg.ae_seq_len)[None, ...]
    res = ae.score_batch(loop_feats)[0]
    assert res.is_anomalous, f"recon {res.error} <= threshold {res.threshold}"


@pytest.mark.skipif(not HAVE_TORCH, reason="torch not installed")
def test_engine_ae_suppressed_when_rule_fires(tmp_path):
    """With the AE loaded, a rule-tripping track still yields exactly one alert
    (the specific rule), and a looping track yields only ANOMALOUS_TRAJECTORY."""
    from anomaly.autoencoder.features import batch_features
    from anomaly.autoencoder.model import TrajectoryAutoencoder

    cfg = _cfg(ae_epochs=25, weights_dir=str(tmp_path))
    ae = TrajectoryAutoencoder(cfg)
    ae.train(batch_features(synthetic.normal_tracks(256, seq_len=cfg.ae_seq_len), cfg.ae_seq_len))

    # Wrong-way trips a rule -> AE alert suppressed -> exactly one WRONG_WAY.
    eng = AnomalyEngine(cfg, autoencoder=ae)
    alerts = eng.evaluate_track(synthetic.wrongway_track(), emit=False)
    assert [a.kind for a in alerts] == ["WRONG_WAY"]

    # Looping trips no rule -> the AE is the only thing that catches it.
    eng2 = AnomalyEngine(cfg, autoencoder=ae)
    loop_alerts = eng2.evaluate_track(synthetic.looping_track(), emit=False)
    assert [a.kind for a in loop_alerts] == [KIND_ANOMALOUS_TRAJECTORY]


@pytest.mark.skipif(not HAVE_TORCH, reason="torch not installed")
def test_ae_save_load_roundtrip(tmp_path):
    from anomaly.autoencoder.features import batch_features
    from anomaly.autoencoder.model import TrajectoryAutoencoder

    cfg = _cfg(ae_epochs=5, weights_dir=str(tmp_path))
    ae = TrajectoryAutoencoder(cfg)
    ae.train(batch_features(synthetic.normal_tracks(80, seq_len=cfg.ae_seq_len), cfg.ae_seq_len))
    ae.save()
    thr = ae.threshold

    ae2 = TrajectoryAutoencoder(cfg)
    assert ae2.load()
    assert abs(ae2.threshold - thr) < 1e-9


# --------------------------------------------------------------------------- frame bus
def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def test_frame_message_parse():
    """The frame-bus parser decodes raw stream fields back into a FrameMessage."""
    from jnpa_shared import frame_bus as fb

    cons = fb.FrameBusConsumer(["CAM-COR-01"])
    msg = cons._parse(
        "frames.CAM-COR-01", "1-0",
        {b"camera_id": b"CAM-COR-01", b"ts": b"2026-06-13T10:00:00+00:00",
         b"seq": b"7", b"jpeg": b"\xff\xd8\xff\xe0jpeg"},
    )
    assert msg.camera_id == "CAM-COR-01"
    assert msg.seq == 7
    assert msg.jpeg.startswith(b"\xff\xd8")
    assert msg.ts.isoformat() == "2026-06-13T10:00:00+00:00"


@pytest.mark.skipif(not _port_open("localhost", 6379), reason="redis not running")
def test_frame_bus_roundtrip():
    """Produce jpeg frames to a stream and read them back via the consumer."""
    from jnpa_shared import frame_bus as fb

    cam = "TEST-CAM-PYTEST"
    url = "redis://localhost:6379/0"
    import redis

    rc = redis.from_url(url, decode_responses=False)
    rc.delete(fb.stream_key(cam))
    prod = fb.FrameBusProducer(url=url, maxlen=600)
    cons = fb.FrameBusConsumer([cam], url=url, start="0")
    try:
        for i in range(3):
            assert prod.publish(cam, f"JPEG{i}".encode()) is not None
        msgs = cons.read(count=10)
        assert [m.jpeg.decode() for m in msgs] == ["JPEG0", "JPEG1", "JPEG2"]
        latest = cons.latest(cam)
        assert latest is not None and latest[1].jpeg.decode() == "JPEG2"
    finally:
        rc.delete(fb.stream_key(cam))
        prod.close()
        cons.close()


# --------------------------------------------------------------------------- integration
@pytest.mark.skipif(not _port_open("localhost", 8321), reason="anomaly service not running")
def test_health_and_recent_endpoints():
    import httpx

    h = httpx.get("http://localhost:8321/health", timeout=5).json()
    assert h["status"] == "ok" and h["service"] == "anomaly"

    r = httpx.get("http://localhost:8321/alerts/recent?since=PT1H", timeout=10)
    r.raise_for_status()
    assert isinstance(r.json(), list)
