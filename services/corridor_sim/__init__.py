"""NH-348 corridor simulation (UC3-005) — generated traffic, always labelled.

No real per-truck GPS exists for the demo window, so corridor traffic at 20k
scale is generated. This package reads the frozen run seeded by
scripts/seed_uc3_005_corridor_simulation.py and never lets it be mistaken for
measured data: simulated/provenance are pinned by CHECK in migration 0135 and
re-asserted on every payload.
"""

from .repository import CorridorSimRepository
from .service import CorridorSimService

__all__ = ["CorridorSimRepository", "CorridorSimService"]
