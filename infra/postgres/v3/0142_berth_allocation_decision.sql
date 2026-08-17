-- 0142  Berth allocation decision log.  GAP-FLOW-15 / flow F-15
--
-- The UC-1 planning panel produces an optimiser proposal and says "a planner
-- accepts or edits" — and nothing recorded what the planner then did. So a berth
-- queue could be re-ordered and afterwards no one could say which call moved,
-- why, on whose authority, or when. F-15 asks for exactly those four facts.
--
-- Append-only by intent. A berth decision is a historical fact: superseding it
-- means recording a NEW decision, never editing the old one, because the value
-- of this table is that it says what was believed at the time.
--
-- Purely additive: one new table.

CREATE TABLE IF NOT EXISTS core.berth_allocation_decision (
    decision_id   bigserial PRIMARY KEY,
    -- WHICH call moved. Text rather than a FK: the planner works from a berth
    -- plan whose rows are keyed by VCN/VIA, and those do not all exist as
    -- vessel_call rows (the API window and the file window describe different
    -- calls). A hard FK would refuse to record real decisions.
    call_id       text        NOT NULL,
    vessel_name   text,
    berth_code    text,
    -- The move itself. Both nullable: a decision can be "hold at current
    -- position", which has no new slot.
    from_position integer,
    to_position   integer,
    planned_start timestamptz,
    revised_start timestamptz,

    reason_code   text        NOT NULL,
    reason_note   text,

    -- WHO and WHEN. `actor` is the authenticated principal where there is one;
    -- the open demo profile records 'unauthenticated' rather than NULL, so an
    -- unattributed decision is visibly unattributed instead of looking absent.
    actor         text        NOT NULL DEFAULT 'unauthenticated',
    actor_role    text,
    decided_at    timestamptz NOT NULL DEFAULT now(),

    -- Provenance, as everywhere: a decision taken during a demonstration must
    -- not be indistinguishable from one taken on the day.
    data_origin   text        NOT NULL DEFAULT 'MANUAL',
    source        text,
    detail        jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE core.berth_allocation_decision IS
  'Append-only log of berth queue re-ordering: which call, from/to position, '
  'reason code, actor and timestamp (F-15). Superseding a decision means '
  'appending another, never editing this one.';

CREATE INDEX IF NOT EXISTS idx_berth_decision_call
    ON core.berth_allocation_decision (call_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_berth_decision_time
    ON core.berth_allocation_decision (decided_at DESC);

-- The reason vocabulary, as a table rather than a CHECK constraint: a berth
-- planner's reasons are operational, not structural, and JNPA will add to them.
-- A CHECK on a shared database would make every addition a migration.
CREATE TABLE IF NOT EXISTS core.berth_reason_code (
    code        text PRIMARY KEY,
    label       text NOT NULL,
    category    text,
    sort_order  integer NOT NULL DEFAULT 100,
    is_active   boolean NOT NULL DEFAULT true
);

INSERT INTO core.berth_reason_code (code, label, category, sort_order) VALUES
    ('TIDE_WINDOW',      'Tide window / draft restriction',        'PHYSICAL',   10),
    ('DRAFT_RESTRICTION','Declared draft exceeds berth depth',     'PHYSICAL',   20),
    ('BERTH_OCCUPIED',   'Berth still occupied by preceding call', 'CONGESTION', 30),
    ('VESSEL_DELAY',     'Vessel delayed at anchorage / arrival',  'VESSEL',     40),
    ('CRANE_AVAILABILITY','Crane or gang not available',           'RESOURCE',   50),
    ('PILOT_AVAILABILITY','Pilot or tug not available',            'RESOURCE',   60),
    ('WEATHER',          'Weather / visibility',                   'PHYSICAL',   70),
    ('PRIORITY_CALL',    'Priority call (line commitment)',        'COMMERCIAL', 80),
    ('CARGO_READINESS',  'Cargo or documentation not ready',       'CARGO',      90),
    ('OPTIMISER_ACCEPTED','Accepted the optimiser proposal as-is', 'SYSTEM',    100),
    ('OPTIMISER_OVERRIDE','Overrode the optimiser proposal',       'SYSTEM',    110),
    ('OTHER',            'Other (see note)',                       'OTHER',    9999)
ON CONFLICT (code) DO NOTHING;
