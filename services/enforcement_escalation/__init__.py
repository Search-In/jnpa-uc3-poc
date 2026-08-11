"""UC3-028 escalation ladder + notification fan-out.

UI-114's N/2N/3N ladder with per-channel delivery recorded against REAL
transporter contacts, plus the EC-5 field-verification task for an unreadable
plate. Delivery states distinguish SENT from DELIVERED from UNAVAILABLE, because
a log that cannot say "we could not send" is a log that lies.
"""

from .repository import EscalationRepository
from .service import CHANNELS, RUNGS, EscalationService, due_rungs, rung_schedule

__all__ = ["EscalationRepository", "EscalationService", "RUNGS", "CHANNELS",
           "due_rungs", "rung_schedule"]
