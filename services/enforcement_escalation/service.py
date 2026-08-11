"""UC3-028 — the N/2N/3N escalation ladder and notification fan-out.

UI-114: "first alert at N minutes, escalation at 2N, enforcement notification at
3N; recipients (owner, transporter, traffic police) come from the vehicle-owner
data over SMS/Email/WhatsApp, and each delivery channel is recorded."

Three design decisions worth stating, because each is the honest option rather
than the convenient one:

1. **A rung fires at most once.** The ladder is a ledger keyed UNIQUE on
   (case_id, rung), so re-running the evaluator — a retry, a restart, an
   over-eager scheduler — cannot send a transporter the same notice twice. The
   idempotency lives in the database, not in the caller's discipline.

2. **UNAVAILABLE is a real outcome.** No SMS/WhatsApp provider is configured
   before award. Recording UNAVAILABLE with the reason is truthful; recording
   SENT would be a fabricated delivery, and a delivery log that lies is worse
   than no delivery log. Email goes through the project's existing mailer when
   it is configured, and reports what actually happened.

3. **Recipients are resolved, never invented.** Addresses come from the REAL
   transporter master via the vehicle→transporter registry. A vehicle with no
   mapping, or a transporter with no mobile number, produces an UNAVAILABLE row
   naming the gap — not a placeholder address that would look like a delivery.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

from .repository import EscalationRepository

log = get_logger("services.enforcement_escalation.service")

#: The ladder. Multipliers of N, per UI-114.
RUNGS: List[Dict[str, Any]] = [
    {"rung": 1, "multiplier": 1, "label": "FIRST_ALERT",
     "recipients": ["OWNER", "TRANSPORTER"]},
    {"rung": 2, "multiplier": 2, "label": "ESCALATION",
     "recipients": ["OWNER", "TRANSPORTER"]},
    {"rung": 3, "multiplier": 3, "label": "ENFORCEMENT_NOTIFICATION",
     "recipients": ["OWNER", "TRANSPORTER", "TRAFFIC_POLICE"]},
]

#: Default N when the zone carries no configured warn_min. Zone config wins.
DEFAULT_N_MINUTES = 5

CHANNELS = ("SMS", "EMAIL", "WHATSAPP")

#: Where the traffic-police notice goes. A role address, not a person, because
#: the recipient is an office and naming an individual would be inventing one.
TRAFFIC_POLICE_DESK = "control.room@jnpa.example"


def rung_schedule(n_minutes: int) -> List[Dict[str, Any]]:
    """The ladder in minutes for a given N — N, 2N, 3N."""
    return [
        {**r, "due_after_min": n_minutes * r["multiplier"], "n_minutes": n_minutes}
        for r in RUNGS
    ]


def due_rungs(dwell_minutes: float, n_minutes: int) -> List[Dict[str, Any]]:
    """Which rungs a case of this dwell has earned. Pure — no I/O, no clock."""
    return [r for r in rung_schedule(n_minutes) if dwell_minutes >= r["due_after_min"]]


class EscalationService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[EscalationRepository] = None,
                 mailer: Any = None) -> None:
        self._repo = repository or EscalationRepository(dsn=dsn)
        self._mailer = mailer

    # ------------------------------------------------------------ recipients
    async def resolve_recipients(self, plate: Optional[str]) -> List[Dict[str, Any]]:
        """Owner/transporter contacts for a plate, from the REAL master data.

        Returns a list even when nothing resolves: each entry says which role it
        represents and whether an address was found, so the fan-out can record an
        honest UNAVAILABLE instead of skipping the recipient silently.
        """
        out: List[Dict[str, Any]] = []
        contact = await self._repo.transporter_for_plate(plate) if plate else None

        if contact:
            out.append({
                "role": "TRANSPORTER",
                "name": contact.get("company_name"),
                "email": contact.get("email"),
                "mobile": contact.get("mobile_number") or contact.get("mobile"),
                "source": "core.transporter via core.transporter_vehicle",
            })
            # The registered contact person stands in for the vehicle owner: the
            # corpus has no separate owner master (gap G6), so claiming a distinct
            # owner record would be inventing one.
            out.append({
                "role": "OWNER",
                "name": contact.get("contact_person"),
                "email": contact.get("email"),
                "mobile": contact.get("mobile_number") or contact.get("mobile"),
                "source": "core.transporter.contact_person (no separate owner master — gap G6)",
            })
        else:
            out.append({
                "role": "TRANSPORTER", "name": None, "email": None, "mobile": None,
                "source": f"no transporter mapping for plate {plate or '(none)'}",
            })
            out.append({
                "role": "OWNER", "name": None, "email": None, "mobile": None,
                "source": f"no owner contact for plate {plate or '(none)'}",
            })

        out.append({
            "role": "TRAFFIC_POLICE", "name": "JNPA traffic control room",
            "email": TRAFFIC_POLICE_DESK, "mobile": None,
            "source": "configured enforcement desk",
        })
        return out

    # --------------------------------------------------------------- fan-out
    async def _deliver(self, *, case_id: str, escalation_id: int, rung: int,
                       recipient: Dict[str, Any], channel: str) -> Dict[str, Any]:
        """One channel to one recipient. Records what ACTUALLY happened."""
        role = recipient["role"]
        address = recipient.get("email") if channel == "EMAIL" else recipient.get("mobile")

        status, provider, detail = "UNAVAILABLE", None, None
        if not address:
            detail = f"no {channel.lower()} address for {role}: {recipient.get('source')}"
        elif channel == "EMAIL":
            sent = False
            if self._mailer is not None:
                try:
                    sent = bool(await self._mailer(address, case_id, rung))
                except Exception as exc:  # noqa: BLE001 — a provider error is data
                    status, detail = "FAILED", f"mailer error: {exc}"
            if sent:
                status, provider, detail = "SENT", "smtp", "handed to the configured mailer"
            elif status != "FAILED":
                detail = "no SMTP mailer configured — nothing was sent"
        else:
            # SMS / WhatsApp have no provider before award. Saying so is the
            # honest record; SENT would assert a delivery that did not happen.
            detail = (f"no {channel} provider configured (post-award integration); "
                      f"recipient {address} resolved but not contacted")

        row = await self._repo.record_delivery(
            case_id=case_id, escalation_id=escalation_id, rung=rung, channel=channel,
            recipient_role=role, recipient=address, recipient_name=recipient.get("name"),
            recipient_source=recipient.get("source"), status=status,
            provider=provider, detail=detail,
        )
        return row

    async def fire_rung(self, *, case_id: str, rung_cfg: Dict[str, Any],
                        plate: Optional[str], zone_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Fire one rung: record it, then fan out to every channel.

        Returns None when the rung already fired — the UNIQUE key makes that a
        no-op rather than a duplicate notice.
        """
        esc = await self._repo.record_escalation(
            case_id=case_id, rung=rung_cfg["rung"], rung_label=rung_cfg["label"],
            n_minutes=rung_cfg["n_minutes"], due_after_min=rung_cfg["due_after_min"],
            zone_id=zone_id,
        )
        if esc is None:
            return None

        recipients = await self.resolve_recipients(plate)
        wanted = set(rung_cfg["recipients"])
        deliveries: List[Dict[str, Any]] = []
        for r in recipients:
            if r["role"] not in wanted:
                continue
            for ch in CHANNELS:
                # The police desk is an email desk; SMS/WhatsApp to it would be
                # a fabricated channel.
                if r["role"] == "TRAFFIC_POLICE" and ch != "EMAIL":
                    continue
                deliveries.append(await self._deliver(
                    case_id=case_id, escalation_id=int(esc["escalation_id"]),
                    rung=rung_cfg["rung"], recipient=r, channel=ch))

        log.info("escalation.fired", extra={"case_id": case_id, "rung": rung_cfg["rung"],
                                            "deliveries": len(deliveries)})
        return {"escalation": esc, "deliveries": deliveries}

    async def evaluate(self, *, case_id: str, plate: Optional[str],
                       dwell_minutes: float, n_minutes: Optional[int] = None,
                       zone_id: Optional[str] = None) -> Dict[str, Any]:
        """Fire every rung this case has earned. Idempotent, and timed.

        ``elapsed_ms`` is measured, not asserted: F-08 budgets the
        detection→evidence→workflow→notice chain at 10 seconds, and a latency
        claim with no measurement behind it is not evidence.
        """
        t0 = perf_counter()
        n = int(n_minutes or await self._repo.zone_n_minutes(zone_id) or DEFAULT_N_MINUTES)
        earned = due_rungs(dwell_minutes, n)

        fired: List[Dict[str, Any]] = []
        skipped: List[int] = []
        for cfg in earned:
            out = await self.fire_rung(case_id=case_id, rung_cfg=cfg,
                                       plate=plate, zone_id=zone_id)
            if out is None:
                skipped.append(cfg["rung"])
            else:
                fired.append(out)

        elapsed_ms = round((perf_counter() - t0) * 1000, 1)
        return {
            "case_id": case_id,
            "n_minutes": n,
            "dwell_minutes": dwell_minutes,
            "schedule": [{"rung": r["rung"], "label": r["label"],
                          "due_after_min": r["due_after_min"]} for r in rung_schedule(n)],
            "rungs_due": [r["rung"] for r in earned],
            "rungs_fired": [f["escalation"]["rung"] for f in fired],
            "rungs_already_fired": skipped,
            "deliveries": [d for f in fired for d in f["deliveries"]],
            "elapsed_ms": elapsed_ms,
            "latency_budget_ms": 10_000,
            "within_budget": elapsed_ms <= 10_000,
        }

    # ------------------------------------------------------------- read side
    async def case_notifications(self, case_id: str) -> Dict[str, Any]:
        escalations = await self._repo.escalations_for(case_id)
        deliveries = await self._repo.deliveries_for(case_id)
        by_status: Dict[str, int] = {}
        for d in deliveries:
            by_status[d["status"]] = by_status.get(d["status"], 0) + 1
        return {
            "case_id": case_id,
            "escalations": escalations,
            "deliveries": deliveries,
            "by_status": by_status,
            "channels": list(CHANNELS),
            "ladder": [{"rung": r["rung"], "label": r["label"],
                        "multiplier": f"{r['multiplier']}N"} for r in RUNGS],
        }

    # --------------------------------------------------- EC-5 field verification
    async def raise_field_verification(self, *, case_id: str, evidence_url: Optional[str],
                                       evidence_sha256: Optional[str],
                                       zone_id: Optional[str]) -> Dict[str, Any]:
        """An unreadable plate has no owner to notify, so a marshal gets the job.

        Guessing the plate would notify the wrong owner, which is worse than a
        slower manual step.
        """
        return await self._repo.create_field_task(
            case_id=case_id, evidence_url=evidence_url,
            evidence_sha256=evidence_sha256, zone_id=zone_id)
