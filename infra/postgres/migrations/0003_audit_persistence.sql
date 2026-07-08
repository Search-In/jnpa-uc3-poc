-- ===========================================================================
-- Migration 0003 — Common audit & persistence framework (single source of truth).
--
-- WHY THIS EXISTS: infra/postgres/init.sql runs ONLY on a fresh Postgres volume
-- (docker-entrypoint-initdb.d). An already-provisioned / RDS database will NOT
-- pick up these tables from init.sql. Apply THIS migration to add them to an
-- existing database. The gateway ALSO applies the same DDL at runtime via
-- gateway/audit.py::ensure_audit_schema(), so a running stack is topped up on
-- boot without a manual step — this file is the record + the manual RDS path.
--
-- Idempotent: every statement is IF NOT EXISTS, so re-running is a safe no-op and
-- NO existing table or row is touched or dropped.
-- Self-contained: creates the schema + pgcrypto if missing (standalone-safe).
--
-- APPLY (existing DB / RDS):
--   psql "$POSTGRES_DSN_PSQL" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0003_audit_persistence.sql
--   -- POSTGRES_DSN_PSQL is the libpq form, e.g.
--   --   postgresql://postgres:jnpa_pw@postgres:5432/postgres
--   -- (the app uses the SQLAlchemy form postgresql+asyncpg://...; strip "+asyncpg").
--
-- VERIFY:
--   \dt jnpa.api_audit_log  \dt jnpa.digital_twin_events  \dt jnpa.notifications
--   \dt jnpa.decision_audit \dt jnpa.geofence_events
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE SCHEMA IF NOT EXISTS jnpa;
SET search_path TO jnpa, public;

-- ---------------------------------------------------------------------------
-- A) api_audit_log — every external API request + response (system-of-record for
--    the integration audit trail: Vahan / Sarathi / FASTag / ULIP / e-Seal /
--    Form-13 / ICEGATE / Weighbridge). Written automatically by the gateway's
--    AuditingAsyncClient on every outbound call, and by any service that calls
--    jnpa_shared.audit helpers on its own egress hop.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.api_audit_log (
    id               bigserial PRIMARY KEY,
    service_name     text NOT NULL,            -- logical integration, e.g. 'vahan'
    endpoint         text,                     -- method + URL/path
    method           text,                     -- GET | POST | ...
    request_payload  jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status_code      integer,                  -- HTTP status; NULL on transport error
    latency_ms       numeric(10,2),
    error            text,                     -- set when the call raised / failed
    transaction_id   text,                     -- correlation id across a request chain
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_api_audit_service_ts
    ON jnpa.api_audit_log (service_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_audit_txn
    ON jnpa.api_audit_log (transaction_id);
CREATE INDEX IF NOT EXISTS idx_api_audit_ts
    ON jnpa.api_audit_log (created_at DESC);

-- ---------------------------------------------------------------------------
-- B) digital_twin_events — every operational event, unified. AI detections, ANPR
--    reads, geo-fence / parking / route-deviation violations, congestion + customs
--    alerts and any AI event land here as one queryable timeline.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.digital_twin_events (
    id           bigserial PRIMARY KEY,
    event_type   text NOT NULL,               -- VEHICLE_DETECTED | ANPR_DETECTION |
                                               -- GEOFENCE_VIOLATION | PARKING_VIOLATION |
                                               -- ROUTE_DEVIATION | CONGESTION_ALERT |
                                               -- CUSTOMS_ALERT | AI_EVENT ...
    vehicle_id   text,                         -- plate / vehicle number
    driver_id    text,
    location     jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {lat,lon,gate_id,segment_id,...}
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dt_events_type_ts
    ON jnpa.digital_twin_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_vehicle_ts
    ON jnpa.digital_twin_events (vehicle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_driver_ts
    ON jnpa.digital_twin_events (driver_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dt_events_ts
    ON jnpa.digital_twin_events (created_at DESC);

-- ---------------------------------------------------------------------------
-- C) notifications — delivery audit trail for every notification (WebPush / SMS /
--    WebSocket / e-mail). Proves an advisory / challan notice was dispatched.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.notifications (
    id                bigserial PRIMARY KEY,
    event_id          text,                    -- links to alerts.id / dt_events.id / case
    channel           text NOT NULL,           -- webpush | sms | ws | email
    receiver          text,                    -- device_id / msisdn / topic
    message           text,
    delivery_status   text NOT NULL DEFAULT 'PENDING'
                      CHECK (delivery_status IN
                             ('PENDING','SENT','DELIVERED','FAILED','SKIPPED','NO_SUBSCRIPTION')),
    provider_response jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts
    ON jnpa.notifications (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_receiver
    ON jnpa.notifications (receiver, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_status
    ON jnpa.notifications (delivery_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_event
    ON jnpa.notifications (event_id);

-- ---------------------------------------------------------------------------
-- D) decision_audit — durable replacement for the in-memory DecisionRing. Every
--    orchestrated fallback decision (which rung served each request) is persisted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.decision_audit (
    id            bigserial PRIMARY KEY,
    request_id    text,                        -- correlation id / decision key
    input_data    jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_executed text,                        -- the api / chain evaluated
    decision      text,                        -- the decision_path chosen (LIVE/CACHED/...)
    action_taken  text,
    created_at    timestamptz NOT NULL DEFAULT now()   -- "timestamp"
);
CREATE INDEX IF NOT EXISTS idx_decision_audit_ts
    ON jnpa.decision_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_audit_request
    ON jnpa.decision_audit (request_id);
CREATE INDEX IF NOT EXISTS idx_decision_audit_rule
    ON jnpa.decision_audit (rule_executed, created_at DESC);

-- ---------------------------------------------------------------------------
-- E) geofence_events — enter/exit + dwell violations against jnpa.geofence_zones.
--    (zone_id is a soft reference to geofence_zones.id — kept text so an event
--    survives a zone rename/delete for the audit record.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jnpa.geofence_events (
    id             bigserial PRIMARY KEY,
    vehicle_id     text,
    zone_id        text,
    entry_time     timestamptz,
    exit_time      timestamptz,
    violation_type text,                       -- ENTER | EXIT | ILLEGAL_PARKING | ABANDONED
    action_taken   text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_geofence_events_vehicle
    ON jnpa.geofence_events (vehicle_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_zone
    ON jnpa.geofence_events (zone_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_geofence_events_ts
    ON jnpa.geofence_events (created_at DESC);
