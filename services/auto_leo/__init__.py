"""Auto-LEO four-way join (UC3-040).

Tender UC3-R5: "Vehicle & container identification, e-seal data, Form 13,
weighbridge data, ICEGATE data capturing for Auto-LEO process". This package
joins those four streams per export truck and reports MATCH / MISMATCH / MISSING
per stream, so a blocked Let Export Order names the evidence that blocked it.

The decision rules live in gate-data/leo.py (a pure, deterministic function);
this package only sources the records from RDS and attaches provenance.
"""

from .repository import AutoLeoRepository
from .service import AutoLeoService

__all__ = ["AutoLeoRepository", "AutoLeoService"]
