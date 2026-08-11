-- 0136 — UC3-021 / UC3-027 / UC3-030 / UC3-040: gate & lane board, CPP metered
-- release, e-Challan simulation badge, Auto-LEO weighbridge reroute.
--
-- Four ticket-scoped additions, one file, all ADDITIVE and idempotent. Nothing
-- existing is dropped, rewritten or back-filled with invented values.
--
-- 1. UC3-021 Gate & Lane Board
--    core.gate and core.camera exist but are EMPTY, so GET /api/gates returned
--    []. They are seeded here with the four real JNPA terminal gates and their
--    ANPR camera pair. core.gate_lane is new: the per-lane state the board
--    renders (type, open/closed, boom-barrier position).
--
--    The board's headline number is QUEUE LENGTH, and the spec test (UI-068) is
--    that stopping a gate must make the queue RISE while throughput reads zero.
--    That is only true if the queue is COUNTED FROM VIDEO, never divided out of
--    throughput. core.camera_ai_count.queue_count is already that counted
--    figure; this migration adds the constraint that makes the rule structural
--    rather than a convention: a lane-board queue reading may only be sourced
--    from a counting method in core.queue_count_method, and 'THROUGHPUT_DERIVED'
--    is deliberately NOT in that table. A future writer that tries to infer the
--    queue from throughput fails the FK instead of silently shipping a wrong
--    number to an evaluator.
--
--    core.lane_reassignment_task is the UI-103 guarantee: applying a
--    reassignment creates a task FOR A HUMAN and never commands gate equipment.
--    dispatched_to_equipment is CHECK-pinned to false, so the database itself
--    refuses to record an equipment command from this table. The column exists
--    precisely so the guarantee is visible and enforced, not merely documented.
--
-- 2. UC3-027 CPP metered release (flow F-06)
--    core.cpp_release_plan holds one row per terminal per recompute: the gate
--    queue it read, the clearing rate, the release rate it derived and the
--    driver advice sentence generated from those same numbers. Storing the
--    inputs beside the output is what lets an evaluator check that only the
--    congested terminal slowed, and lets the UNIFORM comparison (UI-111) be
--    replayed rather than asserted. Every row is simulated = true: no CPP
--    occupancy sensor feed exists in the corpus (declared post-award).
--
-- 3. UC3-030 e-Challan SIMULATED badge
--    core.challan gains issuance_mode. A5 says enforcement authority rests with
--    JNPA/RTO, so no challan this system mints is legally issued. The column
--    defaults to 'SIMULATED' and is CHECK-pinned to it: 'ISSUED' cannot be
--    written until a migration adds it, which is exactly the post-award config
--    change. A challan row therefore cannot exist without carrying its own
--    disclosure, so no screen or PDF can render one unbadged.
--
-- 4. UC3-040 Auto-LEO weighbridge reroute (X4)
--    core.weighbridge_reroute records a failed weighbridge sending a truck to an
--    alternate and notifying customs — the evidence behind the WEIGHT_MISSING
--    flag, which previously had nowhere to be recorded.
BEGIN;

-- ============================================================ 1. UC3-021 ====

-- ---------------------------------------------------------------- the gates
-- The four container-terminal gates on the NH-348 corridor. Coordinates are the
-- same OSM-traced positions jnpa_shared.corridor already uses for the map, so
-- the board and the map cannot disagree about where a gate is.
INSERT INTO core.gate (id, name, lat, lon) VALUES
    ('G-NSICT', 'NSICT Gate',  18.9520, 72.9511),
    ('G-JNPCT', 'JNPCT Gate',  18.9490, 72.9479),
    ('G-NSIGT', 'NSIGT Gate',  18.9457, 72.9531),
    ('G-BMCT',  'BMCT Gate',   18.9381, 72.9388)
ON CONFLICT (id) DO NOTHING;

-- ANPR camera pair per gate. core.camera.role already has a fixed vocabulary
-- (entry/exit/overview/ptz/thermal/anpr); the IN/OUT ANPR arches map onto its
-- existing 'entry'/'exit' values rather than widening the constraint.
INSERT INTO core.camera (id, gate_id, name, lat, lon, role) VALUES
    ('CAM-G-NSICT-1', 'G-NSICT', 'NSICT ANPR IN',  18.9520, 72.9511, 'entry'),
    ('CAM-G-NSICT-2', 'G-NSICT', 'NSICT ANPR OUT', 18.9520, 72.9511, 'exit'),
    ('CAM-G-JNPCT-1', 'G-JNPCT', 'JNPCT ANPR IN',  18.9490, 72.9479, 'entry'),
    ('CAM-G-JNPCT-2', 'G-JNPCT', 'JNPCT ANPR OUT', 18.9490, 72.9479, 'exit'),
    ('CAM-G-NSIGT-1', 'G-NSIGT', 'NSIGT ANPR IN',  18.9457, 72.9531, 'entry'),
    ('CAM-G-NSIGT-2', 'G-NSIGT', 'NSIGT ANPR OUT', 18.9457, 72.9531, 'exit'),
    ('CAM-G-BMCT-1',  'G-BMCT',  'BMCT ANPR IN',   18.9381, 72.9388, 'entry'),
    ('CAM-G-BMCT-2',  'G-BMCT',  'BMCT ANPR OUT',  18.9381, 72.9388, 'exit')
ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------- how a queue may be counted
-- A whitelist, not an enum, so the permitted methods are visible as data and a
-- new method is a row rather than a type rewrite. 'THROUGHPUT_DERIVED' is
-- absent BY DESIGN — see the header note.
CREATE TABLE IF NOT EXISTS core.queue_count_method (
    method        text PRIMARY KEY,
    description   text NOT NULL,
    counts_frames boolean NOT NULL
);

INSERT INTO core.queue_count_method (method, description, counts_frames) VALUES
    ('VIDEO_ANALYTICS',
     'Vehicles counted in the queue zone of a camera frame by the detector. The '
     'only method the gate & lane board accepts (UI-068).', true),
    ('MANUAL_COUNT',
     'A marshal counted the queue by eye and entered it. Real observation, lower '
     'frequency; permitted as a fallback when a camera is down.', false)
ON CONFLICT (method) DO NOTHING;

COMMENT ON TABLE core.queue_count_method IS
    'Permitted queue-counting methods. THROUGHPUT_DERIVED is deliberately absent: '
    'a queue inferred from throughput reads zero when a gate stops, which is the '
    'exact failure UI-068 tests for. Absence from this table makes that inference '
    'unstorable rather than merely discouraged.';

-- core.camera_ai_count is the existing video-analytics sink. Tie its rows to the
-- whitelist so the board's queue provenance is checkable. Existing rows carry
-- source='CAMERA_AI'; they are mapped to VIDEO_ANALYTICS rather than deleted.
ALTER TABLE core.camera_ai_count
    ADD COLUMN IF NOT EXISTS count_method text NOT NULL DEFAULT 'VIDEO_ANALYTICS';

UPDATE core.camera_ai_count SET count_method = 'VIDEO_ANALYTICS'
 WHERE count_method IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'camera_ai_count_method_fk') THEN
        ALTER TABLE core.camera_ai_count
            ADD CONSTRAINT camera_ai_count_method_fk
            FOREIGN KEY (count_method) REFERENCES core.queue_count_method (method);
    END IF;
END $$;

COMMENT ON COLUMN core.camera_ai_count.queue_count IS
    'Vehicles COUNTED in the queue zone of the frame. Never derived from '
    'throughput: when a gate stops, throughput goes to 0 and this must RISE.';

-- ----------------------------------------------------------------- the lanes
CREATE TABLE IF NOT EXISTS core.gate_lane (
    lane_id        text PRIMARY KEY,
    gate_id        text NOT NULL REFERENCES core.gate (id),
    lane_no        integer NOT NULL CHECK (lane_no > 0),
    lane_type      text NOT NULL CHECK (lane_type IN ('IN', 'OUT', 'REVERSIBLE')),
    lane_state     text NOT NULL DEFAULT 'OPEN'
                        CHECK (lane_state IN ('OPEN', 'CLOSED', 'MAINTENANCE')),
    -- Reported position of the boom barrier. Read-only on the board: the twin
    -- observes it, it never drives it (UI-103).
    boom_barrier   text NOT NULL DEFAULT 'DOWN'
                        CHECK (boom_barrier IN ('UP', 'DOWN', 'UNKNOWN')),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (gate_id, lane_no)
);

COMMENT ON TABLE core.gate_lane IS
    'Per-lane state rendered by the T-02 Gate & Lane Board. boom_barrier is an '
    'OBSERVED position, never a commanded one — this PoC issues no gate-equipment '
    'commands (UI-103).';

-- 3 lanes per gate (assumptions.json gates.lanes_per_gate = 3): two directional
-- and one reversible, the standard JNPA gate layout.
INSERT INTO core.gate_lane (lane_id, gate_id, lane_no, lane_type) VALUES
    ('G-NSICT-L1', 'G-NSICT', 1, 'IN'),
    ('G-NSICT-L2', 'G-NSICT', 2, 'OUT'),
    ('G-NSICT-L3', 'G-NSICT', 3, 'REVERSIBLE'),
    ('G-JNPCT-L1', 'G-JNPCT', 1, 'IN'),
    ('G-JNPCT-L2', 'G-JNPCT', 2, 'OUT'),
    ('G-JNPCT-L3', 'G-JNPCT', 3, 'REVERSIBLE'),
    ('G-NSIGT-L1', 'G-NSIGT', 1, 'IN'),
    ('G-NSIGT-L2', 'G-NSIGT', 2, 'OUT'),
    ('G-NSIGT-L3', 'G-NSIGT', 3, 'REVERSIBLE'),
    ('G-BMCT-L1',  'G-BMCT',  1, 'IN'),
    ('G-BMCT-L2',  'G-BMCT',  2, 'OUT'),
    ('G-BMCT-L3',  'G-BMCT',  3, 'REVERSIBLE')
ON CONFLICT (lane_id) DO NOTHING;

-- ------------------------------------------- lane reassignment = human task
CREATE TABLE IF NOT EXISTS core.lane_reassignment_task (
    task_id          uuid PRIMARY KEY,
    gate_id          text NOT NULL REFERENCES core.gate (id),
    lane_id          text NOT NULL REFERENCES core.gate_lane (lane_id),
    from_lane_type   text NOT NULL,
    to_lane_type     text NOT NULL,
    reason           text,
    -- The impact simulation shown as a PREVIEW before the operator applied it,
    -- kept so the task carries the projection it was approved against.
    impact_preview   jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'PENDING'
                          CHECK (status IN ('PENDING', 'ACKNOWLEDGED', 'DONE', 'CANCELLED')),
    assigned_to      text NOT NULL DEFAULT 'GATE_SUPERVISOR',
    created_by       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    acknowledged_by  text,
    acknowledged_at  timestamptz,
    -- UI-103, enforced by the database rather than by review: this workflow
    -- cannot record an equipment command, because the only value the column
    -- accepts is false.
    dispatched_to_equipment boolean NOT NULL DEFAULT false
                          CHECK (dispatched_to_equipment = false)
);

CREATE INDEX IF NOT EXISTS idx_lane_task_gate
    ON core.lane_reassignment_task (gate_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lane_task_status
    ON core.lane_reassignment_task (status, created_at DESC);

COMMENT ON TABLE core.lane_reassignment_task IS
    'Applying a lane reassignment creates a task for a human here. It NEVER sends '
    'a command to gate equipment (UI-103) — dispatched_to_equipment is CHECK-pinned '
    'to false so that guarantee is structural.';

-- ============================================================ 2. UC3-027 ====
CREATE TABLE IF NOT EXISTS core.cpp_release_plan (
    plan_id            bigserial PRIMARY KEY,
    computed_at        timestamptz NOT NULL DEFAULT now(),
    terminal_code      text NOT NULL,
    gate_id            text,
    -- INPUTS the release rate was computed from, stored so the recomputation is
    -- auditable and the metered-vs-uniform comparison is replayable.
    gate_queue_vehicles integer NOT NULL CHECK (gate_queue_vehicles >= 0),
    clearing_rate_vph   numeric NOT NULL CHECK (clearing_rate_vph >= 0),
    -- OUTPUTS.
    release_rate_vph    numeric NOT NULL CHECK (release_rate_vph >= 0),
    hold_minutes        integer NOT NULL CHECK (hold_minutes >= 0),
    congestion_level    text NOT NULL CHECK (congestion_level IN ('LOW', 'MEDIUM', 'HIGH')),
    -- The driver-facing sentence, generated from the SAME numbers (UI-156).
    advice_text         text NOT NULL,
    -- METERED = per-terminal throttling. UNIFORM = the do-nothing comparison
    -- UI-111 asks to be demoable: same inputs, one rate for everybody.
    mode                text NOT NULL DEFAULT 'METERED'
                             CHECK (mode IN ('METERED', 'UNIFORM')),
    -- No CPP occupancy sensor feed exists in the corpus. Pinned, not defaulted.
    simulated           boolean NOT NULL DEFAULT true CHECK (simulated = true)
);

CREATE INDEX IF NOT EXISTS idx_cpp_release_terminal
    ON core.cpp_release_plan (terminal_code, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_cpp_release_computed
    ON core.cpp_release_plan (computed_at DESC);

COMMENT ON TABLE core.cpp_release_plan IS
    'One metered-release recompute per terminal (flow F-06). Inputs are stored '
    'beside outputs so an evaluator can verify that only the congested terminal '
    'slowed. simulated is CHECK-pinned true: the plaza sensor feed is a declared '
    'post-award integration.';

-- ============================================================ 3. UC3-030 ====
ALTER TABLE core.challan
    ADD COLUMN IF NOT EXISTS issuance_mode text NOT NULL DEFAULT 'SIMULATED';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'challan_issuance_mode_check') THEN
        ALTER TABLE core.challan
            ADD CONSTRAINT challan_issuance_mode_check
            CHECK (issuance_mode = 'SIMULATED');
    END IF;
END $$;

COMMENT ON COLUMN core.challan.issuance_mode IS
    'Always SIMULATED in the PoC (assumption A5: issuance authority rests with '
    'JNPA/RTO). CHECK-pinned so a challan cannot be persisted without its own '
    'disclosure — the badge on screen and in the PDF reads this column.';

-- ============================================================ 4. UC3-040 ====
CREATE TABLE IF NOT EXISTS core.weighbridge_reroute (
    reroute_id       bigserial PRIMARY KEY,
    container_no     text NOT NULL,
    vehicle_plate    text,
    failed_wb_id     text NOT NULL,
    alternate_wb_id  text,
    reason           text NOT NULL DEFAULT 'WEIGHBRIDGE_FAULT',
    customs_notified boolean NOT NULL DEFAULT false,
    notified_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- Weighbridge event feeds do not exist in the corpus (gap G8).
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated = true)
);

CREATE INDEX IF NOT EXISTS idx_wb_reroute_container
    ON core.weighbridge_reroute (container_no, created_at DESC);

COMMENT ON TABLE core.weighbridge_reroute IS
    'Edge case X4: a weighbridge fails, the truck is rerouted to an alternate and '
    'customs is notified. Backs the WEIGHT_MISSING Auto-LEO flag, which previously '
    'had no evidence record.';

COMMIT;
