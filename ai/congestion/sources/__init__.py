"""External real-time traffic-speed adapters + a cascading SourceManager.

Each adapter implements ``async get_segment_speed(seg) -> Optional[SpeedReading]``
for one corridor :class:`~congestion.graph.SegmentMeta`. The
:class:`SourceManager` tries them in order (google -> here -> tomtom) with a
1-second timeout each, caches the answer in Redis for 90 s, and — if ALL sources
fail — returns the last cached value marked ``stale=true``. This is the
foundation for Sub-Criterion 3's graceful-degradation fallback.
"""
from .base import SpeedReading, TrafficSource
from .manager import SourceManager

__all__ = ["SpeedReading", "TrafficSource", "SourceManager"]
