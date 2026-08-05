-- 0020_carbon_emission.sql — durable per-vehicle carbon-emission ledger (UC-3 audit R6).
--
-- The carbon calculator (carbon/calculator.py) was compute-only: /api/carbon/rollup
-- and /estimate returned figures but nothing was persisted, so the audit flagged R6
-- as "no persistence". This adds the ledger table the calculation flow writes to:
--
--     Truck telemetry -> Carbon Calculator -> INSERT jnpa.carbon_emission -> API response
--
-- Idempotent and in the existing jnpa schema style (bigserial PK, timestamptz UTC,
-- hot-path indexes on vehicle_id + created_at). Mirrored into init.sql for fresh
-- volumes and self-provisioned lazily by gateway/routers/carbon.py for existing ones.

CREATE SCHEMA IF NOT EXISTS jnpa;

CREATE TABLE IF NOT EXISTS jnpa.carbon_emission (
    id                  bigserial PRIMARY KEY,
    vehicle_id          text NOT NULL,
    vehicle_type        text,
    distance_km         numeric,
    fuel_consumed_litre numeric,
    idle_time_minutes   numeric,
    co2_kg              numeric,
    source              text,
    calculation_method  text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_carbon_emission_vehicle ON jnpa.carbon_emission (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_carbon_emission_created ON jnpa.carbon_emission (created_at DESC);
