-- ===========================================================================
-- Migration 0005 — Parking Management (production-ready, RDS-backed).
-- Replaces the simulated sine-curve occupancy with real inventory + slot state +
-- entry/exit transactions + a parking-event log. Reuses the existing audit
-- framework tables (digital_twin_events / notifications / alerts) — those are
-- NOT modified here.
--
-- Idempotent (IF NOT EXISTS), additive, never touches existing data. The parking
-- service also applies this DDL at runtime (parking/persistence.py).
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0005_parking.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

CREATE TABLE IF NOT EXISTS jnpa.parking_facilities (
    id            text PRIMARY KEY,
    facility_name text NOT NULL,
    location      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {lat,lon,gate_id}
    capacity      integer NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'OPEN',         -- OPEN | CLOSED | FULL
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jnpa.parking_slots (
    id                  bigserial PRIMARY KEY,
    facility_id         text NOT NULL REFERENCES jnpa.parking_facilities(id) ON DELETE CASCADE,
    slot_number         text NOT NULL,
    availability_status text NOT NULL DEFAULT 'AVAILABLE'
                        CHECK (availability_status IN ('AVAILABLE','OCCUPIED','RESERVED','OUT_OF_SERVICE')),
    vehicle_id          text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (facility_id, slot_number)
);
CREATE INDEX IF NOT EXISTS idx_parking_slots_facility ON jnpa.parking_slots (facility_id, availability_status);
CREATE INDEX IF NOT EXISTS idx_parking_slots_vehicle  ON jnpa.parking_slots (vehicle_id);

CREATE TABLE IF NOT EXISTS jnpa.parking_transactions (
    id          bigserial PRIMARY KEY,
    vehicle_id  text,
    driver_id   text,
    facility_id text,
    slot_id     bigint,
    entry_time  timestamptz NOT NULL DEFAULT now(),
    exit_time   timestamptz,
    duration    interval,
    status      text NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','COMPLETED','EXPIRED')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_parking_txn_vehicle ON jnpa.parking_transactions (vehicle_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_parking_txn_status  ON jnpa.parking_transactions (status, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_parking_txn_facility ON jnpa.parking_transactions (facility_id, entry_time DESC);

CREATE TABLE IF NOT EXISTS jnpa.parking_events (
    id          bigserial PRIMARY KEY,
    event_type  text NOT NULL
                CHECK (event_type IN ('ALLOCATION','RELEASE','OVERFLOW',
                                      'ILLEGAL_PARKING','NO_PARKING_VIOLATION')),
    vehicle_id  text,
    driver_id   text,
    facility_id text,
    slot_id     bigint,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_parking_events_type ON jnpa.parking_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parking_events_vehicle ON jnpa.parking_events (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_parking_events_ts ON jnpa.parking_events (created_at DESC);
