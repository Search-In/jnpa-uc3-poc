"""UC3-028 violation queue + UC3-029 hash-chained audit trail.

Pure tests of the rules these two tickets turn on — the lifecycle state machine
and the chain hash — plus a regression guard on the defect that stopped any case
from ever being filed.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from gateway import enforcement  # noqa: E402
from gateway.routers.reports import POLICE_KINDS, _CHALLAN  # noqa: E402


# ----------------------------------------------------------------- UC3-028
def test_five_violation_types_are_configured():
    """The ticket configures FIVE violation types; the console had four."""
    assert len(POLICE_KINDS) == 5
    assert "ABANDONED_VEHICLE" in POLICE_KINDS
    # Every type must be actionable — a kind with no fine schedule cannot be
    # filed, so a missing entry would be an empty row in the console.
    for kind in POLICE_KINDS:
        assert kind in _CHALLAN, kind
        assert _CHALLAN[kind]["fine_inr"] > 0
        assert _CHALLAN[kind]["section"]


def test_audit_row_lock_targets_the_real_primary_key():
    """Regression guard for the defect that made the whole console unusable.

    ``_audit`` locked ``core.violation_case.id``, but that table's primary key is
    ``case_id``. Every commit goes through the audit writer, so the wrong column
    raised UndefinedColumnError on the first write and NO case could ever be
    filed — core.violation_case sat empty and the queue, the evidence viewer and
    the chain all rendered their empty states forever.
    """
    src = (REPO_ROOT / "gateway" / "enforcement.py").read_text()
    assert "SELECT id FROM core.violation_case" not in src, (
        "core.violation_case has no `id` column — lock on case_id")
    assert "SELECT case_id FROM core.violation_case" in src


# ----------------------------------------------------------------- UC3-029
@pytest.mark.parametrize(
    "frm,to",
    [
        ("DETECTED", "REVIEWED"),
        ("REVIEWED", "CONFIRMED"),
        ("CONFIRMED", "CHALLAN_ISSUED"),
        ("CHALLAN_ISSUED", "PAID"),
        ("PAID", "CLOSED"),
        ("CHALLAN_ISSUED", "DISPUTED"),
        ("DISPUTED", "CHALLAN_ISSUED"),
    ],
)
def test_legal_transitions_are_permitted(frm, to):
    assert enforcement.can_transition(frm, to) is True


@pytest.mark.parametrize(
    "frm,to",
    [
        ("DETECTED", "PAID"),          # the ticket's named illegal jump
        ("DETECTED", "CHALLAN_ISSUED"),
        ("REVIEWED", "PAID"),
        ("CLOSED", "PAID"),
        ("PAID", "DETECTED"),
        ("CONFIRMED", "PAID"),
    ],
)
def test_illegal_transitions_are_rejected(frm, to):
    """'Illegal jumps (e.g. DETECTED straight to PAID) must be rejected'."""
    assert enforcement.can_transition(frm, to) is False


def test_closed_is_terminal():
    assert all(not enforcement.can_transition("CLOSED", s)
               for s in enforcement.CASE_STATES)


def test_lifecycle_states_match_the_ticket_verbatim():
    assert list(enforcement.CASE_STATES) == [
        "DETECTED", "REVIEWED", "CONFIRMED", "CHALLAN_ISSUED", "PAID", "CLOSED"]


def test_chain_hash_is_sha256_of_prev_hash_plus_body():
    """The formula the ticket names: hash = sha256(prev_hash + entry body)."""
    a = enforcement.chain_hash(None, event="OPEN", from_status=None,
                               to_status="DETECTED", actor="t", detail={}, at="2026-01-01")
    b = enforcement.chain_hash(a, event="ADVANCE", from_status="DETECTED",
                               to_status="REVIEWED", actor="t", detail={}, at="2026-01-02")
    assert len(a) == 64 and len(b) == 64
    assert a != b
    # Deterministic: the same inputs must reproduce the same link, or the chain
    # could never be re-verified.
    assert b == enforcement.chain_hash(a, event="ADVANCE", from_status="DETECTED",
                                       to_status="REVIEWED", actor="t", detail={},
                                       at="2026-01-02")


def test_changing_any_field_breaks_the_link():
    """Editing a historic entry must visibly break the chain."""
    base = dict(event="ADVANCE", from_status="DETECTED", to_status="REVIEWED",
                actor="operator", detail={"note": "ok"}, at="2026-01-02")
    original = enforcement.chain_hash("prev", **base)
    for field, changed in (
        ("event", "TAMPER"),
        ("to_status", "PAID"),
        ("actor", "someone-else"),
        ("detail", {"note": "edited"}),
        ("at", "2026-01-03"),
    ):
        assert enforcement.chain_hash("prev", **{**base, field: changed}) != original, field
    # …and so must re-parenting the entry onto a different predecessor.
    assert enforcement.chain_hash("other-prev", **base) != original
