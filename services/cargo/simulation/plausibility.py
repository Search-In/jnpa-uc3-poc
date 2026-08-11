"""Physical plausibility guards for derived rates.

The problem this solves
-----------------------
Every input to the what-if engine is a real row from a real table, and the
engine's "never fabricate" rule holds throughout. That turns out not to be
enough. A figure derived correctly, by a sound method, from **too few** real rows
— or from a placeholder row somebody inserted to unblock a migration — is still
wrong, and it arrives wearing the same confidence as a good one.

Three live examples from RDS on 11 Aug 2026, all from correct arithmetic:

======================  =============  ==================================
figure                  derived        why it is impossible
======================  =============  ==================================
``teu_per_trip``        **50.0**       250 road TEU / 5 EIR rows. A
                                       container truck carries 1-2 TEU.
``gate_sustained_rate`` **10.0/h**     from a 16-row TAS stub, against an
                                       observed peak of 284/h. The gate
                                       demonstrably does better.
``moves_per_hour``      **10.0**       min = max = mean across every call.
                                       A constant is not a distribution.
======================  =============  ==================================

The third is the giveaway: a real fleet has spread. When min equals max across
a population, the column is a placeholder.

What a guard does
-----------------
It does **not** substitute a better number — that would be fabrication, which is
the thing the engine exists not to do. It:

1. flags the figure as implausible, with the band it violated and why,
2. lets the scenario demote it (prefer an observed alternative) or refuse,
3. makes the conflict visible in the response instead of burying it.

The bands are deliberately wide. They are not accuracy checks; they are
impossibility checks. A figure inside the band is not thereby correct — it is
merely not absurd.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

#: A container truck carries one TEU (20ft) or two (40ft/45ft). Allowing up to 3
#: leaves room for a reported figure that mixes an occasional double-mount, while
#: still catching the 50-per-trip case by a wide margin.
TEU_PER_TRIP = (0.5, 3.0)

#: Gross moves per hour worked, at BERTH (all cranes on the vessel). A single
#: crane runs ~20-35/h; a big call with 4-5 cranes reaches ~165/h, which is the
#: measured maximum in the JNPA corpus. Below 5/h is not a working vessel.
BERTH_MOVES_PER_HOUR = (5.0, 250.0)

#: Trucks per hour through a gate complex. A single lane clears ~15-30/h; JNPA's
#: gates run many lanes and the corpus shows peaks near 300/h.
GATE_TRUCKS_PER_HOUR = (5.0, 600.0)

#: Minimum observations before a per-unit rate derived from them is defensible.
#: Five gate records cannot characterise a port's road movements.
MIN_SAMPLE_FOR_RATE = 30


@dataclass(frozen=True)
class Verdict:
    """The outcome of one check. ``ok`` False means *do not present this as-is*."""
    ok: bool
    field: str
    value: Optional[float]
    reason: str = ""
    #: Set when the figure is unusable because it rests on too few observations,
    #: as distinct from being outside a physical band.
    thin_sample: bool = False

    def __bool__(self) -> bool:
        return self.ok


def in_band(field: str, value: Optional[float],
            band: tuple[float, float]) -> Verdict:
    """Check one figure against a physical band."""
    low, high = band
    if value is None:
        return Verdict(False, field, value, "no value was derived")
    if value < low:
        return Verdict(False, field, value,
                       f"{value:g} is below the physically plausible minimum of "
                       f"{low:g}")
    if value > high:
        return Verdict(False, field, value,
                       f"{value:g} exceeds the physically plausible maximum of "
                       f"{high:g}")
    return Verdict(True, field, value)


def from_sample(field: str, value: Optional[float], sample_size: int,
                band: tuple[float, float],
                minimum: int = MIN_SAMPLE_FOR_RATE) -> Verdict:
    """Check a rate derived from ``sample_size`` observations.

    Sample size is checked first: a figure from four rows is unusable whether or
    not it happens to land inside the band, and saying "too few observations" is
    more useful than "out of range"."""
    if sample_size < minimum:
        return Verdict(
            False, field, value,
            f"derived from only {sample_size} observation(s); at least {minimum} "
            "are needed for a per-unit rate to be defensible",
            thin_sample=True)
    return in_band(field, value, band)


def is_degenerate(values: Sequence[float], *, minimum_population: int = 3
                  ) -> bool:
    """True when a population has no spread — every value identical.

    Across three or more real vessel calls, identical productivity to the decimal
    is not a coincidence; it means the underlying column is a placeholder. Used to
    downgrade a figure that would otherwise look perfectly reasonable."""
    if len(values) < minimum_population:
        return False
    return max(values) == min(values)


def declared_beats_observed(declared: Optional[float],
                            observed_peak: Optional[float]) -> Verdict:
    """Check a *declared* capacity against what was actually observed.

    A declared policy capacity is normally the best source — it is what the port
    committed to provide. But a declaration that the gate sustains 10 trucks an
    hour, in a window where it demonstrably passed 284 in an hour, is not a policy
    figure; it is a stub nobody replaced. Observed throughput is a **floor** on
    capacity: the gate provably did at least that.
    """
    if declared is None or not observed_peak:
        return Verdict(True, "gate_sustained_rate", declared)
    if declared < observed_peak:
        return Verdict(
            False, "gate_sustained_rate", declared,
            f"the declared capacity of {declared:g}/h is below the observed peak "
            f"of {observed_peak:g}/h in the same window. Observed throughput is a "
            "floor on capacity - the gate demonstrably achieved more than the "
            "declaration claims, so the declaration is treated as a stub rather "
            "than as policy")
    return Verdict(True, "gate_sustained_rate", declared)
