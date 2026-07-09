"""Tests for the cross-UC assumptions single-source (shared/assumptions.json)."""
from __future__ import annotations

from jnpa_shared.assumptions import get, load_assumptions
from jnpa_shared.kpi import KPI_TARGETS


def test_loads_and_has_core_sections():
    doc = load_assumptions()
    for section in ("port", "gates", "corridor", "vehicles", "vessel", "kpi_targets"):
        assert section in doc, section


def test_key_assumptions_present():
    assert get("gates", "throughput_target_vph") == 60.0
    assert get("corridor", "length_km") == 40
    assert get("vehicles", "trucking_app_devices") == 20000
    assert get("port", "poc_total_containers") == 200
    assert get("corridor")["road"]["per_lane_capacity_vph"] == 1800


def test_kpi_mirror_matches_authoritative_targets():
    """The JSON mirror must not diverge from jnpa_shared.kpi.KPI_TARGETS."""
    mirror = get("kpi_targets")
    for key, kt in KPI_TARGETS.items():
        assert key in mirror, key
        assert mirror[key]["target"] == kt.target, key
        assert mirror[key]["baseline"] == kt.baseline, key
        assert mirror[key]["direction"] == kt.direction, key
