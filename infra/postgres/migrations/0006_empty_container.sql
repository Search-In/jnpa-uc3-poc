-- ===========================================================================
-- Migration 0006 — Empty Container Allocation (production persistence).
-- The optimiser existed but allocation results were ephemeral. This lands the
-- inventory, every allocation, and the movement history in RDS. Reuses the audit
-- framework tables (digital_twin_events / decision_audit / api_audit_log) — NOT
-- modified here. Idempotent + additive. Runtime-applied by
-- empty-container/persistence.py.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0006_empty_container.sql
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

CREATE TABLE IF NOT EXISTS jnpa.empty_container_inventory (
    container_id        text PRIMARY KEY,
    container_type      text,                    -- 20GP | 40GP | 40HC | REEFER | TANKER …
    location            text,                    -- depot / ECD / CFS id
    owner               text,                    -- shipping_line | fleet_owner | CFS | ECD
    availability_status text NOT NULL DEFAULT 'AVAILABLE'
                        CHECK (availability_status IN ('AVAILABLE','ALLOCATED','IN_TRANSIT','DELIVERED')),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ec_inventory_status ON jnpa.empty_container_inventory (availability_status, container_type);
CREATE INDEX IF NOT EXISTS idx_ec_inventory_location ON jnpa.empty_container_inventory (location);

CREATE TABLE IF NOT EXISTS jnpa.empty_container_allocations (
    id                bigserial PRIMARY KEY,
    container_id      text,
    truck_id          text,
    trailer_id        text,
    driver_id         text,
    shipping_line     text,
    cfs               text,
    ecd               text,
    allocation_reason text,
    allocated_at      timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'ALLOCATED'
                      CHECK (status IN ('ALLOCATED','PICKED_UP','IN_TRANSIT','DELIVERED','COMPLETED','CANCELLED'))
);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_container ON jnpa.empty_container_allocations (container_id, allocated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_status    ON jnpa.empty_container_allocations (status, allocated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ec_alloc_ts        ON jnpa.empty_container_allocations (allocated_at DESC);

CREATE TABLE IF NOT EXISTS jnpa.container_movement_history (
    id            bigserial PRIMARY KEY,
    container_id  text,
    allocation_id bigint,
    movement_type text NOT NULL
                  CHECK (movement_type IN ('PICKUP','ALLOCATION','TRANSFER','DELIVERY','COMPLETION')),
    location      text,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_container_move_container ON jnpa.container_movement_history (container_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_container_move_type ON jnpa.container_movement_history (movement_type, created_at DESC);
