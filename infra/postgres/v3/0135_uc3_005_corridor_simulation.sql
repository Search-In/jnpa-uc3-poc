-- 0135 — UC3-005: frozen 20k-truck NH-348 corridor simulation.
--
-- There is no real per-truck GPS for the demo window, so corridor traffic at
-- 20k scale has to be generated. Generated traffic that cannot be told apart
-- from measured traffic is the failure mode this schema exists to prevent, so:
--
--   * every simulated row carries simulated = true, NOT NULL, DEFAULT true, and
--     a CHECK that pins it to true — a real observation physically cannot be
--     stored in these tables;
--   * the tables are NEW and separate. Nothing is written into core.gate_document,
--     core.container_event, core.transporter or any other measured store, so the
--     simulation can never contaminate UC3-001/002/003 data;
--   * the run is reproducible: the seed, its version and a SHA-256 over the whole
--     config are stored with the run, so a re-run can be proved identical and a
--     silent reseed after rehearsal is detectable by comparing the hash.
--
-- Calibration is recorded, not assumed: the anchor day's published gate moves
-- (IN / OUT / total TEU) are stored on the run so the generated volumes can be
-- checked against the real figure they were scaled from.
--
-- Fully ADDITIVE and idempotent. No existing table is touched.
BEGIN;

-- ------------------------------------------------------------ 1. the run
CREATE TABLE IF NOT EXISTS core.sim_run (
    run_id            text PRIMARY KEY,
    corridor          text        NOT NULL,
    seed              text        NOT NULL,
    seed_version      text        NOT NULL,
    config_sha256     char(64)    NOT NULL,
    truck_count       integer     NOT NULL CHECK (truck_count >= 0),
    segment_count     integer     NOT NULL CHECK (segment_count >= 0),
    calibration_from  date        NOT NULL,
    calibration_to    date        NOT NULL,
    anchor_date       date        NOT NULL,
    anchor_in_teu     integer     NOT NULL CHECK (anchor_in_teu  >= 0),
    anchor_out_teu    integer     NOT NULL CHECK (anchor_out_teu >= 0),
    anchor_total_teu  integer     NOT NULL CHECK (anchor_total_teu >= 0),
    calibration_note  text,
    frozen_at         timestamptz NOT NULL DEFAULT now(),
    simulated         boolean     NOT NULL DEFAULT true
                                  CHECK (simulated = true)
);

COMMENT ON TABLE core.sim_run IS
    'One frozen corridor-simulation run. config_sha256 is the reproducibility '
    'proof: same seed + same config => same hash => same trucks. A changed hash '
    'after rehearsal means the simulation was reseeded.';
COMMENT ON COLUMN core.sim_run.anchor_total_teu IS
    'Published real gate moves for anchor_date, the figure the generated volume '
    'is calibrated against. Recorded so the calibration can be re-checked.';

-- ------------------------------------------------------------ 2. the trucks
CREATE TABLE IF NOT EXISTS core.sim_truck (
    run_id        text        NOT NULL REFERENCES core.sim_run(run_id) ON DELETE CASCADE,
    truck_uid     text        NOT NULL,
    truck_no      text        NOT NULL,
    segment_code  text        NOT NULL,
    direction     text        NOT NULL CHECK (direction IN ('IN', 'OUT')),
    state         text        NOT NULL,
    replay_ts     timestamptz NOT NULL,
    simulated     boolean     NOT NULL DEFAULT true CHECK (simulated = true),
    provenance    text        NOT NULL DEFAULT 'SIMULATED'
                              CHECK (provenance = 'SIMULATED'),
    PRIMARY KEY (run_id, truck_uid)
);

COMMENT ON TABLE core.sim_truck IS
    'Generated corridor trucks for a sim_run. simulated/provenance are pinned by '
    'CHECK so nothing measured can be stored here and nothing here can be '
    'relabelled as real. Re-seeding upserts on (run_id, truck_uid).';

CREATE INDEX IF NOT EXISTS idx_sim_truck_segment   ON core.sim_truck (run_id, segment_code);
CREATE INDEX IF NOT EXISTS idx_sim_truck_direction ON core.sim_truck (run_id, direction);
CREATE INDEX IF NOT EXISTS idx_sim_truck_replay    ON core.sim_truck (run_id, replay_ts);

COMMIT;
