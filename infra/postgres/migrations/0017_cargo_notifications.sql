-- ===========================================================================
-- Migration 0017 — Cargo stakeholder notifications (POC-3 as the Cargo owner).
--
-- POC-2 (Cargo Twin) raises stakeholder notification events (customs alerts,
-- pendency warnings, control-room escalations) via POST /api/cargo/notifications
-- and polls them via GET /api/cargo/notifications. This is a distinct concern
-- from the append-only cargo lifecycle event log (jnpa.cargo_events, migration
-- 0015): notifications are ADDRESSED to named stakeholders with a severity, and
-- carry their own status.
--
-- Additive + idempotent. Does NOT touch any existing table or contract.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0017_cargo_notifications.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

CREATE TABLE IF NOT EXISTS jnpa.cargo_notifications (
    id                bigserial PRIMARY KEY,
    container_number  text NOT NULL,                -- ISO-6346 (jnpa.cargo PK)
    notification_type text NOT NULL,                -- e.g. 'CUSTOMS_ALERT'
    severity          text NOT NULL DEFAULT 'MEDIUM'
                      CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    message           text,
    stakeholders      jsonb NOT NULL DEFAULT '[]'::jsonb,   -- ["operator","customs",...]
    status            text NOT NULL DEFAULT 'CREATED'
                      CHECK (status IN ('CREATED','ACKNOWLEDGED','RESOLVED')),
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- Filter paths for the notifications board (GET filters: container / type / severity / status).
CREATE INDEX IF NOT EXISTS idx_cargo_notif_container ON jnpa.cargo_notifications (container_number);
CREATE INDEX IF NOT EXISTS idx_cargo_notif_type      ON jnpa.cargo_notifications (notification_type);
CREATE INDEX IF NOT EXISTS idx_cargo_notif_severity  ON jnpa.cargo_notifications (severity);
CREATE INDEX IF NOT EXISTS idx_cargo_notif_status    ON jnpa.cargo_notifications (status);
CREATE INDEX IF NOT EXISTS idx_cargo_notif_created   ON jnpa.cargo_notifications (id DESC);
