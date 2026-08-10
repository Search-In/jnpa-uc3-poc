"""Vehicle -> transporter registry (UC3-004), MIXED provenance by necessity.

Gap G6: the supplied masters contain no vehicle numbers, so only the gate-document
corpus can evidence a mapping. This package reads the registry seeded by
scripts/seed_uc3_004_vehicle_registry.py and keeps the two halves — evidenced and
generated — distinguishable all the way to the UI.
"""

from .repository import VehicleRegistryRepository
from .service import VehicleRegistryService

__all__ = ["VehicleRegistryRepository", "VehicleRegistryService"]
