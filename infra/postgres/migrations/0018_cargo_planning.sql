-- ===========================================================================
-- Migration 0018 — Cargo planning (yard / rake / reefer). POC-3 Cargo owner.
--
-- POC-2 (Cargo Twin) plans terminal operations through POC-3:
--   * yard planning   — pre-allocate a slot in a preferred block   (POST /api/cargo/yard-planning)
--   * rake planning    — group containers onto a rail rake           (POST /api/cargo/rake-planning)
--   * reefer planning  — allocate a powered reefer slot              (POST /api/cargo/reefer-planning)
--
-- These are FORWARD-LOOKING plans, kept separate from the live jnpa.cargo record
-- and the yard-assignment write (which mutates jnpa.cargo.yard_block). Additive +
-- idempotent; nothing existing is touched.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0018_cargo_planning.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ---- Yard planning -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.cargo_yard_plans (
    id                bigserial PRIMARY KEY,
    container_number  text NOT NULL,                -- ISO-6346 (jnpa.cargo PK)
    preferred_block   text,                          -- requested zone, e.g. 'B'
    assigned_block    text NOT NULL,                 -- computed slot, e.g. 'B-09'
    priority          text NOT NULL DEFAULT 'MEDIUM'
                      CHECK (priority IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    status            text NOT NULL DEFAULT 'PLANNED',
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cargo_yard_plan_container ON jnpa.cargo_yard_plans (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_yard_plan_block     ON jnpa.cargo_yard_plans (assigned_block);
CREATE INDEX IF NOT EXISTS idx_cargo_yard_plan_created   ON jnpa.cargo_yard_plans (id DESC);

-- ---- Rake planning -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.cargo_rake_plans (
    id                 bigserial PRIMARY KEY,
    rake_id            text NOT NULL,                -- e.g. 'RKE001'
    containers         jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ["GESU5123996", ...]
    planned_containers integer NOT NULL DEFAULT 0,
    status             text NOT NULL DEFAULT 'PLANNED',
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cargo_rake_plan_rake    ON jnpa.cargo_rake_plans (rake_id);
CREATE INDEX IF NOT EXISTS idx_cargo_rake_plan_created ON jnpa.cargo_rake_plans (id DESC);

-- ---- Reefer planning ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.cargo_reefer_plans (
    id                bigserial PRIMARY KEY,
    container_number  text NOT NULL,                -- ISO-6346 (jnpa.cargo PK)
    temperature       numeric,                       -- set-point, degrees C
    power_required    boolean NOT NULL DEFAULT true,
    slot              text NOT NULL,                 -- computed slot, e.g. 'REEFER-A12'
    status            text NOT NULL DEFAULT 'ALLOCATED',
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cargo_reefer_plan_container ON jnpa.cargo_reefer_plans (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_reefer_plan_created   ON jnpa.cargo_reefer_plans (id DESC);
