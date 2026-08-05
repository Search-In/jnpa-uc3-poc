-- ===========================================================================
-- Migration 0023 — Cargo lifecycle state management (POC-3 as the Cargo owner).
--
-- Closes the remaining UC-II -> UC-III handover gap: a single, validated cargo
-- lifecycle running CREATED -> VESSEL_DISCHARGED -> YARD_ASSIGNED ->
-- [YARD_POSITION_ALLOCATED | REEFER_PLANNED | RAKE_ASSIGNED]* -> SCAN_PENDING ->
-- VERIFIED -> RELEASED. The mandatory gates (discharge, yard-assign, verify,
-- release) cannot be skipped; the transition policy lives in services.cargo.
--
-- Everything here is ADDITIVE + idempotent (IF NOT EXISTS everywhere). Nothing
-- existing is modified: the /api/cargo shape, the legacy PUT release path, and
-- migrations 0013-0022 are all untouched. ``lifecycle_status`` is a NEW nullable
-- column with a server DEFAULT so existing rows/writers are unaffected, and a
-- one-time backfill derives a sensible lifecycle for pre-existing rows from their
-- legacy columns (is_released / yard_block) so the UC-III handover query
-- (?status=RELEASED) is correct for historical data too.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0023_cargo_lifecycle.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ---- Unified lifecycle status on the shared cargo record ---------------------
-- Nullable + DEFAULT 'CREATED': new inserts get 'CREATED' automatically; existing
-- rows are backfilled below. The CHECK enumerates the full lifecycle.
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS lifecycle_status text DEFAULT 'CREATED'
        CHECK (lifecycle_status IN (
            'CREATED','VESSEL_DISCHARGED','YARD_ASSIGNED','YARD_POSITION_ALLOCATED',
            'REEFER_PLANNED','RAKE_ASSIGNED','SCAN_PENDING','VERIFIED','RELEASED'));

CREATE INDEX IF NOT EXISTS idx_cargo_lifecycle_status ON jnpa.cargo (lifecycle_status);

-- One-time, additive backfill for rows that predate this column (lifecycle NULL):
-- released boxes -> RELEASED, yarded boxes -> YARD_ASSIGNED, everything else
-- -> CREATED. Only touches the new column; no existing column is modified.
UPDATE jnpa.cargo
   SET lifecycle_status = CASE
        WHEN is_released THEN 'RELEASED'
        WHEN yard_block IS NOT NULL THEN 'YARD_ASSIGNED'
        ELSE 'CREATED'
   END
 WHERE lifecycle_status IS NULL;

-- ---- Yard POSITION detail on the existing plan table -------------------------
-- Task #4: support block / row / slot / position. Additive nullable columns on
-- jnpa.cargo_yard_plans (migration 0018) so the yard-position endpoint records
-- the full physical address without a new table or touching existing plan rows.
ALTER TABLE jnpa.cargo_yard_plans ADD COLUMN IF NOT EXISTS yard_row      text;
ALTER TABLE jnpa.cargo_yard_plans ADD COLUMN IF NOT EXISTS yard_slot     text;
ALTER TABLE jnpa.cargo_yard_plans ADD COLUMN IF NOT EXISTS yard_position text;

-- ---- Append-only lifecycle audit history -------------------------------------
-- One row per accepted lifecycle transition (task #1: maintain audit history).
-- Never updated/deleted. Written inside the same transaction as the cargo update
-- so the audit trail can never diverge from the record's state.
CREATE TABLE IF NOT EXISTS jnpa.cargo_lifecycle_events (
    id               bigserial PRIMARY KEY,
    container_number text NOT NULL,                 -- ISO-6346 (jnpa.cargo PK)
    action           text NOT NULL,                 -- e.g. 'DISCHARGE','VERIFY','RELEASE'
    old_status       text,                          -- lifecycle_status before
    new_status       text NOT NULL,                 -- lifecycle_status after
    actor_role       text,                          -- authenticated principal role, if any
    note             text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cargo_lifecycle_ev_container ON jnpa.cargo_lifecycle_events (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_lifecycle_ev_created   ON jnpa.cargo_lifecycle_events (id DESC);

-- ---- Scan / customs verification records -------------------------------------
-- Task #6 / #9: durable scan verification records backing POST /{cn}/verify.
CREATE TABLE IF NOT EXISTS jnpa.cargo_scan_verifications (
    id               bigserial PRIMARY KEY,
    container_number text NOT NULL,                 -- ISO-6346 (jnpa.cargo PK)
    verified         boolean NOT NULL DEFAULT true,
    remarks          text,
    actor_role       text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cargo_scan_verif_container ON jnpa.cargo_scan_verifications (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_scan_verif_created   ON jnpa.cargo_scan_verifications (id DESC);
