-- ============================================================================
-- 0130  core.yard_block — real per-block yard capacity.
--
-- Audit finding Y1: GET /api/cargo/yard-optimization returned
--
--     congestion = min(1.0, containers / (zones * 10))
--
-- where 10 was `_YARD_BLOCK_CAPACITY`, a constant in services/cargo/service.py
-- with nothing in the response saying so. A JNPA evaluator asking "10 what, and
-- from where?" would get no answer from the payload.
--
-- This table is the capacity master. When it is populated the score is sourced;
-- when a zone is missing from it the service falls back to the nominal constant
-- AND names that zone in the response's `assumptions` array. Either way the
-- denominator is traceable.
--
-- Seeding: the JNPA terminal yard layout is not in this database, so this
-- migration creates the table and seeds NOTHING by default — an empty master is
-- honest ("we do not have the layout") where a fabricated one would not be. Load
-- real blocks with scripts/seed_yard_blocks.sql or a JNPA layout import; until
-- then every zone is declared as assumed.
--
-- Additive: new table only.
-- ============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.yard_block (
    id             bigserial PRIMARY KEY,
    terminal       text NOT NULL,
    -- 'A', 'B-01', '2P08' … matched against core.cargo.yard_block and against
    -- its leading zone segment (the part before the first '-').
    block_code     text NOT NULL,
    capacity_teus  integer CHECK (capacity_teus IS NULL OR capacity_teus > 0),
    reefer_capacity integer CHECK (reefer_capacity IS NULL OR reefer_capacity >= 0),
    block_type     text NOT NULL DEFAULT 'GENERAL'
                   CHECK (block_type IN ('GENERAL','REEFER','HAZ','EMPTY','OOG')),
    active         boolean NOT NULL DEFAULT true,
    source_note    text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_yard_block UNIQUE (terminal, block_code)
);

CREATE INDEX IF NOT EXISTS idx_yard_block_code ON core.yard_block (block_code)
    WHERE active IS TRUE;
CREATE INDEX IF NOT EXISTS idx_yard_block_type ON core.yard_block (block_type);

DROP TRIGGER IF EXISTS trg_yard_block_updated_at ON core.yard_block;
CREATE TRIGGER trg_yard_block_updated_at
    BEFORE UPDATE ON core.yard_block
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

COMMENT ON TABLE core.yard_block IS
    'Yard block capacity master. Populates the denominator of the yard '
    'congestion score; zones absent here are declared as assumptions by '
    'GET /api/cargo/yard-optimization rather than silently defaulted.';

COMMIT;
