-- ===========================================================================
-- Migration 0013 — Cargo (Traffic Twin ⇄ Cargo Twin shared record).
--
-- POC-3 becomes the single common backend: the Cargo CRUD surface (/api/cargo)
-- reads/writes THIS one table on the shared RDS, and BOTH the POC-3 dashboard
-- and the POC-2 (Cargo Twin) frontend consume it. POC-2 keeps no backend/DB of
-- its own — there is exactly one cargo record, here.
--
-- `container_number` is the ISO-6346 primary key (the cross-twin "follow-the-box"
-- join key — see jnpa_shared/iso6346.py). Additive + idempotent; reuses the
-- audit-framework tables (NOT modified here). `updated_at` is maintained by a
-- BEFORE-UPDATE trigger (jnpa.set_updated_at) so the timestamp is authoritative
-- regardless of which client issues the UPDATE.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0013_cargo.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- Reusable updated_at maintainer (generic; created once, shared by any table
-- that wants server-side updated_at). CREATE OR REPLACE keeps this idempotent.
CREATE OR REPLACE FUNCTION jnpa.set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS jnpa.cargo (
    container_number text PRIMARY KEY,          -- ISO-6346 (validated at the API)
    vessel_name      text,
    customs_status   text NOT NULL DEFAULT 'PENDING'
                     CHECK (customs_status IN ('PENDING','CLEARED','HELD','UNDER_INSPECTION')),
    yard_block       text,
    is_released      boolean NOT NULL DEFAULT false,
    vehicle_number   text,                       -- allocated haulage truck plate (nullable)
    gate             text,                        -- gate id (see jnpa.gates.id)
    camera_id        text,                        -- ANPR camera id (see jnpa.cameras.id)
    eta              timestamptz,                 -- estimated time of arrival
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Query paths: customs board (status + release), yard board, gate/vehicle lookups,
-- and ETA-ordered arrival lists.
CREATE INDEX IF NOT EXISTS idx_cargo_customs_status ON jnpa.cargo (customs_status);
CREATE INDEX IF NOT EXISTS idx_cargo_is_released    ON jnpa.cargo (is_released);
CREATE INDEX IF NOT EXISTS idx_cargo_yard_block     ON jnpa.cargo (yard_block);
CREATE INDEX IF NOT EXISTS idx_cargo_vehicle        ON jnpa.cargo (vehicle_number) WHERE vehicle_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cargo_eta            ON jnpa.cargo (eta DESC NULLS LAST);

DROP TRIGGER IF EXISTS trg_cargo_updated_at ON jnpa.cargo;
CREATE TRIGGER trg_cargo_updated_at
    BEFORE UPDATE ON jnpa.cargo
    FOR EACH ROW EXECUTE FUNCTION jnpa.set_updated_at();
