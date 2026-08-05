-- ===========================================================================
-- Migration 0016 — Cargo workflow lifecycle (POC-3 as the Cargo domain owner).
--
-- POC-2 (Cargo Twin) drives a TRIGGER → APPROVE / REJECT workflow over a
-- container via /api/cargo/{container_number}/workflow. The CURRENT state lives
-- on a new nullable ``jnpa.cargo.workflow_status`` column (backward compatible —
-- existing rows/writers are unaffected); the full transition history lives in an
-- APPEND-ONLY log so nothing is ever mutated in place.
--
-- Additive + idempotent (IF NOT EXISTS everywhere). Does NOT touch any existing
-- column or the /api/cargo contract.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0016_cargo_workflows.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- Current workflow state on the shared cargo record (nullable -> backward compatible).
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS workflow_status text
        CHECK (workflow_status IN ('TRIGGERED','APPROVED','REJECTED'));

-- Append-only workflow transition log. One row per action; never updated/deleted.
CREATE TABLE IF NOT EXISTS jnpa.cargo_workflow_events (
    id               bigserial PRIMARY KEY,
    container_number text NOT NULL,                 -- ISO-6346 (jnpa.cargo PK)
    action           text NOT NULL
                     CHECK (action IN ('TRIGGER','APPROVE','REJECT')),
    old_status       text,                          -- workflow_status before the action
    new_status       text,                          -- workflow_status after the action
    comment          text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cargo_workflow_container ON jnpa.cargo_workflow_events (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_workflow_created   ON jnpa.cargo_workflow_events (id DESC);
