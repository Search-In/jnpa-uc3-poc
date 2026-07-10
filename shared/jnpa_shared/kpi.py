"""Pure, unit-tested KPI engine for JNPA Use Case III.

Every KPI the dashboard shows is computed here and returns the *same* shape
(:class:`KpiResult`) so the UI can render a uniform strip with value / target /
delta / trend. The functions are pure (no IO): callers pass already-fetched
samples — mock fixtures for the instant demo, or live Timescale rows in
production — and get back identical results. The arithmetic is mirrored and
re-tested in ``web/src/kpi`` (Vitest) so the on-screen number can never drift
from this definition.

See docs/KPI_DEFINITIONS.md for the definition of each KPI and its target.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean
from typing import Dict, List, Literal, Sequence

Direction = Literal["lower_is_better", "higher_is_better"]


@dataclass(frozen=True)
class KpiTarget:
    """Acceptance target + current-baseline-ops value a KPI is scored against."""

    label: str
    unit: str
    direction: Direction
    target: float
    baseline: float


# PoC demonstration targets/baselines. Production values come from the JNPA
# baseline study (jnport.gov.in Reports / NLDS) — see docs/ASSUMPTIONS.md. These
# can be overridden per-deployment without touching the arithmetic below.
KPI_TARGETS: Dict[str, KpiTarget] = {
    # Appendix C acceptance KPIs
    "gate_queue_wait": KpiTarget("Gate Queue Wait Time", "min", "lower_is_better", 8.0, 14.5),
    "gate_txn_time": KpiTarget("Avg Gate Transaction Time", "min", "lower_is_better", 3.0, 5.2),
    "trt_empty_ecd": KpiTarget("TRT empty from ECD", "min", "lower_is_better", 45.0, 72.0),
    "tat_inside_port": KpiTarget("TAT inside port", "min", "lower_is_better", 90.0, 135.0),
    # Bid §8.5.4 operational roll-ups
    "queue_length": KpiTarget("Queue Length", "vehicles", "lower_is_better", 25.0, 41.0),
    "avg_dwell": KpiTarget("Avg Vehicle Dwell", "min", "lower_is_better", 12.0, 19.0),
    "gate_throughput": KpiTarget("Gate Throughput", "vph", "higher_is_better", 60.0, 44.0),
}


@dataclass
class KpiResult:
    key: str
    label: str
    unit: str
    value: float
    target: float
    baseline: float
    delta_pct: float
    direction: Direction
    on_target: bool
    trend: List[float] = field(default_factory=list)
    # Provenance so the dashboard can badge a KPI honestly: "live" means the
    # value was aggregated from real event data; "baseline" means no event data
    # was available yet and the configured baseline is shown as a placeholder.
    source: Literal["live", "baseline"] = "live"
    # Number of samples (trips/vehicles) the value was aggregated from.
    n: int = 0

    def to_dict(self) -> dict:
        # camelCase delta to match the web KpiResult contract.
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "value": round(self.value, 2),
            "target": self.target,
            "baseline": self.baseline,
            "deltaPct": round(self.delta_pct, 1),
            "direction": self.direction,
            "onTarget": self.on_target,
            "trend": [round(t, 2) for t in self.trend],
            "source": self.source,
            "n": self.n,
        }


def _on_target(value: float, target: float, direction: Direction) -> bool:
    return value <= target if direction == "lower_is_better" else value >= target


def _delta_pct(value: float, baseline: float, direction: Direction) -> float:
    """% change vs baseline, signed so that improvement is negative for
    lower-is-better metrics and positive for higher-is-better metrics — i.e.
    the sign always reads "did we move the right way".
    """
    if baseline == 0:
        return 0.0
    raw = (value - baseline) / baseline * 100.0
    # For both directions we report the raw signed change vs baseline; the UI
    # colours it using `direction`. A wait-time drop is a negative raw change
    # (improvement); a throughput rise is a positive raw change (improvement).
    return raw


def compute_kpi(key: str, value: float, trend: Sequence[float] | None = None,
                source: Literal["live", "baseline"] = "live", n: int = 0) -> KpiResult:
    """Build a :class:`KpiResult` for ``key`` from an already-aggregated value.

    ``value`` is the current scalar (e.g. mean queue wait in minutes). ``trend``
    is an optional ordered window (oldest -> newest) for the sparkline; if the
    last element is omitted it is set to ``value``. ``source`` records whether the
    value came from real event data (``"live"``) or is the configured baseline
    placeholder (``"baseline"``); ``n`` is the sample count behind it.
    """
    if key not in KPI_TARGETS:
        raise KeyError(f"unknown KPI key: {key!r}")
    t = KPI_TARGETS[key]
    series = list(trend) if trend else [value]
    if not series or series[-1] != value:
        series = series + [value]
    return KpiResult(
        key=key,
        label=t.label,
        unit=t.unit,
        value=float(value),
        target=t.target,
        baseline=t.baseline,
        delta_pct=_delta_pct(value, t.baseline, t.direction),
        direction=t.direction,
        on_target=_on_target(value, t.target, t.direction),
        trend=[float(x) for x in series],
        source=source,
        n=n,
    )


# --- aggregation helpers: raw samples -> scalar value -----------------------
# These keep "how we turn rows into a number" testable and in one place. Each
# returns just the scalar; pass it to compute_kpi() with the KPI key.

def gate_queue_wait_min(queue_dwell_seconds: Sequence[float]) -> float:
    """Mean queue wait (minutes) from per-vehicle in-queue dwell seconds."""
    if not queue_dwell_seconds:
        return 0.0
    return fmean(queue_dwell_seconds) / 60.0


def gate_txn_time_min(txn_seconds: Sequence[float]) -> float:
    """Mean boom-arrival -> boom-clear time (minutes)."""
    if not txn_seconds:
        return 0.0
    return fmean(txn_seconds) / 60.0


def trt_empty_ecd_min(pickup_to_gatein_seconds: Sequence[float]) -> float:
    """Mean empty-container turn-round time from ECD pickup to gate-in (minutes)."""
    if not pickup_to_gatein_seconds:
        return 0.0
    return fmean(pickup_to_gatein_seconds) / 60.0


def tat_inside_port_min(gatein_to_gateout_seconds: Sequence[float]) -> float:
    """Mean turn-around time inside the port (minutes)."""
    if not gatein_to_gateout_seconds:
        return 0.0
    return fmean(gatein_to_gateout_seconds) / 60.0


def queue_length(at_gate_queue_count: int) -> float:
    """Live count of vehicles in AT_GATE_QUEUE."""
    return float(at_gate_queue_count)


def avg_dwell_min(dwell_seconds: Sequence[float]) -> float:
    if not dwell_seconds:
        return 0.0
    return fmean(dwell_seconds) / 60.0


def gate_throughput_vph(cleared_in_window: int, window_minutes: float) -> float:
    """Distinct vehicles cleared per hour, extrapolated from the window."""
    if window_minutes <= 0:
        return 0.0
    return cleared_in_window * (60.0 / window_minutes)


def kpi_strip(values: Dict[str, float],
              trends: Dict[str, Sequence[float]] | None = None) -> List[dict]:
    """Build the full dashboard KPI strip from a {key: value} map.

    Unknown keys are skipped; missing keys are simply absent from the strip, so
    a partially-degraded backend still renders whatever it has.
    """
    trends = trends or {}
    out: List[dict] = []
    for key in KPI_TARGETS:
        if key in values:
            out.append(compute_kpi(key, values[key], trends.get(key)).to_dict())
    return out
