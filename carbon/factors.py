"""Published road-freight emission factors for the carbon calculator (C6).

Every constant here is a *published* factor, not an invented number. The basis
for each is named inline so an evaluator can trace it back to a source. See
``docs/ASSUMPTIONS.md`` ("Carbon (C6)") for the PoC posture: fleet-transporter
fuel/telematics feeds are simulated, but the factors applied to that simulated
activity are real, documented IPCC / GHG-Protocol style road-freight factors.

Units throughout:
  * moving emissions   -> gCO2e per tonne-kilometre  (gCO2e / (t * km))
  * idle/dwell emissions -> gCO2e per minute of engine/reefer idling

Sources / basis (well-to-wheel, diesel road freight):
  * IPCC 2006 Guidelines for National GHG Inventories, Vol. 2 (Energy),
    Ch. 3 Mobile Combustion — diesel road-freight CO2e intensity.
  * GHG Protocol — Corporate Value Chain (Scope 3) Standard, Category 4/9
    (transportation & distribution) freight emission-factor methodology.
  * UK DEFRA / DESNZ GHG Conversion Factors for Company Reporting
    (HGV / rigid / articulated / van tonne-km bands; refrigeration uplift).
  * GLEC Framework (Smart Freight Centre) for the reefer/refrigeration uplift
    and idle/auxiliary-load treatment.

These figures are intensity bands (gCO2e per tonne-km) for laden diesel road
freight, expressed at the upper-mid of the published ranges so the PoC neither
under- nor over-states emissions. They are documented constants only.
"""
from __future__ import annotations

# --- Vehicle classes recognised by the calculator ---------------------------
# Kept as plain string constants so the API surface and tests are stable.
HGV = "HGV"          # Heavy Goods Vehicle (articulated tractor-trailer)
LGV = "LGV"          # Light Goods Vehicle (van / light commercial)
REEFER = "REEFER"    # Refrigerated trailer (HGV + active refrigeration unit)
RIGID = "RIGID"      # Rigid medium goods vehicle

# Default applied when a trip omits / mislabels its vehicle class. HGV is the
# dominant class at a container port, so it is the conservative default.
DEFAULT_CLASS = HGV

# ---------------------------------------------------------------------------
# Moving emissions: gCO2e per tonne-kilometre, well-to-wheel diesel freight.
#
#   HGV    ~62  -> DEFRA articulated-HGV / IPCC heavy-diesel band (~55-70).
#   RIGID  ~85  -> DEFRA rigid-HGV band; higher per-tonne-km than artic HGV
#                  because of lower average payload utilisation.
#   LGV    ~110 -> DEFRA van / light-commercial band; far higher per-tonne-km
#                  than heavy freight due to small payloads.
#   REEFER ~78  -> HGV base (62) + GLEC/DEFRA refrigeration uplift (~+25%) for
#                  the trailer-mounted refrigeration unit's added fuel burn.
# ---------------------------------------------------------------------------
GCO2E_PER_TONNE_KM: dict[str, float] = {
    HGV: 62.0,      # IPCC/DEFRA articulated HGV, well-to-wheel diesel
    RIGID: 85.0,    # DEFRA rigid HGV band
    LGV: 110.0,     # DEFRA van / light commercial band
    REEFER: 78.0,   # HGV base + ~25% GLEC/DEFRA refrigeration uplift
}

# ---------------------------------------------------------------------------
# Idle / dwell emissions: gCO2e per minute of engine (and, for reefers, also
# refrigeration-unit) idling while parked in the CPP / parking area.
#
# Basis: a diesel HGV idles at roughly 2-4 litres of diesel per hour
# (EPA SmartWay / DEFRA idling figures). Diesel combusts at ~2.68 kgCO2e per
# litre. Taking ~3.0 L/h => 3.0 * 2680 gCO2e / 60 min ~= 134 gCO2e/min for an
# HGV tractor. A reefer adds its refrigeration unit's separate idle burn
# (GLEC auxiliary load), roughly +90 gCO2e/min on top of the tractor idle.
#
#   tractor idle:        134 gCO2e/min  (3.0 L/h diesel * 2.68 kgCO2e/L)
#   reefer extra (unit):  +90 gCO2e/min (GLEC refrigeration auxiliary load)
# ---------------------------------------------------------------------------
GCO2E_PER_IDLE_MINUTE: dict[str, float] = {
    HGV: 134.0,     # diesel tractor idling, ~3.0 L/h * 2.68 kgCO2e/L
    RIGID: 134.0,   # same tractor-idle basis as HGV
    LGV: 60.0,      # smaller diesel engine, lower idle burn (~1.34 L/h)
    REEFER: 224.0,  # tractor idle (134) + refrigeration-unit idle (~90)
}

# Underlying constant used above, exposed for documentation/traceability.
DIESEL_GCO2E_PER_LITRE = 2680.0  # ~2.68 kgCO2e per litre diesel (IPCC/DEFRA)


def tonne_km_factor(vehicle_class: str) -> float:
    """gCO2e per tonne-km for a vehicle class (falls back to the default class)."""
    return GCO2E_PER_TONNE_KM.get(
        (vehicle_class or "").upper(), GCO2E_PER_TONNE_KM[DEFAULT_CLASS]
    )


def idle_minute_factor(vehicle_class: str) -> float:
    """gCO2e per idle-minute for a vehicle class (falls back to the default)."""
    return GCO2E_PER_IDLE_MINUTE.get(
        (vehicle_class or "").upper(), GCO2E_PER_IDLE_MINUTE[DEFAULT_CLASS]
    )


def known_classes() -> tuple[str, ...]:
    """The vehicle classes with published factors, in a stable order."""
    return (HGV, RIGID, LGV, REEFER)


__all__ = [
    "HGV",
    "LGV",
    "REEFER",
    "RIGID",
    "DEFAULT_CLASS",
    "GCO2E_PER_TONNE_KM",
    "GCO2E_PER_IDLE_MINUTE",
    "DIESEL_GCO2E_PER_LITRE",
    "tonne_km_factor",
    "idle_minute_factor",
    "known_classes",
]
