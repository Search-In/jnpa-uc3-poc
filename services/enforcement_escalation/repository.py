"""Escalation + notification persistence (UC3-028).

The only layer that speaks SQL for the ladder, the per-channel delivery log and
the field-verification task. Recipient lookup joins the REAL transporter master
through the UC3-004 vehicle registry; a plate with no mapping returns None so the
caller records an honest gap rather than a placeholder contact.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.enforcement_escalation.repository")


def _norm_plate(plate: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


class EscalationRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def _write(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).begin() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            try:
                return [dict(r) for r in res.mappings().all()]
            except Exception:  # noqa: BLE001 — non-RETURNING statement
                return []

    # ---------------------------------------------------------- recipients
    async def transporter_for_plate(self, plate: str) -> Optional[dict]:
        """The transporter contact for a plate, from REAL master data."""
        rows = await self._rows(
            """
            SELECT t.company_name, t.contact_person, t.email,
                   t.mobile_number, t.mobile, tv.provenance
              FROM core.transporter_vehicle tv
              JOIN core.transporter t ON t.id = tv.transporter_id
             WHERE tv.vehicle_no_norm = :p
             LIMIT 1
            """,
            {"p": _norm_plate(plate)},
        )
        return rows[0] if rows else None

    async def zone_n_minutes(self, zone_id: Optional[str]) -> Optional[int]:
        """The zone's configured first-alert minute (N). None when unconfigured."""
        if not zone_id:
            return None
        try:
            rows = await self._rows(
                "SELECT (escalation->>'warn_min')::int AS n "
                "FROM core.geofence_zone WHERE id = :z", {"z": zone_id})
        except Exception as exc:  # noqa: BLE001 — an absent zone table is not fatal
            log.debug("zone_n_lookup_failed", extra={"zone": zone_id, "error": str(exc)})
            return None
        return int(rows[0]["n"]) if rows and rows[0].get("n") else None

    # ------------------------------------------------------------- ladder
    async def record_escalation(self, *, case_id: str, rung: int, rung_label: str,
                                n_minutes: int, due_after_min: int,
                                zone_id: Optional[str]) -> Optional[dict]:
        """Record a rung. Returns None when it already fired (idempotent)."""
        rows = await self._write(
            """
            INSERT INTO core.violation_escalation
                   (case_id, rung, rung_label, n_minutes, due_after_min, zone_id)
            VALUES (CAST(:c AS uuid), :r, :l, :n, :d, :z)
            ON CONFLICT (case_id, rung) DO NOTHING
            RETURNING escalation_id, case_id::text AS case_id, rung, rung_label,
                      n_minutes, due_after_min, zone_id, fired_at
            """,
            {"c": case_id, "r": rung, "l": rung_label, "n": n_minutes,
             "d": due_after_min, "z": zone_id},
        )
        return rows[0] if rows else None

    async def escalations_for(self, case_id: str) -> list[dict]:
        return await self._rows(
            "SELECT escalation_id, rung, rung_label, n_minutes, due_after_min, "
            "zone_id, fired_at FROM core.violation_escalation "
            "WHERE case_id = CAST(:c AS uuid) ORDER BY rung", {"c": case_id})

    # --------------------------------------------------------- deliveries
    async def record_delivery(self, *, case_id: str, escalation_id: int, rung: int,
                              channel: str, recipient_role: str, recipient: Optional[str],
                              recipient_name: Optional[str], recipient_source: Optional[str],
                              status: str, provider: Optional[str],
                              detail: Optional[str]) -> dict:
        rows = await self._write(
            """
            INSERT INTO core.notification_delivery
                   (case_id, escalation_id, rung, channel, recipient_role, recipient,
                    recipient_name, recipient_source, status, provider, detail)
            VALUES (CAST(:c AS uuid), :e, :r, :ch, :role, :rcpt, :name, :src,
                    :status, :prov, :detail)
            RETURNING delivery_id, case_id::text AS case_id, escalation_id, rung,
                      channel, recipient_role, recipient, recipient_name,
                      recipient_source, status, provider, detail, created_at
            """,
            {"c": case_id, "e": escalation_id, "r": rung, "ch": channel,
             "role": recipient_role, "rcpt": recipient, "name": recipient_name,
             "src": recipient_source, "status": status, "prov": provider,
             "detail": detail},
        )
        return rows[0]

    async def deliveries_for(self, case_id: str) -> list[dict]:
        return await self._rows(
            "SELECT delivery_id, escalation_id, rung, channel, recipient_role, "
            "recipient, recipient_name, recipient_source, status, provider, detail, "
            "created_at FROM core.notification_delivery "
            "WHERE case_id = CAST(:c AS uuid) ORDER BY rung, channel", {"c": case_id})

    # ------------------------------------------------- field verification
    async def create_field_task(self, *, case_id: str, evidence_url: Optional[str],
                                evidence_sha256: Optional[str],
                                zone_id: Optional[str]) -> dict:
        rows = await self._write(
            """
            INSERT INTO core.field_verification_task
                   (case_id, evidence_url, evidence_sha256, zone_id)
            VALUES (CAST(:c AS uuid), :u, :s, :z)
            ON CONFLICT (case_id) DO UPDATE SET evidence_url = EXCLUDED.evidence_url
            RETURNING task_id, case_id::text AS case_id, reason, assigned_to,
                      evidence_url, evidence_sha256, zone_id, status, created_at
            """,
            {"c": case_id, "u": evidence_url, "s": evidence_sha256, "z": zone_id},
        )
        return rows[0]

    async def field_task_for(self, case_id: str) -> Optional[dict]:
        rows = await self._rows(
            "SELECT task_id, case_id::text AS case_id, reason, assigned_to, "
            "evidence_url, evidence_sha256, zone_id, status, resolved_plate, "
            "created_at, resolved_at FROM core.field_verification_task "
            "WHERE case_id = CAST(:c AS uuid)", {"c": case_id})
        return rows[0] if rows else None
