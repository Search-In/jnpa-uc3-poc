-- ============================================================================
-- 0141  Repair the plate lookup indexes behind Vehicle & Driver Intelligence.
--
-- FINDING (2026-08-12, measured against RDS jnpa_schema_v3):
--
--     GET /api/vahan/vehicle-360/MH04DV3973  ->  never returns; the console
--     aborts at 15 s and paints "Unable to load live data / 408 ETIMEDOUT".
--
--   Isolated to ONE leg of the aggregate:
--
--       SELECT ts, lat, lon, speed_kmh FROM core.truck_telemetry
--        WHERE plate = 'MH04DV3973' ORDER BY ts DESC LIMIT 20;
--
--   still had not returned after 90 s. Its plan:
--
--       Limit
--         -> Gather Merge  (workers: 2)
--              -> Parallel Index Scan using idx_truck_telemetry_ts
--                   Filter: (plate = 'MH04DV3973'::text)
--
--   ROOT CAUSE — `core.idx_truck_telemetry_plate_ts` EXISTS but is INVALID:
--
--       SELECT indisvalid FROM pg_index
--        WHERE indexrelid = 'core.idx_truck_telemetry_plate_ts'::regclass;
--       -- false        (23 GB of index the planner is not allowed to use)
--
--   It was built by hand with CREATE INDEX CONCURRENTLY and that build was
--   interrupted — the exact failure mode 0116 documents under RECOVERY, and the
--   same one that previously killed idx_truck_telemetry_device_ts. Because the
--   index is invalid the planner falls back to the 14 GB (ts DESC) index and
--   applies `plate` as a FILTER. For a plate WITH recent telemetry that stops
--   early (95 ms, "Rows Removed by Filter: 273,256"). For a plate with NO
--   telemetry rows — MH04DV3973 — "none" cannot be known until all 423,254,061
--   rows have been read. That is the 408.
--
--   No migration in this repo ever created idx_truck_telemetry_plate_ts, so
--   `make migrate` had no way to notice or repair it. This file gives it one.
--
--   Two smaller offenders on the same screen, fixed here while we are in the
--   right place — both are plate lookups that no index can serve today:
--
--     * core.gate_event (774,389 rows) — vehicle_360's gate timeline compares a
--       NORMALISED plate, `regexp_replace(upper(coalesce(plate,'')), ...)`, so
--       a plain index on `plate` is useless to it. Measured 8.3 s wall /
--       25,309 buffers. An index on the EXPRESSION is what makes it sargable;
--       it must match gateway/vehicle_intel.py:_norm_sql() character for
--       character or the planner will not use it.
--     * core.alert (152,030 rows) — `WHERE plate = ... ORDER BY ts DESC` plans
--       as a Parallel Seq Scan. Cheap today (16 ms), unbounded as alerts grow.
--
-- SAFETY
--   * ADDITIVE apart from the one DROP, and that DROP targets an index the
--     planner already refuses to use — dropping it removes no capability and
--     reclaims 23 GB. Every other statement creates an index. No table, column,
--     constraint, view or row is altered; no data is rewritten.
--   * IDEMPOTENT. DROP ... IF EXISTS + CREATE ... IF NOT EXISTS throughout.
--   * NOT wrapped in BEGIN/COMMIT: CONCURRENTLY is forbidden inside a
--     transaction block. scripts/migrate.py detects that and runs this file in
--     autocommit (and sweeps invalid indexes first, which is what makes the
--     rebuild below actually rebuild rather than silently skip on the name).
--   * CONCURRENTLY throughout, so core.truck_telemetry stays readable AND
--     writable for the whole build — the telemetry COPY ingest keeps running.
--
-- COST
--   ~10-20 min and ~23 GB for the truck_telemetry rebuild; seconds for the
--   other two. Net storage change is ~0 (the invalid 23 GB is freed first).
--
-- RECOVERY
--   An interrupted CONCURRENTLY build leaves ANOTHER invalid index. Same drill:
--       SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
--       DROP INDEX CONCURRENTLY <name>;
--   ...then re-run. `make migrate` does this sweep automatically.
--
-- VERIFY (after applying)
--   SELECT indisvalid FROM pg_index
--    WHERE indexrelid = 'core.idx_truck_telemetry_plate_ts'::regclass;  -- t
--
--   EXPLAIN (ANALYZE, BUFFERS) SELECT ts, lat, lon, speed_kmh
--     FROM core.truck_telemetry WHERE plate = 'MH04DV3973'
--     ORDER BY ts DESC LIMIT 20;
--   -- expect "Index Scan using idx_truck_telemetry_plate_ts", 0 rows, << 10 ms
--   -- (was: Parallel Index Scan using idx_truck_telemetry_ts, > 90 s)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. core.truck_telemetry — the 90s+ offender. Drop the dead index FIRST:
--    CREATE INDEX CONCURRENTLY IF NOT EXISTS matches on NAME, so without the
--    drop it would see the invalid index, skip, and report success while
--    leaving the timeout in place.
-- ----------------------------------------------------------------------------
DROP INDEX CONCURRENTLY IF EXISTS core.idx_truck_telemetry_plate_ts;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_truck_telemetry_plate_ts
    ON core.truck_telemetry (plate, ts DESC);

-- ----------------------------------------------------------------------------
-- 2. core.gate_event — normalised-plate lookup (vehicle_360 gate timeline).
--    The expression MUST stay identical to _norm_sql() in
--    gateway/vehicle_intel.py; change one and this index stops being used.
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gate_event_plate_norm_ts
    ON core.gate_event (
        (regexp_replace(upper(coalesce(plate, '')), '[^A-Z0-9]', '', 'g')),
        ts DESC);

-- ----------------------------------------------------------------------------
-- 3. core.alert — plate lookup (vehicle_intel alerts card).
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_alert_plate_ts
    ON core.alert (plate, ts DESC);
