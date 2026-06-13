"""Shared Postgres helpers for the Vahan services (simulator + live adapter).

Two concerns live here so the sim and live services stay DRY:

* `register_service()` — upsert the caller's row into `jnpa.services` on
  startup so the fallback orchestrator (Prompt 4) can discover sim vs. live.
* `upsert_vehicle_master()` — write back every successful RC lookup into
  `jnpa.vehicle_master` with `provisional=false` / `provisional_until=null`.
  This is the "verified at the gate" row the dashboard reads.

Both run idempotent `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` guards first
so the services also work against a Postgres volume created before this PoC
stage added the columns (init.sql only runs on a fresh volume).
"""
from __future__ import annotations

import json
from typing import Optional

from .db import execute, fetch_one
from .schemas import ServiceRegistration, VahanRecord

# Idempotent schema guards (safe on both fresh + pre-existing volumes).
_ENSURE_SERVICES = """
CREATE SCHEMA IF NOT EXISTS jnpa;
CREATE TABLE IF NOT EXISTS jnpa.services (
    name          text NOT NULL,
    kind          text NOT NULL,
    base_url      text NOT NULL,
    healthy       boolean DEFAULT true,
    enabled       boolean DEFAULT true,
    registered_at timestamptz DEFAULT now(),
    meta          jsonb DEFAULT '{}'::jsonb,
    PRIMARY KEY (name, kind)
);
"""

_ENSURE_VM_COLUMNS = """
ALTER TABLE jnpa.vehicle_master
    ADD COLUMN IF NOT EXISTS owner_name_masked  text,
    ADD COLUMN IF NOT EXISTS vehicle_class      text,
    ADD COLUMN IF NOT EXISTS fuel_type          text,
    ADD COLUMN IF NOT EXISTS insurance_valid_to date,
    ADD COLUMN IF NOT EXISTS registration_date  date,
    ADD COLUMN IF NOT EXISTS state              text,
    ADD COLUMN IF NOT EXISTS rto_code           text,
    ADD COLUMN IF NOT EXISTS blacklist_status   text DEFAULT 'CLEAR',
    ADD COLUMN IF NOT EXISTS updated_at         timestamptz DEFAULT now();
"""


async def ensure_schema(*, dsn: Optional[str] = None) -> None:
    """Create the services table + vehicle_master columns if missing."""
    # asyncpg/SQLAlchemy text() runs one statement per call; split on ';'.
    for stmt in (_ENSURE_SERVICES, _ENSURE_VM_COLUMNS):
        for piece in (s.strip() for s in stmt.split(";")):
            if piece:
                await execute(piece, dsn=dsn)


async def register_service(reg: ServiceRegistration, *, dsn: Optional[str] = None) -> None:
    """Upsert the caller into jnpa.services (PK = name+kind)."""
    await execute(
        """
        INSERT INTO jnpa.services (name, kind, base_url, healthy, enabled, registered_at, meta)
        VALUES (:name, :kind, :base_url, :healthy, :enabled, now(), CAST(:meta AS jsonb))
        ON CONFLICT (name, kind) DO UPDATE SET
            base_url      = EXCLUDED.base_url,
            healthy       = EXCLUDED.healthy,
            enabled       = EXCLUDED.enabled,
            registered_at = now(),
            meta          = EXCLUDED.meta
        """,
        {
            "name": reg.name,
            "kind": reg.kind,
            "base_url": reg.base_url,
            "healthy": reg.healthy,
            "enabled": reg.enabled,
            "meta": json.dumps(reg.meta),
        },
        dsn=dsn,
    )


async def upsert_vehicle_master(rec: VahanRecord, *, dsn: Optional[str] = None) -> None:
    """Upsert a verified RC into jnpa.vehicle_master.

    Sets provisional=false and provisional_until=null — this row represents a
    *confirmed* registration the dashboard can show as verified at the gate.
    """
    plate = rec.plate_number
    await execute(
        """
        INSERT INTO jnpa.vehicle_master (
            plate, rc_type, owner_hash, fitness_valid_to, puc_valid_to,
            fastag_status, provisional, provisional_until,
            owner_name_masked, vehicle_class, fuel_type, insurance_valid_to,
            registration_date, state, rto_code, blacklist_status, updated_at
        ) VALUES (
            :plate, :rc_type, :owner_hash, :fitness_valid_to, :puc_valid_to,
            :fastag_status, false, NULL,
            :owner_name_masked, :vehicle_class, :fuel_type, :insurance_valid_to,
            :registration_date, :state, :rto_code, :blacklist_status, now()
        )
        ON CONFLICT (plate) DO UPDATE SET
            rc_type            = EXCLUDED.rc_type,
            owner_hash         = EXCLUDED.owner_hash,
            fitness_valid_to   = EXCLUDED.fitness_valid_to,
            puc_valid_to       = EXCLUDED.puc_valid_to,
            fastag_status      = EXCLUDED.fastag_status,
            provisional        = false,
            provisional_until  = NULL,
            owner_name_masked  = EXCLUDED.owner_name_masked,
            vehicle_class      = EXCLUDED.vehicle_class,
            fuel_type          = EXCLUDED.fuel_type,
            insurance_valid_to = EXCLUDED.insurance_valid_to,
            registration_date  = EXCLUDED.registration_date,
            state              = EXCLUDED.state,
            rto_code           = EXCLUDED.rto_code,
            blacklist_status   = EXCLUDED.blacklist_status,
            updated_at         = now()
        """,
        {
            "plate": plate,
            "rc_type": rec.rc_type or rec.vehicle_class,
            "owner_hash": rec.owner_hash,
            "fitness_valid_to": rec.fitness_valid_to,
            "puc_valid_to": rec.puc_valid_to,
            "fastag_status": rec.fastag_status,
            "owner_name_masked": rec.owner_name_masked,
            "vehicle_class": rec.vehicle_class,
            "fuel_type": rec.fuel_type,
            "insurance_valid_to": rec.insurance_valid_to,
            "registration_date": rec.registration_date,
            "state": rec.state,
            "rto_code": rec.rto_code,
            "blacklist_status": (
                rec.blacklist_status.value
                if hasattr(rec.blacklist_status, "value")
                else rec.blacklist_status
            ),
        },
        dsn=dsn,
    )


async def vehicle_master_count(*, dsn: Optional[str] = None) -> int:
    row = await fetch_one("SELECT count(*) AS n FROM jnpa.vehicle_master", dsn=dsn)
    return int(row["n"]) if row else 0


__all__ = [
    "ensure_schema",
    "register_service",
    "upsert_vehicle_master",
    "vehicle_master_count",
]
