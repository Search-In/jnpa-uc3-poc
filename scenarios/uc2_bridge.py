"""UC-II <-> UC-III cross-twin bridge.

UC-II (the cargo/DPD twin) would, in production, publish a release-spike event to
the Kafka topic ``cargo.dpd_release`` when DPD (Direct Port Delivery) volumes
surge. UC-III treats that as a leading indicator of upstream truck demand.

``translate_release`` turns a release-spike multiplier into an expected upstream
truck demand profile: a baseline of ~240 trucks/h scaled by the multiplier,
released as bursts over a window (default 40 min). For the PoC the listener is
driven inline by TFC-3 (publish -> consume one message -> translate -> instantiate
trucks) so the cross-twin link is demonstrable end-to-end without a separate
long-running UC-II producer; the same ``translate_release`` is what a standalone
``consume(TOPIC_DPD_RELEASE, ...)`` loop would call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import TOPIC_DPD_RELEASE, DpdReleaseEvent

log = get_logger("scenarios.uc2_bridge")

# Cross-twin Kafka topic + typed event live in the shared schemas package (XT-1):
# defined ONCE so UC-II (producer) and UC-III (consumer) agree on the contract.
# Re-exported here for the existing call sites.

# Baseline corridor truck demand (trucks/hour) at 1.0x release.
BASELINE_TRUCKS_PER_H = 240


@dataclass
class DemandProfile:
    """Translated UC-III truck demand from a UC-II DPD release spike."""

    multiplier: float
    trucks_per_h: int
    window_min: int
    total_trucks: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "multiplier": self.multiplier,
            "trucks_per_h": self.trucks_per_h,
            "window_min": self.window_min,
            "total_trucks": self.total_trucks,
        }


def translate_release(event: "Dict[str, Any] | DpdReleaseEvent") -> DemandProfile:
    """Translate a ``cargo.dpd_release`` event into a UC-III demand profile.

    Accepts either the typed cross-twin model (``DpdReleaseEvent``) or the raw
    dict UC-II would put on the wire:
        {"dpd_release_spike": 2.5, "window_min": 40}
    The spec's TFC-3 calls for "bursts of 600 trucks/h released over 40 min";
    that is exactly 2.5x the 240/h baseline, so the defaults reproduce it.
    """
    if isinstance(event, DpdReleaseEvent):
        event = event.model_dump()
    mult = float(event.get("dpd_release_spike", 1.0))
    window_min = int(event.get("window_min", 40))
    trucks_per_h = int(round(BASELINE_TRUCKS_PER_H * mult))
    total = int(round(trucks_per_h * (window_min / 60.0)))
    profile = DemandProfile(
        multiplier=mult, trucks_per_h=trucks_per_h, window_min=window_min, total_trucks=total,
    )
    log.info("dpd_release_translated", **profile.to_dict())
    return profile


__all__ = [
    "TOPIC_DPD_RELEASE",
    "DpdReleaseEvent",
    "BASELINE_TRUCKS_PER_H",
    "DemandProfile",
    "translate_release",
]
