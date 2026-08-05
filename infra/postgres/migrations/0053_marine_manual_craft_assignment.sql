-- 0053_marine_manual_craft_assignment.sql — UC-I Marine: manual craft assignment.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0053_marine_manual_craft_assignment.sql
--
-- The craft counterpart to 0052. A movement that needs tugs, a pilot launch or a mooring
-- gang has no imported source in this corpus — Details_of_Port_Crafts.pdf gives the FLEET
-- (core.port_craft) but no per-call roster — so committing craft to a movement is an
-- operator action, and until now it lived only in browser state and was invisible to
-- every other module.
--
-- WHY NOT REUSE core.port_craft
-- -----------------------------
-- That table is the fleet REGISTER: one row per vessel the port owns or hires, with its
-- particulars. An assignment is a many-to-many fact over time between a craft and a call.
-- Putting it on the register would destroy the register's grain and make a craft's
-- history unrepresentable.
--
-- STRICTLY ADDITIVE. Creates ONE new table in `core`. No existing column, index or
-- constraint is altered — port_craft, pilotage, vessel_call and vessel_call_event are
-- untouched, so the imported fleet and pilot-memo workflows are unaffected.
--
-- ROLLBACK: DROP TABLE core.manual_craft_assignment;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.manual_craft_assignment (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Soft link, matching manual_pilot_assignment: a hard FK would let an import fail on
    -- an operator's record when a call row is replaced.
    call_id       bigint NOT NULL,
    -- Identity snapshot — what the operator SAW when they committed the craft.
    vcn           text,
    via_no        text,
    vessel_name   text,
    -- The craft, from core.port_craft. Denormalised name/type for the same reason.
    craft_id      smallint NOT NULL,
    craft_name    text,
    craft_type    text,
    -- Assigned -> Dispatched -> On Scene -> Assisting -> Released. The full dispatch
    -- ladder is modelled now even though the UI drives only the ends of it, so adding the
    -- middle later is a UI change rather than a migration.
    status        text NOT NULL DEFAULT 'Assigned'
                  CHECK (status IN ('Assigned', 'Dispatched', 'On Scene',
                                    'Assisting', 'Released')),
    assigned_at   timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    arrived_at    timestamptz,
    assisting_at  timestamptz,
    released_at   timestamptz,
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    -- Imported craft data would win the same way pilot memos do. No such feed exists
    -- today, so nothing sets this false yet — the column is here so precedence has a
    -- home when one arrives, not as speculation about its shape.
    active        boolean NOT NULL DEFAULT true,
    superseded_at timestamptz);

-- One craft cannot be committed to two movements at once. Partial, so released and
-- superseded history accumulates freely.
CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_craft_assignment_live
    ON core.manual_craft_assignment (craft_id)
    WHERE active AND status <> 'Released';

-- The projection's hot path: live assignments for a batch of calls.
CREATE INDEX IF NOT EXISTS idx_manual_craft_assignment_call
    ON core.manual_craft_assignment (call_id) WHERE active;
