"""Tests for the congestion forecaster (UC-III Sub-Criterion 2B).

Two layers:

  * **Pure-logic tests** (always run, no infra): corridor graph construction,
    synthetic-history commute shape, feature-window / onset-label correctness,
    the metrics module, and the SourceManager cascade + stale fallback (with a
    fake Redis + monkeypatched sources). The model + a short training run are
    exercised only when torch is importable.

  * **Integration test** (skipped unless the docker stack's congestion service
    is reachable on localhost:8311): hits /predict and /metrics and asserts the
    bid verification surface.
"""
from __future__ import annotations

import asyncio
import math
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT / "ai")):
    if p not in sys.path:
        sys.path.insert(0, p)

from congestion.config import CongestionConfig  # noqa: E402
from congestion.features import FEATURE_NAMES, FeatureBuilder  # noqa: E402
from congestion.graph import build_corridor_graph  # noqa: E402
from congestion.synthetic import SyntheticHistory  # noqa: E402
from congestion import metrics as M  # noqa: E402

try:
    import torch  # noqa: F401

    HAVE_TORCH = True
except Exception:  # noqa: BLE001
    HAVE_TORCH = False

REF_NOW = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def _small_cfg(**kw) -> CongestionConfig:
    cfg = CongestionConfig()
    cfg.history_days = 2
    cfg.window = 30
    cfg.aggregate_s = 60
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------- graph
def test_graph_matches_corridor_segments():
    from jnpa_shared import corridor

    g = build_corridor_graph()
    assert g.num_nodes == len(corridor.segments)
    assert g.segment_ids == [s.id for s in corridor.segments]
    # self-loop + both directions per adjacency
    expected_edges = g.num_nodes + 2 * (g.num_nodes - 1)
    assert g.edge_index.shape[1] == expected_edges
    assert g.edge_attr.shape == (expected_edges, 2)
    # lane count tapers from the port end (more lanes) to the junction end.
    assert g.meta[0].lane_count >= g.meta[-1].lane_count


def test_graph_has_signalised_segments():
    g = build_corridor_graph()
    assert any(m.signalised for m in g.meta), "expected at least one signalised segment"


# --------------------------------------------------------------------------- synthetic
def test_synthetic_history_shape_and_peaks():
    cfg = _small_cfg()
    g = build_corridor_graph()
    rows = SyntheticHistory(cfg, g).generate(end=REF_NOW)
    steps = int(cfg.history_days * 24 * 3600 / cfg.aggregate_s)
    assert len(rows) == steps * g.num_nodes
    # jam_factor in range; speeds positive.
    assert all(0.0 <= r.jam_factor <= 10.0 for r in rows[:1000])
    assert all(r.speed_kmh > 0 for r in rows[:1000])


def test_synthetic_respects_commute_peaks():
    """Mean jam during the evening peak window must exceed a 03:00 night floor."""
    cfg = _small_cfg(history_days=3)
    g = build_corridor_graph()
    rows = SyntheticHistory(cfg, g).generate(end=REF_NOW)
    from datetime import timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    peak, night = [], []
    for r in rows:
        h = r.ts.astimezone(ist).hour
        if h == 19:
            peak.append(r.jam_factor)
        elif h == 3:
            night.append(r.jam_factor)
    assert peak and night
    assert sum(peak) / len(peak) > sum(night) / len(night)


# --------------------------------------------------------------------------- features
def test_feature_matrix_dims_and_labels():
    cfg = _small_cfg()
    g = build_corridor_graph()
    rows = SyntheticHistory(cfg, g).generate(end=REF_NOW)
    fb = FeatureBuilder(cfg, g)
    fm = fb.build_matrix(rows)
    assert fm.features.shape[1] == g.num_nodes
    assert fm.features.shape[2] == len(FEATURE_NAMES) == cfg.in_features
    X, Y, mask, ends = fb.make_supervised(fm)
    assert X.shape[1:] == (cfg.window, g.num_nodes, len(FEATURE_NAMES))
    assert Y.shape == (X.shape[0], g.num_nodes)
    assert mask.shape == Y.shape
    # onset label only positive where not currently congested (mask covers it).
    assert ((Y == 1) & (mask == 0)).sum() == 0
    # features normalised into a sane band.
    assert fm.features.min() >= -1.01 and fm.features.max() <= 1.6


def test_onset_excludes_already_congested():
    """A segment congested at window-end must be masked out (not a positive)."""
    cfg = _small_cfg()
    g = build_corridor_graph()
    rows = SyntheticHistory(cfg, g).generate(end=REF_NOW)
    fb = FeatureBuilder(cfg, g)
    fm = fb.build_matrix(rows)
    X, Y, mask, ends = fb.make_supervised(fm)
    assert mask.sum() > 0
    assert Y.sum() > 0, "synthetic history should contain onset events"


# --------------------------------------------------------------------------- metrics
def test_metrics_perfect_and_auc():
    import numpy as np

    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.8, 0.9])
    prec, rec, f1 = M.precision_recall_f1(y, p, thr=0.5)
    assert prec == rec == f1 == 1.0
    assert M.roc_auc(y, p) == 1.0
    thr, best = M.best_threshold(y, p)
    assert best == 1.0


def test_metrics_summary_keys():
    import numpy as np

    y = np.array([0, 1, 0, 1, 1, 0])
    p = np.array([0.2, 0.7, 0.6, 0.9, 0.3, 0.1])
    s = M.summary(y, p, thr=0.5)
    for k in ("congestion_onset_f1", "precision", "recall", "roc_auc",
              "support_positive", "tp", "fp", "fn", "tn"):
        assert k in s


# --------------------------------------------------------------------------- sources
class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        self.store[k] = v


def test_source_manager_cascade_and_stale(monkeypatch):
    from congestion.sources import SourceManager
    from congestion.sources.base import SpeedReading

    cfg = _small_cfg()
    g = build_corridor_graph()
    seg = g.meta[0]

    fake = _FakeRedis()
    import jnpa_shared.redis_io as rio

    async def _cset(k, v, ttl=90):
        import json
        await fake.set(k, json.dumps(v))

    async def _cget(k):
        import json
        raw = await fake.store.get(k) if False else fake.store.get(k)
        return json.loads(raw) if raw else None

    monkeypatch.setattr(rio, "cache_set", _cset)
    monkeypatch.setattr(rio, "cache_get", _cget)
    monkeypatch.setattr(rio, "get_client", lambda url=None: fake)

    mgr = SourceManager(cfg)

    # 1. first source succeeds -> cached, source preserved
    async def ok(self, s):
        return SpeedReading(s.id, 40.0, 3.0, self.name, "2026-01-01T00:00:00+00:00")

    for src in mgr.sources:
        monkeypatch.setattr(type(src), "get_segment_speed", ok, raising=False)

    r = asyncio.run(mgr.get(seg))
    assert r.speed_kmh == 40.0 and not r.stale
    assert r.source == mgr.sources[0].name

    # 2. all sources fail, but a last-known value exists -> stale fallback
    fake.store.pop(mgr._cache_key(seg.id), None)  # expire the 90s entry

    async def boom(self, s):
        raise RuntimeError("provider down")

    for src in mgr.sources:
        monkeypatch.setattr(type(src), "get_segment_speed", boom, raising=False)

    r2 = asyncio.run(mgr.get(seg))
    assert r2.stale is True
    assert r2.speed_kmh == 40.0  # the last-known value


def test_source_synthetic_when_unconfigured():
    """An adapter with no API key returns a synthetic reading tagged with its
    own name (keeps the cascade alive offline)."""
    from congestion.sources.google import GoogleSource

    g = build_corridor_graph()
    src = GoogleSource(api_key="", free_flow_kmh=55.0)
    r = asyncio.run(src.get_segment_speed(g.meta[0]))
    assert r is not None and r.source == "google"
    assert 0.0 <= r.jam_factor <= 10.0


# --------------------------------------------------------------------------- model
@pytest.mark.skipif(not HAVE_TORCH, reason="torch not installed")
def test_model_forward_shapes():
    import torch
    from congestion.model import build_model

    cfg = _small_cfg()
    g = build_corridor_graph()
    m = build_model(cfg, edge_dim=int(g.edge_attr.shape[1]))
    x1 = torch.randn(cfg.window, g.num_nodes, cfg.in_features)
    assert tuple(m(x1, g.edge_index, g.edge_attr).shape) == (g.num_nodes,)
    xb = torch.randn(4, cfg.window, g.num_nodes, cfg.in_features)
    assert tuple(m(xb, g.edge_index, g.edge_attr).shape) == (4, g.num_nodes)
    prob = m.predict_proba(x1, g.edge_index, g.edge_attr)
    assert float(prob.min()) >= 0.0 and float(prob.max()) <= 1.0


@pytest.mark.skipif(not HAVE_TORCH, reason="torch not installed")
def test_model_can_overfit_tiny():
    """A few steps of training must reduce loss on a tiny batch (learning works)."""
    import torch
    from congestion.model import build_model

    cfg = _small_cfg()
    g = build_corridor_graph()
    m = build_model(cfg, edge_dim=int(g.edge_attr.shape[1]))
    x = torch.randn(8, cfg.window, g.num_nodes, cfg.in_features)
    y = (torch.rand(8, g.num_nodes) > 0.5).float()
    opt = torch.optim.Adam(m.parameters(), lr=1e-2)
    loss_fn = torch.nn.BCEWithLogitsLoss()
    first = last = None
    for i in range(40):
        opt.zero_grad()
        loss = loss_fn(m(x, g.edge_index, g.edge_attr), y)
        loss.backward()
        opt.step()
        if i == 0:
            first = float(loss.detach())
        last = float(loss.detach())
    assert last < first


# --------------------------------------------------------------------------- integration
def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _port_open("localhost", 8311), reason="congestion service not running")
def test_predict_and_metrics_endpoints():
    import httpx

    g = build_corridor_graph()
    r = httpx.post("http://localhost:8311/predict", json={"horizon_min": 15}, timeout=20)
    r.raise_for_status()
    probs = r.json()
    assert len(probs) == g.num_nodes
    assert all(0.0 <= v <= 1.0 for v in probs.values())

    m = httpx.get("http://localhost:8311/metrics", timeout=10).json()
    if "congestion_onset_f1" in m:
        assert 0.0 <= m["congestion_onset_f1"] <= 1.0
