-- ============================================================================
-- 0116  Time-series indexes for the high-volume event tables.
--
-- FINDING (2026-08-04 production-readiness audit, measured against RDS):
--
--     GET /api/kpi   ->  200 in 81.2s
--
--   Isolated to ONE view: mart.v_gate_dwell took 58.8s of it. That view is
--
--       SELECT ... FROM core.truck_telemetry WHERE ts > now() - interval '6 hours'
--
--   against a table of 101,482,848 rows / 9,038 MB carrying ZERO indexes — not
--   even a primary key. Every call sequential-scanned 9 GB to return 25 rows.
--
--   Root cause: the legacy schema ran on TimescaleDB, where these were
--   hypertables and time filtering was served by chunk exclusion. The v3 target
--   (`jnpa_schema_v3` on plain RDS PostgreSQL — `pg_extension` lists only
--   plpgsql) has no TimescaleDB, so the migration copied the DATA but the time
--   partitioning had no equivalent and no B-tree replaced it.
--
--   Affected tables, all filtered/ordered by `ts` by the mart views and the
--   /api/kpi, /api/geo, /api/reports and /api/debug read paths:
--
--       core.truck_telemetry   101,482,848 rows   9,038 MB   0 indexes
--       core.rfid_read          30,318,072 rows   2,217 MB   0 indexes
--       core.anpr_read              96,336 rows   7,280 kB   0 indexes
--       core.gate_event            131,910 rows      20 MB   no ts index
--
-- SAFETY
--   * Purely ADDITIVE. Creates indexes only. No table, column, constraint, view
--     or row is altered or dropped; no data is rewritten.
--   * IDEMPOTENT. Every statement is CREATE INDEX IF NOT EXISTS, so re-running
--     the migration is a no-op.
--   * NOT wrapped in BEGIN/COMMIT, unlike the other v3 migrations: this file
--     uses CREATE INDEX CONCURRENTLY, which PostgreSQL forbids inside a
--     transaction block. CONCURRENTLY is deliberate — a plain CREATE INDEX takes
--     an ACCESS EXCLUSIVE lock and would block every reader and writer of
--     truck_telemetry for the duration of a 9 GB build. The runner
--     (scripts/migrate.py) knows to run this file in autocommit.
--
-- COST
--   Expect ~10-20 minutes and ~2-3 GB of additional storage for the
--   truck_telemetry and rfid_read indexes. Run it BEFORE demo day, not during.
--   CONCURRENTLY keeps the tables readable and writable throughout.
--
-- RECOVERY
--   A CONCURRENTLY build that is interrupted leaves an INVALID index behind. It
--   is inert (the planner ignores it) but wastes space. Find and drop:
--       SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
--       DROP INDEX CONCURRENTLY <name>;
--   ...then re-run this migration.
--
-- VERIFY (after applying)
--   \timing on
--   SELECT count(*) FROM mart.v_gate_dwell;     -- expect << 1s (was 58.8s)
--   EXPLAIN (ANALYZE, BUFFERS)
--     SELECT * FROM core.truck_telemetry WHERE ts > now() - interval '6 hours';
--     -- expect "Index Scan using idx_truck_telemetry_ts", not "Seq Scan"
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. core.truck_telemetry — the 58.8s offender (mart.v_gate_dwell).
--    DESC matches the views' ORDER BY ... DESC and the `ts > now() - interval`
--    range filter equally well; a DESC B-tree serves both directions, but
--    declaring it DESC lets the planner satisfy the ordering without a sort.
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_truck_telemetry_ts
    ON core.truck_telemetry (ts DESC);

-- COVERING index for mart.v_gate_dwell specifically.
--
-- MEASURED: the plain (ts DESC) index above took /api/kpi from 81s to ~27s — the
-- planner switched from Seq Scan to Index Scan and the range lookup itself
-- dropped to ~1.1s. The remaining 26s is the AGGREGATION: a 6-hour window holds
-- ~7.34M telemetry rows, and every one of them had to be fetched from the heap
-- just to read `speed_kmh` for the `count(*) FILTER (WHERE speed_kmh <= 3)`.
--
-- INCLUDE (speed_kmh) puts that column in the index leaf, so the aggregate is
-- served by an INDEX-ONLY scan and never touches the 9 GB heap.
--
-- Ordinary (ts, speed_kmh) would work too, but INCLUDE keeps speed_kmh out of
-- the B-tree's ordering key: it is payload, never a search or sort term, so the
-- index stays smaller and cheaper to maintain on insert.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_truck_telemetry_ts_speed
    ON core.truck_telemetry (ts DESC) INCLUDE (speed_kmh);

-- Extended statistics on the 15-minute bucket EXPRESSION.
--
-- With the covering index in place the scan was fast but the plan was still
-- wrong: PostgreSQL has no statistics for an expression it has never seen, so it
-- estimated 7,369,277 groups for what is actually 25. On that estimate it chose
-- a GroupAggregate that sorted 7.34M rows and spilled ~39,000 blocks to temp
-- (measured: 190ms temp read + 721ms temp write).
--
-- Teaching the planner the real cardinality lets it pick a parallel
-- Finalize GroupAggregate with no spill at all.
--
-- MEASURED on RDS, mart.v_gate_dwell:
--     no index                    58.8s
--     + idx_truck_telemetry_ts    27.2s   (Index Scan, still heap-fetching)
--     + INCLUDE (speed_kmh)        5.8s   (index-only, but spilling to temp)
--     + these statistics           3.5s   (no spill)   <- 17x total
CREATE STATISTICS IF NOT EXISTS core.stat_truck_telemetry_bucket
    ON (to_timestamp(floor(extract(epoch from ts) / 900) * 900))
    FROM core.truck_telemetry;

-- ----------------------------------------------------------------------------
-- 2. core.rfid_read — 30M rows, 0 indexes. Feeds the RFID correlator and the
--    tag-history reports.
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rfid_read_ts
    ON core.rfid_read (ts DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rfid_read_tag_ts
    ON core.rfid_read (tag_id, ts DESC);

-- ----------------------------------------------------------------------------
-- 3. core.anpr_read — feeds mart.v_anpr_hourly and the plate-search reports.
--    Small today (96k rows) but grows with every camera minute.
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_anpr_read_ts
    ON core.anpr_read (ts DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_anpr_read_plate_ts
    ON core.anpr_read (plate, ts DESC);

-- ----------------------------------------------------------------------------
-- 4. core.gate_event — already has PK + container + job indexes, but NOT the
--    (event_type, ts) / ts pair that mart.v_gate_trip_timeline and the three
--    Appendix-C gate KPI views filter on. Fast today at 131k rows; this keeps it
--    fast as gate traffic accumulates. Mirrors the DDL declared in
--    gateway/routers/kpi.py::_GATE_KPI_DDL, which is inert at runtime
--    (JNPA_RUNTIME_DDL is off — schema is migration-owned).
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gate_event_ts
    ON core.gate_event (ts DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gate_event_type_ts
    ON core.gate_event (event_type, ts DESC);

-- ----------------------------------------------------------------------------
-- 5. core.geofence_event / core.digital_twin_event — replayed by the alerts and
--    evidence surfaces, both time-ordered, both PK-only today.
--    NOTE: these two tables timestamp with `created_at`, NOT `ts` (unlike the
--    telemetry tables above). Verified against the live schema — do not
--    "normalise" these to ts.
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_geofence_event_created_at
    ON core.geofence_event (created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_digital_twin_event_created_at
    ON core.digital_twin_event (created_at DESC);

-- ----------------------------------------------------------------------------
-- 6. LAST, and deliberately so: the largest build in this migration.
--
--    (device_id, ts) on 101M rows carries a text column in the key, so it is
--    roughly twice the size of the ts-only index and takes correspondingly
--    longer. Ordering it last means a dropped connection costs only THIS index —
--    everything cheaper is already committed, and the next `make migrate` skips
--    what succeeded (IF NOT EXISTS) and resumes here.
--
--    OPERATIONAL NOTE: this build repeatedly failed with
--    "SSL SYSCALL error: EOF detected" when run from a laptop over the public
--    internet, even with libpq keepalives — a >10 min silent socket is simply
--    not survivable across consumer NAT. RUN IT FROM THE EC2 HOST (same VPC),
--    or inside a `screen`/`tmux` session on that host:
--
--        ssh <ec2-host> 'cd /opt/jnpa-uc3 && make migrate'
--
--    Everything above it is already applied and the KPI fix does not depend on
--    this index — it serves per-device history ("where has TRK-000026 been?"),
--    not /api/kpi.
-- ----------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_truck_telemetry_device_ts
    ON core.truck_telemetry (device_id, ts DESC);
