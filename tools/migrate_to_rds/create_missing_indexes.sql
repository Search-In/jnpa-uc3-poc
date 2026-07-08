-- ===========================================================================
-- Indexes that must exist on RDS after migration.
-- ===========================================================================
-- When a TimescaleDB hypertable is recreated as a *plain* PostgreSQL table on
-- RDS, the indexes TimescaleDB auto-created on the time column are lost. The
-- application also relies on the composite indexes below for its query paths.
--
-- Run this on RDS AFTER the data is migrated (creating indexes after bulk load
-- is much faster than maintaining them during the load).
--
-- All statements use IF NOT EXISTS, so this is safe to run repeatedly and safe
-- to run even if your schema DDL already created some of them.
--
-- NOTE: `migrate.py emit-indexes` generates the authoritative, always-current
-- version of this file straight from the live source database:
--     python migrate.py emit-indexes --out create_missing_indexes.generated.sql
-- Prefer that output if your schema has drifted from this checked-in copy.
-- ===========================================================================

SET search_path TO jnpa, public;

-- --- Hypertable time-column indexes (TimescaleDB auto-created these on the
-- --- source; they do NOT exist on a plain-Postgres target) -----------------
CREATE INDEX IF NOT EXISTS anpr_reads_ts_idx          ON jnpa.anpr_reads (ts DESC);
CREATE INDEX IF NOT EXISTS rfid_reads_ts_idx          ON jnpa.rfid_reads (ts DESC);
CREATE INDEX IF NOT EXISTS truck_telemetry_ts_idx     ON jnpa.truck_telemetry (ts DESC);
CREATE INDEX IF NOT EXISTS traffic_snapshots_ts_idx   ON jnpa.traffic_snapshots (ts DESC);

-- --- Hypertable secondary indexes defined in application DDL ---------------
CREATE INDEX IF NOT EXISTS idx_anpr_plate_ts          ON jnpa.anpr_reads (plate, ts DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_plate_ts     ON jnpa.truck_telemetry (plate, ts DESC);

-- Add a segment/time index for traffic queries (parity with hypertable usage).
CREATE INDEX IF NOT EXISTS idx_traffic_segment_ts     ON jnpa.traffic_snapshots (segment_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_rfid_tag_ts            ON jnpa.rfid_reads (tag_id, ts DESC);

-- ---------------------------------------------------------------------------
-- Every other (non-hypertable) index should already be present because it is
-- part of the schema DDL you loaded on RDS. If you are unsure, run:
--     python migrate.py emit-indexes
-- and apply the generated file - it reproduces ALL non-primary-key indexes.
-- ---------------------------------------------------------------------------

ANALYZE jnpa.anpr_reads;
ANALYZE jnpa.rfid_reads;
ANALYZE jnpa.truck_telemetry;
ANALYZE jnpa.traffic_snapshots;
