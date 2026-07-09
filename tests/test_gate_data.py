"""Tests for the gate-data capture + Auto-LEO reconciliation service.

These are pure-function tests of the deterministic seed corpus and the
``leo.reconcile`` join — no running server, no docker stack, no DB — so they
stay green in CI without infra. They cover Appendix C requirements #4 and #5:
the container/vehicle identity match, the e-seal / weight / LEO checks, and the
Customs flags & alerts.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "shared"
GATE_DIR = REPO_ROOT / "gate-data"
for p in (str(SHARED_DIR), str(GATE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gate_data import seed as seed_mod  # noqa: E402
from gate_data.leo import (  # noqa: E402
    FLAG_ESEAL_TAMPER,
    FLAG_LEO_MISSING,
    FLAG_RECORDS_MISSING,
    FLAG_WEIGHT_MISMATCH,
    customs_alerts,
    reconcile,
    reconcile_all,
)
from gate_data.seed import PINNED_CLEAN, PINNED_TAMPER  # noqa: E402
from jnpa_shared.schemas import is_valid_plate  # noqa: E402
from jnpa_shared.iso6346 import is_valid_container_no  # noqa: E402

_ISO6346_LEN = 11  # 3 owner letters + 'U' + 6-digit serial + check digit


def _dataset():
    return seed_mod.generate_dataset(seed_mod.TOTAL_CONTAINERS)


# ---------------------------------------------------------------------------
# Dataset shape / determinism
# ---------------------------------------------------------------------------
def test_dataset_is_schema_faithful():
    """Container numbers are ISO 6346-ish and vehicle plates are valid plates."""
    ds = _dataset()
    assert len(ds) >= seed_mod.TOTAL_CONTAINERS
    assert PINNED_CLEAN in ds and PINNED_TAMPER in ds

    for cn, rec in ds.items():
        # 4 letters + 7 digits, 4th char is the 'U' category id.
        assert len(cn) == _ISO6346_LEN, cn
        assert cn[:4].isalpha() and cn[3] == "U", cn
        assert cn[4:].isdigit(), cn
        # Every generated container number is a check-digit-VALID ISO 6346 number.
        assert is_valid_container_no(cn), cn
        # Every source record references the same container number.
        assert rec.eseal.container_no == cn
        assert rec.form13.container_no == cn
        assert rec.weighbridge.container_no == cn
        assert rec.icegate.container_no == cn
        # The haulage plate is a valid Indian plate, shared across the join.
        assert is_valid_plate(rec.vehicle_plate)
        # Form-13 shipping bill matches the ICEGATE shipping bill.
        assert rec.form13.shipping_bill_no == rec.icegate.shipping_bill_no


def test_reconcile_is_deterministic():
    """Same input -> identical AutoLeoResult, run after run."""
    ds1 = _dataset()
    ds2 = _dataset()
    for cn in sorted(ds1):
        r1 = reconcile(cn, dataset=ds1)
        r2 = reconcile(cn, dataset=ds2)
        assert r1.to_dict() == r2.to_dict(), cn


# ---------------------------------------------------------------------------
# (a) Clean container -> leo_ready, no flags
# ---------------------------------------------------------------------------
def test_clean_container_is_leo_ready():
    ds = _dataset()
    result = reconcile(PINNED_CLEAN, dataset=ds)

    assert result.container_no == PINNED_CLEAN
    assert result.leo_ready is True
    assert result.customs_flags == []
    # All gating checks pass.
    assert result.checks["id_match"] is True
    assert result.checks["eseal_ok"] is True
    assert result.checks["weight_ok"] is True
    assert result.checks["leo_present"] is True
    assert result.checks["weight_discrepancy_pct"] <= result.checks["weight_tolerance_pct"]
    # A clean container raises no Customs alerts.
    assert customs_alerts(result) == []


# ---------------------------------------------------------------------------
# (b) Tampered / mismatched container -> right flags, not leo_ready
# ---------------------------------------------------------------------------
def test_tampered_container_flags_and_blocked():
    ds = _dataset()
    result = reconcile(PINNED_TAMPER, dataset=ds)

    assert result.leo_ready is False
    assert FLAG_ESEAL_TAMPER in result.customs_flags
    assert result.checks["eseal_tamper_flag"] is True
    assert result.checks["eseal_ok"] is False

    # The Customs alert is shaped like a jnpa_shared Alert with the right kind.
    alerts = customs_alerts(result)
    assert alerts, "a tampered container must raise at least one Customs alert"
    tamper_alerts = [a for a in alerts if a["payload"]["flag"] == FLAG_ESEAL_TAMPER]
    assert tamper_alerts
    alert = tamper_alerts[0]
    assert alert["kind"] == "CUSTOMS_FLAG"
    assert alert["severity"] == "critical"
    assert alert["payload"]["container_no"] == PINNED_TAMPER
    assert alert["plate"] == result.vehicle_plate


def test_weight_mismatch_flag_fires_for_out_of_tolerance_weight():
    """A >2% weighbridge/Form-13 discrepancy raises WEIGHT_MISMATCH only."""
    ds = _dataset()
    # Find a container the seed marked as a weight-mismatch (and nothing else).
    target = None
    for cn, rec in ds.items():
        gross = rec.form13.gross_wt_kg
        disc = abs(rec.weighbridge.measured_wt_kg - gross) / gross * 100.0
        if disc > 2.0 and not rec.eseal.tamper_flag and rec.icegate.leo_status == "GRANTED":
            target = cn
            break
    assert target is not None, "seed should contain a pure weight-mismatch container"

    result = reconcile(target, dataset=ds)
    assert result.leo_ready is False
    assert result.customs_flags == [FLAG_WEIGHT_MISMATCH]
    assert result.checks["weight_discrepancy_pct"] > result.checks["weight_tolerance_pct"]

    wm = [a for a in customs_alerts(result) if a["payload"]["flag"] == FLAG_WEIGHT_MISMATCH]
    assert wm and wm[0]["payload"]["weight_discrepancy_pct"] == result.checks["weight_discrepancy_pct"]


def test_missing_leo_flag_fires_when_icegate_pending():
    """A PENDING ICEGATE LEO raises LEO_MISSING."""
    ds = _dataset()
    target = next(cn for cn, rec in ds.items() if rec.icegate.leo_status == "PENDING")
    result = reconcile(target, dataset=ds)
    assert result.leo_ready is False
    assert FLAG_LEO_MISSING in result.customs_flags
    assert result.checks["leo_present"] is False


def test_weight_tolerance_is_configurable():
    """Tightening the tolerance to 0% turns a within-tolerance container into a flag."""
    ds = _dataset()
    # PINNED_CLEAN passes at 2%; with a 0% tolerance any nonzero discrepancy trips.
    strict = reconcile(PINNED_CLEAN, dataset=ds, weight_tolerance_pct=0.0)
    if strict.checks["weight_discrepancy_pct"] > 0.0:
        assert FLAG_WEIGHT_MISMATCH in strict.customs_flags
        assert strict.leo_ready is False


# ---------------------------------------------------------------------------
# (c) Unknown container -> RECORDS_MISSING
# ---------------------------------------------------------------------------
def test_unknown_container_records_missing():
    ds = _dataset()
    result = reconcile("ZZZU0000000", dataset=ds)
    assert result.leo_ready is False
    assert result.customs_flags == [FLAG_RECORDS_MISSING]
    assert result.vehicle_plate is None


# ---------------------------------------------------------------------------
# (d) Container/vehicle identity match joins the records correctly
# ---------------------------------------------------------------------------
def test_id_match_joins_records_by_container_and_vehicle():
    ds = _dataset()
    cn = PINNED_CLEAN
    rec = ds[cn]
    result = reconcile(cn, dataset=ds)

    # The reconciler carries the weighbridge's plate forward as the identity,
    # and the join holds across all four source records.
    assert result.checks["id_match"] is True
    assert result.vehicle_plate == rec.weighbridge.vehicle_plate
    # The weighbridge weight and Form-13 weight surfaced in checks are the
    # actual captured values (the join read the right records).
    assert result.checks["form13_gross_wt_kg"] == rec.form13.gross_wt_kg
    assert result.checks["weighbridge_measured_wt_kg"] == rec.weighbridge.measured_wt_kg
    assert result.checks["icegate_leo_status"] == rec.icegate.leo_status


# ---------------------------------------------------------------------------
# Queue feed + flag aggregation
# ---------------------------------------------------------------------------
def test_reconcile_all_has_ready_and_blocked():
    ds = _dataset()
    results = reconcile_all(dataset=ds)
    assert len(results) == len(ds)
    ready = [r for r in results if r.leo_ready]
    blocked = [r for r in results if not r.leo_ready]
    # The deliberately-mismatched slices guarantee both buckets are non-empty.
    assert ready, "some containers must be clear for Auto-LEO"
    assert blocked, "some containers must be blocked by Customs flags"
    # Every blocked container has at least one flag; every ready one has none.
    assert all(r.customs_flags for r in blocked)
    assert all(not r.customs_flags for r in ready)
