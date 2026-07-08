-- ===========================================================================
-- Migration 0001 — ULIP FASTag foundation tables.
--
-- WHY THIS EXISTS: infra/postgres/init.sql runs ONLY on a fresh Postgres volume
-- (docker-entrypoint-initdb.d). An already-provisioned / production database will
-- NOT pick up the FASTag tables from init.sql. Apply THIS migration to add them
-- to an existing database.
--
-- Idempotent: every statement is IF NOT EXISTS, so re-running is a safe no-op.
-- Self-contained: creates the schema + pgcrypto extension if missing, so it runs
-- standalone (does not assume init.sql ran first).
--
-- APPLY (existing DB):
--   psql "$POSTGRES_DSN_PSQL" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0001_fastag.sql
--   -- where POSTGRES_DSN_PSQL is the libpq form, e.g.
--   --   postgresql://postgres:jnpa_pw@postgres:5432/postgres
--   -- (the app uses the SQLAlchemy form postgresql+asyncpg://...; strip "+asyncpg").
--
-- VERIFY:
--   \dt jnpa.fastag_balance  \dt jnpa.fastag_transactions  \dt jnpa.toll_enroute
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- A) RC-based FASTag balance snapshot (latest known state per registration).
CREATE TABLE IF NOT EXISTS jnpa.fastag_balance (
    rc_number                 text PRIMARY KEY,
    tag_id                    text,
    provider_name             text,
    provider_code             text,
    customer_name             text,
    available_recharge_limit  numeric(10,2),
    available_balance         numeric(10,2),
    tag_status                text,
    vehicle_class             text,
    vehicle_class_desc        text,
    model_name                text,              -- nullable per ULIP spec
    updated_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fastag_balance_tag
    ON jnpa.fastag_balance (tag_id);

-- B) FASTag plaza transactions. seq_no is the vendor idempotency key: UNIQUE so a
-- replayed batch cannot double-insert a crossing.
CREATE TABLE IF NOT EXISTS jnpa.fastag_transactions (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_id                 text,
    rc_number              text,
    seq_no                 text UNIQUE,
    transaction_date_time  timestamptz,
    lane_direction         text,
    toll_plaza_name        text,
    toll_plaza_geocode     text,               -- raw "lat,lng" as returned by vendor
    vehicle_type           text,
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fastag_txn_rc
    ON jnpa.fastag_transactions (rc_number, transaction_date_time DESC);
CREATE INDEX IF NOT EXISTS idx_fastag_txn_tag
    ON jnpa.fastag_transactions (tag_id, transaction_date_time DESC);

-- C) Toll Enroute route lookups. Full toll_plaza_details array preserved as JSONB.
CREATE TABLE IF NOT EXISTS jnpa.toll_enroute (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           text,
    source_state        text,
    source_name         text,
    destination_state   text,
    destination_name    text,
    vehicle_type        text,
    duration            text,
    distance            numeric(10,2),
    toll_plaza_details  jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_toll_enroute_route
    ON jnpa.toll_enroute (source_name, destination_name, created_at DESC);
