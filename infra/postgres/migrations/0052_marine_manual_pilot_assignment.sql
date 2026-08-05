-- 0052_marine_manual_pilot_assignment.sql — UC-I Marine: manual pilot assignment.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0052_marine_manual_pilot_assignment.sql
--
-- WHY A NEW TABLE RATHER THAN REUSING core.pilotage
-- -------------------------------------------------
-- A vessel can complete VESPRO -> CALINF -> CALINV -> BERMAN -> BERALT and appear
-- correctly in Vessel Calls while no pilot card or pilot memo has been imported for it.
-- That is a valid operational state, not a parser fault, and an operator must be able to
-- carry the lifecycle forward.
--
-- core.pilotage was rejected as the home for that record. It is the IMPORTED ledger: its
-- rows are deduplicated on row_sha256, they are rewritten by re-import and Override
-- Import, and pilot_status.py treats every row in it as source-of-truth marine actuals.
-- Writing operator-entered rows into it would (a) mix a transaction into a document
-- table, (b) collide with the import dedup key, and (c) make manual data indistinguishable
-- from imported data at exactly the moment the two must be told apart.
--
-- This table is therefore a SEPARATE, SUBORDINATE record. Imported pilotage always wins;
-- see `active` below.
--
-- STRICTLY ADDITIVE. Creates ONE new table in `core`. Touches NOTHING existing — no
-- column is added, altered or dropped on vessel / vessel_call / vessel_call_event /
-- pilotage / pilot / port_craft, and no existing index or constraint is modified.
--
-- ROLLBACK: DROP TABLE core.manual_pilot_assignment;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.manual_pilot_assignment (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The join key to everything else. No FK: core.vessel_call rows are replaced by the
    -- import path, and a hard FK would let an import fail on an operator's record. The
    -- soft link matches how core.pilotage and core.port_craft already reference imports.
    call_id       bigint NOT NULL,
    -- Identity snapshot, denormalised on purpose: it is what the operator SAW when they
    -- assigned, and it must stay readable if the call row is later re-keyed by an import.
    vcn           text,
    via_no        text,
    imo_no        text,
    vessel_name   text,
    -- From the Pilot Register (core.pilot / the imported movements). Free text because
    -- the corpus identifies a pilot by roster code OR by acknowledged name and the two
    -- populations are disjoint — no single FK target exists.
    pilot_code    text NOT NULL,
    pilot_name    text,
    -- Assigned -> Onboard -> Released. Deliberately NOT the imported pilot_status
    -- vocabulary: this is an operator transaction, and conflating the two vocabularies is
    -- what the projection merge exists to avoid.
    status        text NOT NULL DEFAULT 'Assigned'
                  CHECK (status IN ('Assigned', 'Onboard', 'Released')),
    assigned_at   timestamptz NOT NULL DEFAULT now(),
    boarded_at    timestamptz,
    released_at   timestamptz,
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    -- IMPORTED DATA ALWAYS WINS, expressed as data rather than as application logic.
    -- Set false when a pilot memo / pilot card later lands for the same call_id. The row
    -- is never deleted, so the operator action stays auditable after it is superseded.
    active        boolean NOT NULL DEFAULT true,
    superseded_at timestamptz);

-- At most ONE live manual assignment per call. Partial, so superseded history accumulates
-- freely while a second live assignment for the same call is impossible.
CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_pilot_assignment_live
    ON core.manual_pilot_assignment (call_id) WHERE active;

-- The projection's hot path: fetch live assignments for a batch of call_ids.
CREATE INDEX IF NOT EXISTS idx_manual_pilot_assignment_call
    ON core.manual_pilot_assignment (call_id) WHERE active;

CREATE INDEX IF NOT EXISTS idx_manual_pilot_assignment_via
    ON core.manual_pilot_assignment (via_no);
