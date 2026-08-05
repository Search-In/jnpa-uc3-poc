-- ===========================================================================
-- Migration 0015 — Cargo contract extensions (POC-3 as the Cargo domain owner).
--
-- UC-2 (Cargo Twin) consumes /api/cargo but keeps NO backend of its own. These
-- additive columns + the append-only event log close the remaining POC-3 backend
-- contract gaps UC-2 needs, WITHOUT touching the existing /api/cargo shape:
--
--   * eseal_status / eseal_number   — electronic seal state + id (e-Seal support)
--   * pre_document_status           — pre-document (pre-gate paperwork) state
--   * origin_stream                 — cargo source stream (e.g. 'UC-II', 'UC-III')
--   * jnpa.cargo_events             — append-only cargo lifecycle event log that
--                                     backs the notifications contract (created /
--                                     released / yard_assigned / status_changed /
--                                     gate_movement), polled by UC-2.
--
-- All columns are NULLable so existing rows and existing writers are unaffected
-- (backward compatible). CHECK sets mirror the customs_status pattern already on
-- the table. Idempotent (IF NOT EXISTS everywhere).
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0015_cargo_contracts.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ---- e-Seal, pre-document, origin-stream columns on the shared cargo record ---
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS eseal_status text
        CHECK (eseal_status IN ('ACTIVE','ARMED','TAMPERED','REMOVED','NONE'));
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS eseal_number text;
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS pre_document_status text
        CHECK (pre_document_status IN ('NOT_STARTED','PENDING','IN_PROGRESS','COMPLETED'));
ALTER TABLE jnpa.cargo
    ADD COLUMN IF NOT EXISTS origin_stream text;

-- Query paths added for the new fields (source-stream boards, e-Seal + pre-doc filters).
CREATE INDEX IF NOT EXISTS idx_cargo_origin_stream        ON jnpa.cargo (origin_stream);
CREATE INDEX IF NOT EXISTS idx_cargo_eseal_status         ON jnpa.cargo (eseal_status);
CREATE INDEX IF NOT EXISTS idx_cargo_pre_document_status  ON jnpa.cargo (pre_document_status);

-- ---- Append-only cargo lifecycle event log (notifications contract) ----------
-- One row per cargo lifecycle transition. POC-3 writes it from CargoService (the
-- single orchestration point); UC-2 reads it via GET /api/cargo/events (poll by
-- ``since`` id). ``payload`` carries the event-specific detail (the changed
-- fields / new values) as JSON so the contract can grow without a schema change.
CREATE TABLE IF NOT EXISTS jnpa.cargo_events (
    id               bigserial PRIMARY KEY,
    event            text NOT NULL,                 -- e.g. 'cargo.released'
    container_number text NOT NULL,                 -- ISO-6346 (jnpa.cargo PK)
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cargo_events_created   ON jnpa.cargo_events (id DESC);
CREATE INDEX IF NOT EXISTS idx_cargo_events_container ON jnpa.cargo_events (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_events_event     ON jnpa.cargo_events (event);
