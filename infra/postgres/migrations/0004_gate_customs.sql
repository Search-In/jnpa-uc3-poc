-- ===========================================================================
-- Migration 0004 — Customs & Gate systems persistence (e-Seal / Form-13 /
-- Weighbridge / ICEGATE capture + Auto-LEO reconciliation).
--
-- Makes the gate/customs domain RDS-backed (previously in-memory only):
--   gate_captures      : one row per captured source record (the four systems)
--   leo_reconciliation : one row per Auto-LEO reconciliation outcome
-- Customs flags are additionally written to jnpa.alerts (kind='CUSTOMS_FLAG')
-- by the gate-data service, and mirrored into jnpa.digital_twin_events by the
-- gateway alert pump — so the customs feed is durable + queryable.
--
-- Idempotent (IF NOT EXISTS) and additive: never touches existing data. The
-- gate-data service also applies this DDL at runtime (gate-data/persistence.py
-- ::ensure_gate_schema) so an existing / RDS database is topped up on boot.
--
-- APPLY (existing DB / RDS):
--   psql "$POSTGRES_DSN_PSQL" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0004_gate_customs.sql
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- A) gate_captures — every e-Seal / Form-13 / Weighbridge / ICEGATE record.
CREATE TABLE IF NOT EXISTS jnpa.gate_captures (
    id            bigserial PRIMARY KEY,
    capture_type  text NOT NULL
                  CHECK (capture_type IN ('ESEAL','FORM13','WEIGHBRIDGE','ICEGATE')),
    container_no  text,
    vehicle_plate text,
    gate_id       text,
    source_mode   text NOT NULL DEFAULT 'sim',   -- sim | live
    status        text,                          -- per-type status (ARMED/TAMPERED/GRANTED/…)
    captured_at   timestamptz,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- full typed source record
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- Idempotent seeding: the deterministic corpus (same captured_at) upserts to
    -- one row; a genuine re-capture at a new time appends a fresh audit row.
    UNIQUE (container_no, capture_type, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_gate_captures_container ON jnpa.gate_captures (container_no);
CREATE INDEX IF NOT EXISTS idx_gate_captures_plate     ON jnpa.gate_captures (vehicle_plate);
CREATE INDEX IF NOT EXISTS idx_gate_captures_type_ts   ON jnpa.gate_captures (capture_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_captures_ts        ON jnpa.gate_captures (created_at DESC);

-- B) leo_reconciliation — one row per Auto-LEO reconciliation outcome.
CREATE TABLE IF NOT EXISTS jnpa.leo_reconciliation (
    id             bigserial PRIMARY KEY,
    container_no   text,
    vehicle_plate  text,
    leo_ready      boolean NOT NULL DEFAULT false,
    customs_flags  jsonb NOT NULL DEFAULT '[]'::jsonb,
    checks         jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_mode    text NOT NULL DEFAULT 'sim',
    reconciled_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leo_recon_container ON jnpa.leo_reconciliation (container_no, reconciled_at DESC);
CREATE INDEX IF NOT EXISTS idx_leo_recon_ready     ON jnpa.leo_reconciliation (leo_ready, reconciled_at DESC);
CREATE INDEX IF NOT EXISTS idx_leo_recon_ts        ON jnpa.leo_reconciliation (reconciled_at DESC);
