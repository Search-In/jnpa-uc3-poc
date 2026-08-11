"""Universal trip resolver (UC3-024) + per-visit checkpoint timeline (UC3-025).

A plate, a container, an e-seal and a Form 13 number all resolve to the same
trip, because all four are printed on the one gate document that IS the trip
record. Every checkpoint on that trip's timeline carries an evidence label, so a
step the corpus cannot source is disclosed rather than filled in.
"""

from .repository import TripSearchRepository
from .service import TripSearchService, build_timeline

__all__ = ["TripSearchRepository", "TripSearchService", "build_timeline"]
