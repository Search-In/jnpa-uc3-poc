"""What-if simulation framework — the response contract every scenario shares.

The JNPA What-If Notice (05 Aug 2026 §1) asks each answer to carry four things:

  a. the METHOD, stated plainly enough to be reproduced,
  b. the RESULT, with the figures that support it,
  c. every ASSUMPTION, stated explicitly and *separately from the result*,
  d. the QUERIES used to obtain the underlying data, so the working can be traced.

:class:`SimulationResult` is that contract in code, so a scenario cannot forget a
part of it. The Notice is explicit that "an assumption declared openly will be
treated more favourably than a figure presented without one" — which is why
:class:`Assumption` is a first-class object threaded through every calculation
rather than a comment in a docstring.

Design rules for everything in this package:

* **Read-only.** A simulation answers "what would this cost"; it never mutates
  operational state. ``scenarios/`` (the TFC demo harness) does the opposite — it
  injects trucks and closes gates and needs a ``reset()``. The two must not be
  confused. :class:`~services.cargo.simulation.repository.SimulationRepository`
  enforces this at the SQL layer, not by convention.
* **Never fabricate.** When the data needed for a figure is absent, the scenario
  reports ``data_available: false`` and says which table was empty. It does not
  substitute a plausible number.
* **Deterministic.** Same inputs + same database state => byte-identical output.
  No randomness, no wall-clock reads inside the calculation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

# Assumption provenance. DERIVED and ASSUMED are the two that matter to an
# evaluator: DERIVED means "computed from real rows in this database, by the
# stated rule"; ASSUMED means "the data does not carry this, here is the value we
# used and why" — the case the Notice asks to be declared.
SOURCE_MEASURED = "MEASURED"   # read straight from a JNPA-sourced column
SOURCE_DERIVED = "DERIVED"     # computed from JNPA-sourced rows by a stated rule
SOURCE_ASSUMED = "ASSUMED"     # not in the data at all — a declared input
SOURCE_PARAMETER = "PARAMETER"  # supplied by the caller in the request body


@dataclass(frozen=True)
class Assumption:
    """One declared assumption. Serialised into every simulation response.

    ``field``  what the assumption is about (a column, a rate, a policy)
    ``value``  the value actually used in the arithmetic
    ``reason`` why this value and not another — the sentence an evaluator reads
    ``source`` one of MEASURED / DERIVED / ASSUMED / PARAMETER
    """
    field: str
    value: Any
    reason: str
    source: str = SOURCE_ASSUMED

    def to_dict(self) -> dict:
        return {"field": self.field, "value": _jsonable(self.value),
                "reason": self.reason, "source": self.source}


@dataclass(frozen=True)
class QueryTrace:
    """One query the answer rests on — Notice §1.d ("so the working can be
    traced"). Carries the SQL and its bound parameters verbatim, so a reviewer can
    re-run it, plus the API route that issued it."""
    purpose: str
    sql: str
    params: Mapping[str, Any] = field(default_factory=dict)
    api: Optional[str] = None
    row_count: Optional[int] = None
    #: Set when the query FAILED rather than returned nothing. The distinction is
    #: not cosmetic: "the table is empty" and "the query errored" look identical
    #: downstream (both yield zero rows), and reporting a failure as an empty
    #: result would put a confidently wrong "no data" in front of an evaluator.
    error: Optional[str] = None

    def to_dict(self) -> dict:
        out = {"purpose": self.purpose, "sql": _squash(self.sql),
               "params": {k: _jsonable(v) for k, v in dict(self.params).items()}}
        if self.api:
            out["api"] = self.api
        if self.row_count is not None:
            out["row_count"] = self.row_count
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class SimulationResult:
    """The full what-if answer: method + result + assumptions + queries."""
    scenario: str
    method: str
    result: dict = field(default_factory=dict)
    figures: dict = field(default_factory=dict)
    assumptions: list[Assumption] = field(default_factory=list)
    queries: list[QueryTrace] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    #: False when a required input table was empty/absent. The result then carries
    #: whatever partial figures were computable and `notes` says what was missing —
    #: an honest empty answer, never an invented one.
    data_available: bool = True
    notes: list[str] = field(default_factory=list)

    def assume(self, field_: str, value: Any, reason: str,
               source: str = SOURCE_ASSUMED) -> "SimulationResult":
        self.assumptions.append(Assumption(field_, value, reason, source))
        return self

    def trace(self, trace: Optional[QueryTrace]) -> "SimulationResult":
        if trace is not None:
            self.queries.append(trace)
        return self

    def trace_all(self, traces: Sequence[QueryTrace]) -> "SimulationResult":
        for t in traces:
            self.trace(t)
        return self

    def recommend(self, action: str, reason: str, **detail: Any) -> "SimulationResult":
        self.recommendations.append({"action": action, "reason": reason,
                                     **{k: _jsonable(v) for k, v in detail.items()}})
        return self

    def note(self, message: str, *, blocks_answer: bool = False) -> "SimulationResult":
        self.notes.append(message)
        if blocks_answer:
            self.data_available = False
        return self

    @property
    def failed_queries(self) -> list[QueryTrace]:
        """Traces whose query ERRORED rather than returned nothing."""
        return [q for q in self.queries if q.error]

    def audit_query_failures(self) -> "SimulationResult":
        """Promote any query failure into a visible note and clear
        ``data_available``.

        Called once at serialisation. Without it a broken query is
        indistinguishable from an empty table — both produce zero rows — and the
        scenario would report "no data in this window" when the truth is "the
        query did not run". That is a confidently wrong answer, which is worse
        than no answer at all."""
        for q in self.failed_queries:
            msg = (f"QUERY FAILED ({q.purpose}): {q.error}. This is a failure, not "
                   "an empty result — do not read the figures below as 'no data "
                   "in this window'.")
            if msg not in self.notes:
                self.notes.append(msg)
            self.data_available = False
        return self

    def to_dict(self) -> dict:
        # A query failure must never reach a reader as a quiet zero.
        self.audit_query_failures()
        return {
            "scenario": self.scenario,
            "method": self.method,
            "result": _jsonable(self.result),
            "figures": _jsonable(self.figures),
            "assumptions": [a.to_dict() for a in self.assumptions],
            "queries": [q.to_dict() for q in self.queries],
            "recommendations": self.recommendations,
            "data_available": self.data_available,
            "notes": self.notes,
        }


class SimulationError(ValueError):
    """Raised for an un-runnable request (unknown scenario, bad window). The
    router maps it to 400 — distinct from "ran fine, but the data was empty",
    which is a 200 with ``data_available: false``."""


# --------------------------------------------------------------------- helpers
def _squash(sql: str) -> str:
    """Collapse a multi-line SQL literal to one readable line for the response."""
    return " ".join(str(sql).split())


def _jsonable(value: Any) -> Any:
    """Recursively coerce datetimes/dates/Decimals to JSON-safe primitives.

    The routers return plain dicts (no response_model on the simulate endpoints —
    each scenario has its own result shape), so the coercion happens here rather
    than relying on a Pydantic encoder."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    # Decimal and friends -> float, so the payload is numeric rather than a string.
    if value.__class__.__name__ == "Decimal":
        return float(value)
    return value


def hours_between(start: Any, end: Any) -> Optional[float]:
    """Elapsed hours between two timestamps, or None when either is missing or
    the pair is inverted. Returning None (rather than 0) is deliberate: a call
    with no operation window has an UNKNOWN duration, not a zero one, and a zero
    would silently divide into an infinite productivity."""
    if start is None or end is None:
        return None
    try:
        delta = (end - start).total_seconds() / 3600.0
    except (TypeError, AttributeError):
        return None
    return round(delta, 4) if delta > 0 else None


def pct(numerator: float, denominator: float, *, digits: int = 1) -> float:
    """Percentage, 0.0 when the denominator is zero (never a ZeroDivisionError)."""
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, digits)
