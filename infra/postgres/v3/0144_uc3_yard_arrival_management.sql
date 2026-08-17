-- ============================================================
-- 0144  UC-3 · Peak yard utilisation and truck-arrival management
--
-- WHAT PROBLEM THIS SOLVES
-- ------------------------
-- UC-3 already detects CORRIDOR congestion (services/congestion_alert.py) and
-- can re-route a truck between gates. It had no notion of the other constraint
-- that actually stops trucks at JNPA: the YARD being full. When the yard has no
-- ground slots left, sending more trucks to the gate does not move a single
-- container — it converts a yard problem into a gate-approach jam.
--
-- Three additive tables, no existing table/column/row touched:
--
--   core.yard_capacity_state   the live occupancy of each terminal yard. This
--                              stands in for the TOS ground-slot feed, which
--                              this deployment does not receive. Every change
--                              goes through the service and is audited below —
--                              the number is never edited from the UI directly.
--
--   core.yard_capacity_event   append-only audit of every occupancy change:
--                              who, why, before/after, and the utilisation the
--                              change produced. This is the evidence trail an
--                              auditor reads to answer "why did the port hold
--                              those trucks at 14:05?".
--
--   core.truck_arrival_hold    one row per truck whose arrival was managed
--                              (held at an authorised parking facility) plus
--                              core.truck_arrival_hold_event, its own audit
--                              trail (HELD / NOTIFIED / RELEASED / ...).
--
-- WHERE CAPACITY COMES FROM (honesty rule)
-- ----------------------------------------
-- ``capacity_slots`` here is a DECLARED figure, and every payload that uses it
-- says so (``capacity_source = "core.yard_capacity_state"`` +
-- ``declared: true``). When core.yard_block (migration 0130 — the yard capacity
-- master) carries rows for the same terminal, the service prefers that SUM and
-- reports ``capacity_source = "core.yard_block"`` instead. This migration seeds
-- NOTHING into core.yard_block: fabricating a block layout would make an
-- assumed denominator look measured, which is exactly the failure 0130 exists
-- to prevent.
--
-- Additive: new tables only.
-- Rollback:
--   DROP TABLE IF EXISTS core.truck_arrival_hold_event;
--   DROP TABLE IF EXISTS core.truck_arrival_hold;
--   DROP TABLE IF EXISTS core.yard_capacity_event;
--   DROP TABLE IF EXISTS core.yard_capacity_state;
-- ============================================================
BEGIN;

-- ---------------------------------------------------------------- yard state
CREATE TABLE IF NOT EXISTS core.yard_capacity_state (
    yard_id                text PRIMARY KEY,
    terminal_code          text        NOT NULL,
    name                   text        NOT NULL,
    -- Declared ground-slot capacity. Superseded by SUM(core.yard_block) when
    -- that master carries rows for this terminal (see the header).
    capacity_slots         integer     NOT NULL CHECK (capacity_slots > 0),
    occupied_slots         integer     NOT NULL DEFAULT 0 CHECK (occupied_slots >= 0),
    -- Per-yard threshold overrides. NULL -> the gateway's env-configured
    -- defaults (YARD_HIGH_UTILIZATION_PCT / YARD_CRITICAL_UTILIZATION_PCT).
    high_threshold_pct     numeric(5,2) CHECK (high_threshold_pct IS NULL
                                    OR (high_threshold_pct > 0 AND high_threshold_pct <= 100)),
    critical_threshold_pct numeric(5,2) CHECK (critical_threshold_pct IS NULL
                                    OR (critical_threshold_pct > 0 AND critical_threshold_pct <= 100)),
    -- Where the occupancy figure came from: DECLARED_SEED on first load,
    -- DEMO_CONTROL after an operator/demo adjustment, TOS_FEED if a real
    -- terminal-operating-system feed is ever wired in.
    source                 text        NOT NULL DEFAULT 'DECLARED_SEED',
    source_note            text,
    active                 boolean     NOT NULL DEFAULT true,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_yard_capacity_occupied_le_capacity
        CHECK (occupied_slots <= capacity_slots)
);

CREATE INDEX IF NOT EXISTS idx_yard_capacity_terminal
    ON core.yard_capacity_state (terminal_code) WHERE active IS TRUE;

DROP TRIGGER IF EXISTS trg_yard_capacity_state_updated_at ON core.yard_capacity_state;
CREATE TRIGGER trg_yard_capacity_state_updated_at
    BEFORE UPDATE ON core.yard_capacity_state
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

COMMENT ON TABLE core.yard_capacity_state IS
    'Live ground-slot occupancy per terminal yard (UC-3 arrival management). '
    'capacity_slots is a DECLARED figure; core.yard_block supersedes it when populated.';

-- ---------------------------------------------------------------- yard audit
CREATE TABLE IF NOT EXISTS core.yard_capacity_event (
    id               bigserial PRIMARY KEY,
    yard_id          text        NOT NULL,
    event_type       text        NOT NULL
                     CHECK (event_type IN ('SEED','INCREASE','RELEASE','SET','SYNC')),
    delta_slots      integer     NOT NULL DEFAULT 0,
    occupied_before  integer     NOT NULL,
    occupied_after   integer     NOT NULL,
    capacity_slots   integer     NOT NULL,
    utilization_pct  numeric(5,2) NOT NULL,
    status           text        NOT NULL,
    reason           text,
    actor            text,
    detail           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_yard_capacity_event_yard
    ON core.yard_capacity_event (yard_id, created_at DESC);

COMMENT ON TABLE core.yard_capacity_event IS
    'Append-only audit of every yard-occupancy change (UC-3). One row per '
    'INCREASE / RELEASE / SET, carrying before, after and the resulting status.';

-- ------------------------------------------------------------ arrival holds
CREATE TABLE IF NOT EXISTS core.truck_arrival_hold (
    id                        bigserial PRIMARY KEY,
    device_id                 text        NOT NULL,
    plate                     text,
    driver_id                 text,
    driver_name               text,
    -- 'truck-sim' | 'pwa-registered' — the SAME provenance strings the fleet
    -- list already stamps (gateway/routers/trucks.py), so the console can never
    -- present a simulator truck as an enrolled driver's vehicle.
    source                    text        NOT NULL DEFAULT 'truck-sim',
    gate_id                   text,
    eta_s                     double precision,
    yard_id                   text        NOT NULL,
    yard_utilization_pct      numeric(5,2),
    status                    text        NOT NULL DEFAULT 'HOLD_AT_PARKING'
                              CHECK (status IN ('HOLD_AT_PARKING','RELEASED','CANCELLED')),
    reason                    text        NOT NULL,
    recommended_facility_id   text,
    recommended_facility_name text,
    facility_available        integer,
    facility_lat              double precision,
    facility_lon              double precision,
    estimated_wait_min        integer,
    -- core.alert.id of the TRAFFIC_CONGESTION alert this hold was raised under.
    alert_id                  text,
    notified                  boolean     NOT NULL DEFAULT false,
    release_notified          boolean     NOT NULL DEFAULT false,
    detail                    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    held_at                   timestamptz NOT NULL DEFAULT now(),
    released_at               timestamptz,
    updated_at                timestamptz NOT NULL DEFAULT now()
);

-- One ACTIVE hold per device: re-running the evaluation must top up the set of
-- held trucks, never duplicate a truck that is already waiting.
CREATE UNIQUE INDEX IF NOT EXISTS uq_truck_arrival_hold_active
    ON core.truck_arrival_hold (device_id) WHERE status = 'HOLD_AT_PARKING';
CREATE INDEX IF NOT EXISTS idx_truck_arrival_hold_yard
    ON core.truck_arrival_hold (yard_id, status, held_at DESC);
CREATE INDEX IF NOT EXISTS idx_truck_arrival_hold_device
    ON core.truck_arrival_hold (device_id, held_at DESC);

DROP TRIGGER IF EXISTS trg_truck_arrival_hold_updated_at ON core.truck_arrival_hold;
CREATE TRIGGER trg_truck_arrival_hold_updated_at
    BEFORE UPDATE ON core.truck_arrival_hold
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

COMMENT ON TABLE core.truck_arrival_hold IS
    'One row per truck whose arrival was managed because the yard had '
    'insufficient capacity (UC-3). Carries the parking recommendation actually '
    'given to the driver and the alert it was raised under.';

CREATE TABLE IF NOT EXISTS core.truck_arrival_hold_event (
    id         bigserial PRIMARY KEY,
    hold_id    bigint      NOT NULL REFERENCES core.truck_arrival_hold(id) ON DELETE CASCADE,
    device_id  text        NOT NULL,
    action     text        NOT NULL
               CHECK (action IN ('HELD','NOTIFIED','NOTIFY_FAILED','RELEASED',
                                 'RELEASE_NOTIFIED','RELEASE_NOTIFY_FAILED','CANCELLED')),
    actor      text,
    detail     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_truck_arrival_hold_event_hold
    ON core.truck_arrival_hold_event (hold_id, created_at);

COMMENT ON TABLE core.truck_arrival_hold_event IS
    'Audit trail per arrival hold: HELD -> NOTIFIED -> RELEASED -> '
    'RELEASE_NOTIFIED, each with the delivery result in detail.';

-- ---------------------------------------------------------------- seed yards
-- The four JNPA container terminals. capacity_slots and the opening occupancy
-- are DECLARED demo figures (source_note says so on every row) because no
-- ground-slot feed reaches this deployment; they are the starting point the
-- demo raises and releases from, not a measurement of the real port.
INSERT INTO core.yard_capacity_state
    (yard_id, terminal_code, name, capacity_slots, occupied_slots, source, source_note)
VALUES
    ('JNPA-NSICT-YARD', 'NSICT', 'NSICT container yard', 4800, 3360,
     'DECLARED_SEED',
     'Declared ground-slot capacity and opening occupancy (~70%). No TOS '
     'ground-slot feed reaches this deployment; supersede with core.yard_block '
     'or a TOS sync when one is available.'),
    ('JNPA-JNPCT-YARD', 'JNPCT', 'JNPCT container yard', 7200, 4680,
     'DECLARED_SEED',
     'Declared ground-slot capacity and opening occupancy (~65%).'),
    ('JNPA-NSIGT-YARD', 'NSIGT', 'NSIGT container yard', 6000, 3900,
     'DECLARED_SEED',
     'Declared ground-slot capacity and opening occupancy (~65%).'),
    ('JNPA-BMCT-YARD',  'BMCT',  'BMCT container yard', 10800, 6480,
     'DECLARED_SEED',
     'Declared ground-slot capacity and opening occupancy (~60%).')
ON CONFLICT (yard_id) DO NOTHING;

-- The seed itself is auditable: one SEED event per yard, once.
INSERT INTO core.yard_capacity_event
    (yard_id, event_type, delta_slots, occupied_before, occupied_after,
     capacity_slots, utilization_pct, status, reason, actor, detail)
SELECT s.yard_id, 'SEED', s.occupied_slots, 0, s.occupied_slots, s.capacity_slots,
       round(100.0 * s.occupied_slots / s.capacity_slots, 2),
       'NORMAL', 'migration 0144 declared seed', 'migration:0144',
       jsonb_build_object('source_note', s.source_note)
FROM core.yard_capacity_state s
WHERE NOT EXISTS (
    SELECT 1 FROM core.yard_capacity_event e
    WHERE e.yard_id = s.yard_id AND e.event_type = 'SEED'
);

COMMIT;
