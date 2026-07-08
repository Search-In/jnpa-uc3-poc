-- ===========================================================================
-- Migration 0008 — Vehicle & Driver Intelligence (Vahan / Sarathi) history.
-- Persists every RC verification and every DL lookup (request + response +
-- status + source + timestamp). Complements api_audit_log (the raw envelope) and
-- vehicle_master / drivers (the canonical parsed records). Additive + idempotent;
-- runtime-applied by gateway/vehicle_intel.py.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0008_vehicle_driver_intel.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

CREATE TABLE IF NOT EXISTS jnpa.vehicle_verification_history (
    id                  bigserial PRIMARY KEY,
    vehicle_number      text,
    request_payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    verification_status text,                     -- VERIFIED | PROVISIONAL | NOT_FOUND | ERROR
    source              text,                     -- LIVE_PRIMARY | LIVE_FALLBACK | CACHED | SIM | PROVISIONAL
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_veh_verif_number ON jnpa.vehicle_verification_history (vehicle_number, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_veh_verif_ts     ON jnpa.vehicle_verification_history (created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.driver_license_lookup_history (
    id                bigserial PRIMARY KEY,
    dl_number         text,
    request_payload   jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload  jsonb NOT NULL DEFAULT '{}'::jsonb,
    status            text,                       -- VALID | EXPIRED | NOT_FOUND | ERROR
    source            text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dl_lookup_number ON jnpa.driver_license_lookup_history (dl_number, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dl_lookup_ts     ON jnpa.driver_license_lookup_history (created_at DESC);
