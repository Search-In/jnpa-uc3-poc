"""Gate & Lane Board (UC3-021) and CPP metered release (UC3-027).

Two boards that share one input — the COUNTED gate queue — and therefore share a
package. The queue is read from video analytics and never inferred from
throughput (UI-068); lane reassignment raises a task for a human and never
commands gate equipment (UI-103); each terminal's plaza release rate is derived
from that terminal's own queue, so only the congested terminal slows (F-06).
"""

from .repository import GateBoardRepository
from .service import GateBoardService

__all__ = ["GateBoardRepository", "GateBoardService"]
