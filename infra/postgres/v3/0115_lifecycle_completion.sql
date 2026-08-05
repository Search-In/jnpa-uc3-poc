-- ============================================================================
-- 0115  Lifecycle completion: cross-twin durability + the export leg.
--
-- Closes four gaps the 2026-08-03 audit proved against this database:
--
--   1. XT-2 (jnpa.crosstwin.deferred-arrival) was applied to an IN-MEMORY slot
--      book only (gateway/tas_mock.py `_WINDOWS`), so every consumed UC-II
--      window vanished on gateway restart and nothing survived a refresh.
--      -> core.deferred_arrival_window
--   2. Re-route advisories and web check-ins lived in module-level dicts
--      (gateway/routers/trucks.py LAST_REROUTE / CHECKINS), the direct cause of
--      "loaded data disappears after refresh".
--      -> core.reroute_advisory
--   3. core.cargo.lifecycle_status stopped at RELEASED — there were NO export
--      states at all, so the export flow (booking -> Form13 -> gate-in -> VGM ->
--      LEO -> COPRAR -> vessel load) could not be represented.
--      -> widened CHECK + core.cargo.direction + core.export_booking
--   4. 5 rows had is_released = true while lifecycle_status = 'CREATED'
--      (POST /api/cargo accepted is_released at create, bypassing the gate).
--      -> backfilled below; the service now refuses the same input.
--
-- Everything is additive. The one CHECK constraint that is replaced is WIDENED
-- (every previously-legal value stays legal), so no existing row can be
-- invalidated and no column or table is dropped.
-- ============================================================================
BEGIN;

-- ------------------------------------------------- 1. cross-twin durability
-- One row per DeferredArrivalWindow consumed from UC-II. `correlation_id` is the
-- idempotency key: re-consuming the same window (redelivery, consumer restart,
-- a re-run of UC-II scenario S2) updates in place instead of double-counting.
CREATE TABLE IF NOT EXISTS core.deferred_arrival_window (
    correlation_id  text PRIMARY KEY,
    gate_id         text,
    window_start    timestamptz NOT NULL,
    window_end      timestamptz NOT NULL,
    window_min      integer     NOT NULL,
    slot_cap        integer     NOT NULL,
    booked          integer     NOT NULL DEFAULT 0,
    applied_slots   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    source          text,
    transport       text        NOT NULL DEFAULT 'KAFKA'
                    CHECK (transport IN ('KAFKA','HTTP')),
    received_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_deferred_window_recv
    ON core.deferred_arrival_window (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_deferred_window_gate
    ON core.deferred_arrival_window (gate_id, window_start DESC);

-- Durable mirror of the last re-route advisory per device. The PWA polling
-- fallback (GET /api/trucks/{id}/route/latest) reads this back, so an advisory
-- now survives a gateway restart and a browser refresh.
CREATE TABLE IF NOT EXISTS core.reroute_advisory (
    device_id     text PRIMARY KEY,
    plate         text,
    from_gate     text,
    to_gate       text,
    reason        text,
    advisory      jsonb       NOT NULL,
    ack_state     text        CHECK (ack_state IS NULL OR
                                     ack_state IN ('ACK','DECLINE')),
    acked_at      timestamptz,
    dispatched_at timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reroute_dispatched
    ON core.reroute_advisory (dispatched_at DESC);

-- ------------------------------------------------------- 2. cargo direction
-- Import and export boxes were indistinguishable. NULL-safe default keeps every
-- existing row valid; the IGM materialiser stamps IMPORT, export booking stamps
-- EXPORT.
ALTER TABLE core.cargo ADD COLUMN IF NOT EXISTS direction text;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cargo_direction_check') THEN
        ALTER TABLE core.cargo ADD CONSTRAINT cargo_direction_check
            CHECK (direction IS NULL OR direction IN ('IMPORT','EXPORT','TRANSHIPMENT'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_cargo_direction ON core.cargo (direction, lifecycle_status);

-- Provenance for containers materialised from a customs manifest, so the bridge
-- is idempotent and the UI can show "where did this row come from".
ALTER TABLE core.cargo ADD COLUMN IF NOT EXISTS source_igm_no text;
CREATE INDEX IF NOT EXISTS idx_cargo_source_igm ON core.cargo (source_igm_no)
    WHERE source_igm_no IS NOT NULL;

-- ------------------------------------------------ 3. export lifecycle states
-- Widen the lifecycle CHECK: all nine import states are preserved verbatim and
-- seven export states are appended. Dropping + re-adding is the only way to
-- widen a CHECK in Postgres; every pre-existing value remains legal.
ALTER TABLE core.cargo DROP CONSTRAINT IF EXISTS cargo_lifecycle_status_check;
ALTER TABLE core.cargo ADD CONSTRAINT cargo_lifecycle_status_check
    CHECK (lifecycle_status IN (
        -- import leg (unchanged)
        'CREATED','VESSEL_DISCHARGED','YARD_ASSIGNED','YARD_POSITION_ALLOCATED',
        'REEFER_PLANNED','RAKE_ASSIGNED','SCAN_PENDING','VERIFIED','RELEASED',
        -- export leg (new)
        'EXPORT_BOOKED','FORM13_ISSUED','EXPORT_GATE_IN','VGM_CAPTURED',
        'LEO_GRANTED','LOAD_LISTED','VESSEL_LOADED'
    ));

-- The export spine. One row per export container booking; the lifecycle it
-- drives lives on core.cargo (single source of truth) — this table holds the
-- documentary facts each export step contributes.
CREATE TABLE IF NOT EXISTS core.export_booking (
    id                bigserial PRIMARY KEY,
    booking_no        text NOT NULL,
    container_number  text,
    shipping_line     text,
    vessel_name       text,
    voyage_no         text,
    via_no            text,                     -- terminal visit / VIA (S0633)
    pod               text,                     -- port of discharge (UNLOCODE)
    terminal          text,
    cfs_code          text,
    -- Form 13 (export gate pass)
    form13_no         text,
    form13_issued_at  timestamptz,
    -- gate-in
    gate_in_at        timestamptz,
    gate_in_gate      text,
    truck_no          text,
    job_id            bigint REFERENCES core.container_job_assignment(id) ON DELETE SET NULL,
    -- VGM (SOLAS verified gross mass)
    vgm_kg            numeric(12,2),
    vgm_method        text CHECK (vgm_method IS NULL OR vgm_method IN ('METHOD_1','METHOD_2')),
    vgm_captured_at   timestamptz,
    declared_gross_kg numeric(12,2),
    vgm_variance_pct  numeric(6,3),
    -- customs export
    shipping_bill_no  text,
    leo_no            text,
    leo_granted_at    timestamptz,
    -- load list / load confirmation
    coprar_ref        text,
    load_listed_at    timestamptz,
    loaded_at         timestamptz,
    stowage_position  text,
    status            text NOT NULL DEFAULT 'BOOKED'
                      CHECK (status IN ('BOOKED','FORM13_ISSUED','GATE_IN','VGM_CAPTURED',
                                        'LEO_GRANTED','LOAD_LISTED','LOADED','CANCELLED')),
    created_by        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- One OPEN booking per container at a time; cancelled/loaded rows may repeat so a
-- box can be re-exported on a later voyage.
CREATE UNIQUE INDEX IF NOT EXISTS uq_export_booking_no ON core.export_booking (booking_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_export_booking_open
    ON core.export_booking (container_number)
    WHERE container_number IS NOT NULL AND status NOT IN ('LOADED','CANCELLED');
CREATE INDEX IF NOT EXISTS idx_export_booking_container
    ON core.export_booking (container_number, id DESC);
CREATE INDEX IF NOT EXISTS idx_export_booking_status ON core.export_booking (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_export_booking_vessel ON core.export_booking (via_no, vessel_name);

-- Append-only step history (mirrors core.container_job_event's discipline).
CREATE TABLE IF NOT EXISTS core.export_booking_event (
    id         bigserial PRIMARY KEY,
    booking_id bigint NOT NULL REFERENCES core.export_booking(id) ON DELETE CASCADE,
    event      text   NOT NULL,
    old_status text,
    new_status text,
    detail     jsonb,
    actor      text,
    actor_role text,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_export_event_booking ON core.export_booking_event (booking_id, id);

-- --------------------------------------------- 4. lifecycle consistency fix
-- Rows released before the VERIFY gate existed still claim lifecycle 'CREATED'.
-- Promote them to RELEASED so `?status=RELEASED` (the UC-III handover query) and
-- `is_released` can never disagree again. Only touches rows that are already
-- flagged released — no row changes its release state.
UPDATE core.cargo
   SET lifecycle_status = 'RELEASED'
 WHERE is_released IS TRUE
   AND lifecycle_status <> 'RELEASED';

-- Stamp direction on what we can infer today: anything with an export booking is
-- EXPORT, everything else already in cargo came off the import path.
UPDATE core.cargo SET direction = 'IMPORT' WHERE direction IS NULL;

COMMIT;
