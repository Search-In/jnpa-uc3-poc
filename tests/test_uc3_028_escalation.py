"""UC3-028 — the N/2N/3N escalation ladder, fan-out and F-08 latency.

The ladder arithmetic and the delivery-state vocabulary are pure, so they are
tested without a database. The F-08 budget is asserted against a MEASURED
elapsed time produced by the service itself, never a hard-coded claim.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from services.enforcement_escalation import (  # noqa: E402
    CHANNELS,
    RUNGS,
    EscalationService,
    due_rungs,
    rung_schedule,
)


# ------------------------------------------------------------------ ladder
def test_schedule_is_exactly_n_2n_3n():
    """UI-114: first alert at N, escalation at 2N, enforcement notice at 3N."""
    s = rung_schedule(5)
    assert [r["due_after_min"] for r in s] == [5, 10, 15]
    s = rung_schedule(12)
    assert [r["due_after_min"] for r in s] == [12, 24, 36]
    assert [r["rung"] for r in s] == [1, 2, 3]


@pytest.mark.parametrize(
    "dwell,expected",
    [
        (0, []),
        (4.9, []),
        (5, [1]),       # exactly N fires the first alert
        (6, [1]),       # the ticket's example: 6 min dwell in NP-02 at N=5
        (9.9, [1]),
        (10, [1, 2]),   # 2N
        (15, [1, 2, 3]),  # 3N
        (120, [1, 2, 3]),  # never climbs past the top rung
    ],
)
def test_due_rungs_at_each_threshold(dwell, expected):
    assert [r["rung"] for r in due_rungs(dwell, 5)] == expected


def test_third_rung_adds_traffic_police():
    """Only the enforcement notice reaches the police — not the earlier alerts."""
    by_rung = {r["rung"]: r["recipients"] for r in RUNGS}
    assert "TRAFFIC_POLICE" not in by_rung[1]
    assert "TRAFFIC_POLICE" not in by_rung[2]
    assert "TRAFFIC_POLICE" in by_rung[3]


def test_all_three_channels_are_configured():
    assert set(CHANNELS) == {"SMS", "EMAIL", "WHATSAPP"}


# ------------------------------------------------------------ fan-out rules
class _FakeRepo:
    """In-memory stand-in. Enforces the UNIQUE(case_id, rung) idempotency the
    real table enforces, so the service's behaviour is tested, not the DB's."""

    def __init__(self, contact=None):
        self.contact = contact
        self.escalations: dict = {}
        self.deliveries: list = []
        self._next = 1

    async def transporter_for_plate(self, plate):
        return self.contact

    async def zone_n_minutes(self, zone_id):
        return None

    async def record_escalation(self, *, case_id, rung, rung_label, n_minutes,
                                due_after_min, zone_id):
        key = (case_id, rung)
        if key in self.escalations:
            return None  # already fired — the ladder is a ledger
        row = {"escalation_id": self._next, "case_id": case_id, "rung": rung,
               "rung_label": rung_label, "n_minutes": n_minutes,
               "due_after_min": due_after_min, "zone_id": zone_id}
        self.escalations[key] = row
        self._next += 1
        return row

    async def record_delivery(self, **kw):
        row = {"delivery_id": len(self.deliveries) + 1, **kw}
        self.deliveries.append(row)
        return row


@pytest.mark.asyncio
async def test_unavailable_is_recorded_when_no_provider_is_configured():
    """A delivery log that cannot say 'we could not send' is a log that lies."""
    repo = _FakeRepo(contact={"company_name": "Transtar", "contact_person": "D P",
                              "email": "ops@transtar.example", "mobile_number": "9999999999"})
    svc = EscalationService(repository=repo)
    out = await svc.evaluate(case_id="c1", plate="MH43CQ2814", dwell_minutes=6, n_minutes=5)

    assert out["rungs_fired"] == [1]
    assert out["deliveries"], "a fired rung must produce delivery records"
    # No SMS/WhatsApp provider exists pre-award, so nothing may claim SENT.
    for d in out["deliveries"]:
        if d["channel"] in ("SMS", "WHATSAPP"):
            assert d["status"] == "UNAVAILABLE", d
            assert d["status"] != "SENT"
        # Every row names where the address came from, so it can be retraced.
        assert d["recipient_source"]


@pytest.mark.asyncio
async def test_recipients_are_resolved_never_invented():
    """A plate with no mapping yields UNAVAILABLE naming the gap, not a placeholder."""
    svc = EscalationService(repository=_FakeRepo(contact=None))
    out = await svc.evaluate(case_id="c2", plate="ZZ00ZZ0000", dwell_minutes=6, n_minutes=5)
    for d in out["deliveries"]:
        assert d["status"] == "UNAVAILABLE"
        if d["recipient_role"] in ("OWNER", "TRANSPORTER"):
            assert d["recipient"] is None, "no address must be invented"


@pytest.mark.asyncio
async def test_a_rung_never_fires_twice():
    """Idempotency: a retried evaluation must not resend the same notice."""
    repo = _FakeRepo(contact={"company_name": "T", "contact_person": "P",
                              "email": "a@b.example", "mobile_number": "9"})
    svc = EscalationService(repository=repo)

    first = await svc.evaluate(case_id="c3", plate="MH43CQ2814", dwell_minutes=6, n_minutes=5)
    again = await svc.evaluate(case_id="c3", plate="MH43CQ2814", dwell_minutes=6, n_minutes=5)

    assert first["rungs_fired"] == [1]
    assert again["rungs_fired"] == []
    assert again["rungs_already_fired"] == [1]
    assert again["deliveries"] == [], "a re-run must send nothing"


@pytest.mark.asyncio
async def test_climbing_fires_only_the_new_rungs():
    repo = _FakeRepo(contact={"company_name": "T", "contact_person": "P",
                              "email": "a@b.example", "mobile_number": "9"})
    svc = EscalationService(repository=repo)
    await svc.evaluate(case_id="c4", plate="P", dwell_minutes=6, n_minutes=5)
    climbed = await svc.evaluate(case_id="c4", plate="P", dwell_minutes=16, n_minutes=5)
    assert climbed["rungs_fired"] == [2, 3]
    assert climbed["rungs_already_fired"] == [1]


@pytest.mark.asyncio
async def test_f08_latency_is_measured_and_within_budget():
    """F-08 budgets the chain at 10 s. The figure is MEASURED, not asserted."""
    repo = _FakeRepo(contact={"company_name": "T", "contact_person": "P",
                              "email": "a@b.example", "mobile_number": "9"})
    svc = EscalationService(repository=repo)
    out = await svc.evaluate(case_id="c5", plate="P", dwell_minutes=16, n_minutes=5)

    assert out["latency_budget_ms"] == 10_000
    assert isinstance(out["elapsed_ms"], float)
    assert out["elapsed_ms"] >= 0
    assert out["within_budget"] is (out["elapsed_ms"] <= 10_000)
    assert out["within_budget"] is True, f"chain took {out['elapsed_ms']} ms"
