-- ===========================================================================
-- Migration 0009 — OTP login + device binding (Phase 2 · Track 5 security).
-- Replaces the static PWA pairing with an OTP-based login. Stores each OTP
-- request (hashed code, expiry, attempts) and the device binding established on
-- successful verification. OTP delivery is logged to jnpa.notifications (SMS-ready).
-- Additive + idempotent; runtime-applied by gateway/routers/otp.py.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0009_otp_auth.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

CREATE TABLE IF NOT EXISTS jnpa.otp_requests (
    id          bigserial PRIMARY KEY,
    mobile      text NOT NULL,
    device_id   text,
    code_hash   text NOT NULL,
    expires_at  timestamptz NOT NULL,
    verified    boolean NOT NULL DEFAULT false,
    attempts    integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_otp_mobile ON jnpa.otp_requests (mobile, created_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.device_bindings (
    device_id   text PRIMARY KEY,
    mobile      text NOT NULL,
    driver_id   text,
    bound_at    timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    active      boolean NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_device_bindings_mobile ON jnpa.device_bindings (mobile);
