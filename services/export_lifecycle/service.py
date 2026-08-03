"""Export lifecycle service — the export state machine and its events.

    BOOKED -> FORM13_ISSUED -> GATE_IN -> VGM_CAPTURED -> LEO_GRANTED
           -> LOAD_LISTED -> LOADED

Mirrors ``services.cargo``'s design: forward-only ranked states, no mandatory gate
may be skipped, every applied move emits on the existing lifecycle bus
(``jnpa.uc3.lifecycle`` + the WS hub) and mirrors the state onto ``core.cargo`` so
one container has one lifecycle regardless of which leg it is on.

The VGM step carries the only real business rule on this leg: a SOLAS verified
gross mass more than ``VGM_TOLERANCE_PCT`` away from the declared gross is
flagged, reusing the same 2 % tolerance the UC-III Auto-LEO reconciler already
applies to the weighbridge (gate-data/leo.py), so the two never disagree.
"""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Mapping, Optional

from jnpa_shared.iso6346 import is_valid_container_no
from jnpa_shared.logging import get_logger

from .repository import ExportRepository

log = get_logger("services.export_lifecycle")

# --------------------------------------------------------------------- states
ST_BOOKED = "BOOKED"
ST_FORM13 = "FORM13_ISSUED"
ST_GATE_IN = "GATE_IN"
ST_VGM = "VGM_CAPTURED"
ST_LEO = "LEO_GRANTED"
ST_LOAD_LISTED = "LOAD_LISTED"
ST_LOADED = "LOADED"
ST_CANCELLED = "CANCELLED"

_RANK: dict[str, int] = {
    ST_BOOKED: 0, ST_FORM13: 10, ST_GATE_IN: 20, ST_VGM: 30,
    ST_LEO: 40, ST_LOAD_LISTED: 50, ST_LOADED: 60,
}

# The booking status -> the cargo lifecycle_status it implies (migration 0115).
_CARGO_STATE: dict[str, str] = {
    ST_BOOKED: "EXPORT_BOOKED",
    ST_FORM13: "FORM13_ISSUED",
    ST_GATE_IN: "EXPORT_GATE_IN",
    ST_VGM: "VGM_CAPTURED",
    ST_LEO: "LEO_GRANTED",
    ST_LOAD_LISTED: "LOAD_LISTED",
    ST_LOADED: "VESSEL_LOADED",
}

# Lifecycle-bus event names (stable wire contract for UC-II consumers).
EVENT_BOOKED = "export.booked"
EVENT_FORM13 = "export.form13_issued"
EVENT_GATE_IN = "export.gate_in"
EVENT_VGM = "export.vgm_captured"
EVENT_LEO = "export.leo_granted"
EVENT_LOAD_LISTED = "export.load_listed"
EVENT_LOADED = "export.vessel_loaded"
EVENT_CANCELLED = "export.cancelled"

# SOLAS VGM vs declared-gross tolerance. Same 2 % the Auto-LEO weighbridge
# reconciler uses, so a box cannot pass one check and fail the other.
VGM_TOLERANCE_PCT = 2.0


def predecessors(target: str) -> set[str]:
    """States from which ``target`` is legal: strictly the immediately lower rank.

    The export chain has no optional stages — every step is a document or a
    physical event that genuinely must precede the next one — so unlike the import
    machine this is a simple ordered walk.
    """
    tr = _RANK.get(target)
    if tr is None:
        return set()
    lower = [s for s, r in _RANK.items() if r < tr]
    if not lower:
        return set()
    top = max(_RANK[s] for s in lower)
    return {s for s in lower if _RANK[s] == top}


# ------------------------------------------------------------------ exceptions
class ExportValidationError(Exception):
    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code, self.detail, self.extra = code, detail, extra


class ExportBookingNotFound(Exception):
    def __init__(self, ref: Any) -> None:
        super().__init__(f"export booking {ref} not found")
        self.ref = ref


class ExportTransitionError(Exception):
    def __init__(self, booking_id: int, current: str, target: str) -> None:
        super().__init__(f"booking #{booking_id}: {current} -> {target} is not a legal move")
        self.booking_id, self.current, self.target = booking_id, current, target


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExportLifecycleService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[ExportRepository] = None) -> None:
        self._repo = repository or ExportRepository(dsn)

    @staticmethod
    def _ms(t0: float) -> float:
        return round((perf_counter() - t0) * 1000, 1)

    async def _publish(self, event: str, booking: Mapping[str, Any],
                       detail: Optional[Mapping[str, Any]] = None) -> None:
        """Emit on the shared UC-III lifecycle bus. Best-effort by construction —
        a broker blip must never fail an export step (the DB row is already the
        source of truth by the time this runs)."""
        try:
            from services.lifecycle_bus import publish

            await publish(event, {
                "booking_id": booking.get("id"),
                "booking_no": booking.get("booking_no"),
                "container_number": booking.get("container_number"),
                "status": booking.get("status"),
                "vessel_name": booking.get("vessel_name"),
                "via_no": booking.get("via_no"),
                **dict(detail or {}),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("export.publish_failed", extra={"event": event, "error": str(exc)})

    async def _step(self, booking_id: int, *, new_status: str, event: str,
                    set_fields: Mapping[str, Any],
                    detail: Optional[Mapping[str, Any]] = None,
                    actor: Optional[str] = None,
                    actor_role: Optional[str] = None) -> dict:
        res = await self._repo.advance(
            booking_id, new_status=new_status, event=event, set_fields=set_fields,
            allowed_from=predecessors(new_status), detail=detail,
            actor=actor, actor_role=actor_role)
        if not res["ok"]:
            if res["reason"] == "booking_not_found":
                raise ExportBookingNotFound(booking_id)
            raise ExportTransitionError(booking_id, res.get("from") or "?", new_status)
        booking = res["booking"]
        # Keep the container's single lifecycle in step with the booking.
        if booking.get("container_number"):
            await self._repo.upsert_cargo_for_export(
                booking["container_number"],
                lifecycle_status=_CARGO_STATE[new_status])
        await self._publish(event, booking, detail)
        return booking

    # ------------------------------------------------------------------ reads
    async def get(self, booking_id: int) -> dict:
        row = await self._repo.get(booking_id)
        if row is None:
            raise ExportBookingNotFound(booking_id)
        return row

    async def get_with_events(self, booking_id: int) -> dict:
        row = await self.get(booking_id)
        return {**row, "events": await self._repo.events(booking_id)}

    async def list(self, **kw: Any) -> tuple[list[dict], int]:
        return await self._repo.list(**kw)

    async def for_container(self, container_number: str) -> Optional[dict]:
        return await self._repo.open_for_container(container_number.strip().upper())

    async def summary(self) -> dict:
        return await self._repo.summary()

    # ------------------------------------------------------------- 1. booking
    async def create_booking(self, *, booking_no: str,
                             container_number: Optional[str] = None,
                             shipping_line: Optional[str] = None,
                             vessel_name: Optional[str] = None,
                             voyage_no: Optional[str] = None,
                             via_no: Optional[str] = None,
                             pod: Optional[str] = None,
                             terminal: Optional[str] = None,
                             cfs_code: Optional[str] = None,
                             declared_gross_kg: Optional[float] = None,
                             created_by: Optional[str] = None) -> dict:
        """Step 1 — the liner booking that starts the export leg."""
        t0 = perf_counter()
        booking_no = (booking_no or "").strip()
        if not booking_no:
            raise ExportValidationError("booking_no_required", "booking_no is required")
        cn = (container_number or "").strip().upper() or None
        if cn and not is_valid_container_no(cn):
            raise ExportValidationError(
                "invalid_container_number",
                f"{cn} is not a valid ISO-6346 container number", container_number=cn)
        if await self._repo.by_booking_no(booking_no):
            raise ExportValidationError("booking_already_exists",
                                        f"booking {booking_no} already exists",
                                        booking_no=booking_no)
        if cn:
            open_row = await self._repo.open_for_container(cn)
            if open_row is not None:
                raise ExportValidationError(
                    "container_already_booked",
                    f"container {cn} already has open booking {open_row['booking_no']}",
                    booking_no=open_row["booking_no"], booking_id=open_row["id"])
        row = await self._repo.create({
            "booking_no": booking_no, "container_number": cn,
            "shipping_line": shipping_line, "vessel_name": vessel_name,
            "voyage_no": voyage_no, "via_no": via_no, "pod": pod,
            "terminal": terminal, "cfs_code": cfs_code,
            "declared_gross_kg": declared_gross_kg, "created_by": created_by,
        })
        if cn:
            await self._repo.upsert_cargo_for_export(
                cn, lifecycle_status=_CARGO_STATE[ST_BOOKED])
        await self._publish(EVENT_BOOKED, row)
        log.info("export.booked", booking_no=booking_no, container=cn,
                 latency_ms=self._ms(t0))
        return row

    # ------------------------------------------------------------- 2. Form 13
    async def issue_form13(self, booking_id: int, *, form13_no: str,
                           issued_at: Optional[datetime] = None,
                           actor: Optional[str] = None,
                           actor_role: Optional[str] = None) -> dict:
        """Step 2 — the export gate pass (Form 13 / E-Gate) is issued."""
        if not (form13_no or "").strip():
            raise ExportValidationError("form13_no_required", "form13_no is required")
        return await self._step(
            booking_id, new_status=ST_FORM13, event=EVENT_FORM13,
            set_fields={"form13_no": form13_no.strip(),
                        "form13_issued_at": issued_at or _now()},
            detail={"form13_no": form13_no.strip()},
            actor=actor, actor_role=actor_role)

    # ------------------------------------------------------------- 3. gate-in
    async def gate_in(self, booking_id: int, *, gate_id: Optional[str] = None,
                      truck_no: Optional[str] = None, job_id: Optional[int] = None,
                      occurred_at: Optional[datetime] = None,
                      actor: Optional[str] = None,
                      actor_role: Optional[str] = None) -> dict:
        """Step 3 — the box physically enters the terminal on a truck.

        ``job_id`` links the export move to the UC-III container-job spine, which
        is what makes an export container visible on the same gate/yard timeline
        as an import one.
        """
        return await self._step(
            booking_id, new_status=ST_GATE_IN, event=EVENT_GATE_IN,
            set_fields={"gate_in_at": occurred_at or _now(),
                        "gate_in_gate": gate_id, "truck_no": truck_no,
                        "job_id": job_id},
            detail={"gate_id": gate_id, "truck_no": truck_no, "job_id": job_id},
            actor=actor, actor_role=actor_role)

    # ----------------------------------------------------------------- 4. VGM
    async def capture_vgm(self, booking_id: int, *, vgm_kg: float,
                          method: str = "METHOD_1",
                          declared_gross_kg: Optional[float] = None,
                          captured_at: Optional[datetime] = None,
                          actor: Optional[str] = None,
                          actor_role: Optional[str] = None) -> dict:
        """Step 4 — SOLAS verified gross mass.

        Computes the variance against the declared gross and flags it when it
        exceeds ``VGM_TOLERANCE_PCT``. The step still applies when flagged — the
        finding is reported, never silently corrected, and never silently dropped.
        """
        if vgm_kg is None or float(vgm_kg) <= 0:
            raise ExportValidationError("invalid_vgm", "vgm_kg must be greater than zero")
        if method not in ("METHOD_1", "METHOD_2"):
            raise ExportValidationError(
                "invalid_vgm_method", "method must be METHOD_1 (weighing) or METHOD_2 (calculation)")
        current = await self.get(booking_id)
        declared = declared_gross_kg if declared_gross_kg is not None else current.get("declared_gross_kg")
        variance: Optional[float] = None
        flagged = False
        if declared:
            variance = round(abs(float(vgm_kg) - float(declared)) / float(declared) * 100.0, 3)
            flagged = variance > VGM_TOLERANCE_PCT
        fields: dict[str, Any] = {
            "vgm_kg": float(vgm_kg), "vgm_method": method,
            "vgm_captured_at": captured_at or _now(),
            "vgm_variance_pct": variance,
        }
        if declared_gross_kg is not None:
            fields["declared_gross_kg"] = float(declared_gross_kg)
        row = await self._step(
            booking_id, new_status=ST_VGM, event=EVENT_VGM, set_fields=fields,
            detail={"vgm_kg": float(vgm_kg), "method": method,
                    "declared_gross_kg": declared, "variance_pct": variance,
                    "tolerance_pct": VGM_TOLERANCE_PCT,
                    "flag": "VGM_MISMATCH" if flagged else None},
            actor=actor, actor_role=actor_role)
        return {**row, "vgm_variance_pct": variance,
                "vgm_flag": "VGM_MISMATCH" if flagged else None,
                "vgm_tolerance_pct": VGM_TOLERANCE_PCT}

    # ----------------------------------------------------------------- 5. LEO
    async def grant_leo(self, booking_id: int, *, leo_no: str,
                        shipping_bill_no: Optional[str] = None,
                        granted_at: Optional[datetime] = None,
                        actor: Optional[str] = None,
                        actor_role: Optional[str] = None) -> dict:
        """Step 5 — Customs Let Export Order against the shipping bill."""
        if not (leo_no or "").strip():
            raise ExportValidationError("leo_no_required", "leo_no is required")
        fields: dict[str, Any] = {"leo_no": leo_no.strip(),
                                  "leo_granted_at": granted_at or _now()}
        if shipping_bill_no:
            fields["shipping_bill_no"] = shipping_bill_no.strip()
        return await self._step(
            booking_id, new_status=ST_LEO, event=EVENT_LEO, set_fields=fields,
            detail={"leo_no": leo_no.strip(), "shipping_bill_no": shipping_bill_no},
            actor=actor, actor_role=actor_role)

    # -------------------------------------------------------------- 6. COPRAR
    async def add_to_load_list(self, booking_id: int, *, coprar_ref: str,
                               listed_at: Optional[datetime] = None,
                               actor: Optional[str] = None,
                               actor_role: Optional[str] = None) -> dict:
        """Step 6 — the box appears on the carrier's COPRAR load list."""
        if not (coprar_ref or "").strip():
            raise ExportValidationError("coprar_ref_required", "coprar_ref is required")
        return await self._step(
            booking_id, new_status=ST_LOAD_LISTED, event=EVENT_LOAD_LISTED,
            set_fields={"coprar_ref": coprar_ref.strip(),
                        "load_listed_at": listed_at or _now()},
            detail={"coprar_ref": coprar_ref.strip()},
            actor=actor, actor_role=actor_role)

    # -------------------------------------------------------- 7. vessel load
    async def confirm_loaded(self, booking_id: int, *,
                             stowage_position: Optional[str] = None,
                             loaded_at: Optional[datetime] = None,
                             actor: Optional[str] = None,
                             actor_role: Optional[str] = None) -> dict:
        """Step 7 — COARRI load confirmation: the container is on the ship."""
        return await self._step(
            booking_id, new_status=ST_LOADED, event=EVENT_LOADED,
            set_fields={"loaded_at": loaded_at or _now(),
                        "stowage_position": stowage_position},
            detail={"stowage_position": stowage_position},
            actor=actor, actor_role=actor_role)

    # ------------------------------------------------------------------ cancel
    async def cancel(self, booking_id: int, *, reason: Optional[str] = None,
                     actor: Optional[str] = None,
                     actor_role: Optional[str] = None) -> dict:
        """Cancel an open booking. Legal from any non-terminal state — a real
        booking can fall through at any point before it is on the vessel."""
        res = await self._repo.advance(
            booking_id, new_status=ST_CANCELLED, event=EVENT_CANCELLED,
            set_fields={}, allowed_from=set(_RANK) - {ST_LOADED},
            detail={"reason": reason}, actor=actor, actor_role=actor_role)
        if not res["ok"]:
            if res["reason"] == "booking_not_found":
                raise ExportBookingNotFound(booking_id)
            raise ExportTransitionError(booking_id, res.get("from") or "?", ST_CANCELLED)
        await self._publish(EVENT_CANCELLED, res["booking"], {"reason": reason})
        return res["booking"]


__all__ = [
    "ExportLifecycleService", "ExportValidationError", "ExportBookingNotFound",
    "ExportTransitionError", "predecessors", "VGM_TOLERANCE_PCT",
]
