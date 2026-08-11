"""UC-3 what-if simulation — the JNPA Notice scenarios, answered from real data.

    POST /api/cargo/simulate/{scenario}   run one scenario
    GET  /api/cargo/simulate/scenarios    the catalog (what can be asked, and how)

:class:`SimulationService` is the single entry point, mirroring
:class:`services.cargo.CargoService`: thin over a repository, stateless apart from
the DSN, dependency-injected so tests can pass a fake.

**This layer is read-only.** It answers "what would this cost" from the data
already in the database; it never writes. That is enforced in
:class:`~services.cargo.simulation.repository.SimulationRepository`, which refuses
any statement that is not a SELECT/WITH and only ever opens a non-transactional
connection. Contrast ``scenarios/`` (tfc1/tfc2/tfc3/monsoon_friday), which is a
live-injection demo harness: it closes gates and injects trucks and needs a
``reset()``. The two are different things and must not be confused.

Scenario coverage (JNPA Notice, 05 August 2026) — all six:

    vessel-bunching     I-A   berthing order vs a stated objective, alternatives costed
    berth-cascade       I-B   extended berth window -> 48h queue displacement
    crane-productivity  II-B  gross moves/hour, -25%, turnaround + queue impact
    modal-shift         II-A  20% rail -> road, hourly gate profile before/after
    gate-slotting       III-A arrival pattern, saturated hours, slotting proposal
    driver-shortage     III-B trips/vehicle cut by a third -> throughput + exposure

Scenario I-A was previously left unregistered on the grounds that the Notice
leaves the objective to the bidder, so the backend could not choose it. That
reasoning was right about the objective and wrong about the conclusion: the fix
is to make the objective an explicit **request parameter**, name it in the
response, and score every candidate ordering against it — which is exactly the
like-for-like comparison the Notice asks for ("show what an alternative order
would cost against the same objective"). See :mod:`.vessel_bunching`.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Optional

from jnpa_shared.logging import get_logger

from . import (berth_cascade, crane_productivity, driver_shortage, gate_slotting,
               modal_shift, vessel_bunching)
from .base import (Assumption, QueryTrace, SimulationError, SimulationResult,
                   hours_between, pct)
from .repository import SimulationRepository, SimulationWriteAttempt

log = get_logger("services.cargo.simulation")

#: scenario name -> module exposing ``async run(repo, params) -> SimulationResult``.
#: The in-package equivalent of the ``scenarios/`` REGISTRY, kept import-light.
REGISTRY: dict[str, Any] = {
    vessel_bunching.SCENARIO: vessel_bunching,
    berth_cascade.SCENARIO: berth_cascade,
    crane_productivity.SCENARIO: crane_productivity,
    modal_shift.SCENARIO: modal_shift,
    gate_slotting.SCENARIO: gate_slotting,
    driver_shortage.SCENARIO: driver_shortage,
}

#: Human-facing catalog for GET /api/cargo/simulate/scenarios. Kept beside the
#: registry so a new scenario cannot be added without describing itself.
CATALOG: list[dict] = [
    {
        "scenario": vessel_bunching.SCENARIO,
        "jnpa_reference": "I-A — Vessel Bunching",
        "question": ("A large number of vessels are alongside, unevenly distributed "
                     "between terminals. What berthing order should be run, against "
                     "which stated objective, and what would an alternative order "
                     "cost against that same objective?"),
        "required": ["as_of"],
        "optional": ["terminal", "horizon_hours (default 24)",
                     "objective (waiting_time | moves_handled | line_priority)"],
        "reads": ["core.berthing_record", "core.vessel_call_moves (migration 0129)"],
        "note": ("6 Aug 2026 lies beyond the corpus; the state is carried forward "
                 "from the last measured day and declared as assumption A-07."),
    },
    {
        "scenario": berth_cascade.SCENARIO,
        "jnpa_reference": "I-B — Extended Berth Window",
        "question": ("A vessel's operation overruns by N hours. Which subsequent "
                     "calls at that terminal are displaced, by how long, and what "
                     "is the cumulative delay over the following 48 hours?"),
        "required": ["terminal", "as_of"],
        "optional": ["delay_hours (default 6)", "horizon_hours (default 48)",
                     "vessel_name", "voyage_number", "berthing_record_id"],
        "reads": ["core.berthing_record"],
    },
    {
        "scenario": crane_productivity.SCENARIO,
        "jnpa_reference": "II-B — Equipment Availability",
        "question": ("What is the effective crane productivity (gross moves per "
                     "hour worked) per vessel call, and what does a 25% reduction "
                     "on one call cost in turnaround and berth-queue delay?"),
        "required": ["as_of"],
        "optional": ["terminal", "reduction_pct (default 0.25)",
                     "window_hours (default 48)", "vessel_name", "voyage_number",
                     "berthing_record_id"],
        "reads": ["core.berthing_record", "core.vessel_call_moves (migration 0129)"],
    },
    {
        "scenario": modal_shift.SCENARIO,
        "jnpa_reference": "II-A — Rail to Road Modal Shift",
        "question": ("If 20% of rail-evacuated volume moves to road, does the gate "
                     "absorb it? Hourly gate profile before and after, and the "
                     "first constraint to saturate."),
        "required": ["from_date", "to_date"],
        "optional": ["shift_pct (default 0.20)", "terminal", "gate_id",
                     "sustained_rate"],
        "reads": ["core.perf_daily_traffic", "core.cargo (migration 0128)",
                  "core.eir", "core.gate_event", "core.tas_appointment"],
    },
    {
        "scenario": gate_slotting.SCENARIO,
        "jnpa_reference": "III-A — Gate Approach Congestion",
        "question": ("Characterise the arrival pattern, identify the periods where "
                     "arrivals exceed the rate the gate sustains, and propose a "
                     "slotting arrangement that flattens the peak."),
        "required": ["from_ts", "to_ts"],
        "optional": ["terminal", "gate_id", "sustained_rate"],
        "reads": ["core.eir", "core.gate_event", "core.tas_appointment"],
    },
    {
        "scenario": driver_shortage.SCENARIO,
        "jnpa_reference": "III-B — Driver Shortage",
        "question": ("If each vehicle completes a third fewer trips per day, what "
                     "happens to evacuation throughput, which transporters and "
                     "cargo flows are most exposed, and what is the state on the "
                     "report date?"),
        "required": ["from_date", "to_date"],
        "optional": ["state_date (default to_date + 1)",
                     "reduction_pct (default 0.3333)"],
        "reads": ["core.eir", "core.cargo"],
    },
]


class SimulationService:
    """Orchestrates the what-if scenarios. One structured log line per run, the
    typed error envelope, and nothing else — the arithmetic lives in the scenario
    modules and the SQL in the repository."""

    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[SimulationRepository] = None) -> None:
        self._repo = repository or SimulationRepository(dsn)

    @staticmethod
    def catalog() -> list[dict]:
        return [dict(entry) for entry in CATALOG]

    @staticmethod
    def names() -> list[str]:
        return sorted(REGISTRY)

    async def run(self, scenario: str, params: dict) -> dict:
        """Run one scenario and return its serialised result.

        Raises :class:`SimulationError` for an unknown scenario or a missing
        required parameter (the router maps it to 400). A scenario that runs but
        finds no data returns normally with ``data_available: false`` — "we could
        not answer this from the data" is a legitimate answer, and a different one
        from "you asked wrongly"."""
        t0 = perf_counter()
        module = REGISTRY.get(str(scenario or "").strip().lower())
        if module is None:
            raise SimulationError(
                f"unknown scenario {scenario!r}; available: {', '.join(self.names())}")
        try:
            result = await module.run(self._repo, params)
        except KeyError as exc:  # a scenario indexed a required param that was absent
            raise SimulationError(f"missing required parameter: {exc}") from exc
        log.info("cargo.simulation", module="cargo.simulation", operation=scenario,
                 status="success" if result.data_available else "no_data",
                 assumptions=len(result.assumptions), queries=len(result.queries),
                 latency_ms=round((perf_counter() - t0) * 1000, 1))
        return result.to_dict()


__all__ = [
    "Assumption",
    "CATALOG",
    "QueryTrace",
    "REGISTRY",
    "SimulationError",
    "SimulationRepository",
    "SimulationResult",
    "SimulationService",
    "SimulationWriteAttempt",
    "hours_between",
    "pct",
]
