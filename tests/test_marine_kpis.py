"""Phase 4 — operational KPIs derived from the projection and nothing else.

The guard these tests exist for: every counter must be a TALLY of a CallProjection field,
never a re-derivation. A KPI that computes its own status would be a second lifecycle
engine wearing a different name.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "services/marine/state_service.py").read_text(encoding="utf-8")


def _kpis_body() -> str:
    """The kpis() method source, comments and docstring stripped.

    Stripped because these assertions scan for SQL and rule-shaped code, and prose that
    DESCRIBES a rule would otherwise read as one.
    """
    m = re.search(r"\n    async def kpis\(self\).*?(?=\n    # ---|\n    async def )", SRC, re.S)
    assert m, "kpis() not found"
    body = m.group(0)
    body = re.sub(r'""".*?"""', "", body, flags=re.S)
    return "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))


class TestNoDuplicatedLifecycle:
    def test_kpis_does_not_call_the_engine_directly(self):
        assert "derive_state" not in _kpis_body()

    def test_kpis_reads_the_projection(self):
        assert "self._projection.by_call_ids" in _kpis_body()

    def test_kpis_writes_no_lifecycle_sql(self):
        """Roster aggregates are allowed; a query against the call or event tables is not
        — that would be deriving lifecycle instead of tallying it."""
        body = _kpis_body()
        for banned in ("core.vessel_call_event", "FROM core.vessel_call c"):
            assert banned not in body, banned

    def test_the_stats_endpoint_is_untouched(self):
        """Backward compatibility: /calls/stats keeps its own factual aggregates."""
        repo = (REPO / "services/marine/repository.py").read_text(encoding="utf-8")
        assert "avg_turnaround_hours" in repo
        assert "async def stats" in repo


class TestShape:
    def _model(self, name):
        import gateway.routers.marine_state as m
        return getattr(m, name)

    def test_every_requested_pilot_kpi_is_exposed(self):
        f = self._model("PilotKpiOut").model_fields
        for k in ("busy", "available", "utilisation_pct", "demand", "waiting_assignment"):
            assert k in f, k

    def test_every_requested_craft_kpi_is_exposed(self):
        f = self._model("CraftKpiOut").model_fields
        for k in ("busy", "available", "utilisation_pct", "demand", "waiting_assignment"):
            assert k in f, k

    def test_every_requested_operations_kpi_is_exposed(self):
        f = self._model("OperationsKpiOut").model_fields
        for k in ("marine_support_required", "awaiting_berthing", "at_berth",
                  "under_pilotage", "preparing_departure", "sailing", "completed_today"):
            assert k in f, k

    def test_scope_declares_its_basis(self):
        assert "basis" in self._model("KpiScopeOut").model_fields


class TestReconciliation:
    def _model(self, name):
        import gateway.routers.marine_state as m
        return getattr(m, name)

    def test_marine_support_required_is_the_phased_count(self):
        """It must equal what the Port Craft board LISTS, or two screens show two numbers
        for one idea. The raw Busy tally stays on craft.demand, and the difference is
        published as craft.demand_unphased rather than dropped."""
        body = _kpis_body()
        assert ('"marine_support_required": awaiting_berthing + at_berth '
                "+ preparing_departure") in body.replace("\n", " ").replace("  ", " ") \
            or "awaiting_berthing + at_berth + preparing_departure" in body
        assert "demand_unphased" in body

    def test_the_gap_is_published_not_hidden(self):
        assert "demand_unphased" in self._model("CraftKpiOut").model_fields
