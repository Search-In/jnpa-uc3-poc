-- ============================================================================
-- 0129  core.vessel_call_moves — the missing NUMERATOR for crane productivity.
--
-- JNPA What-If Notice (05 Aug 2026) scenario II-B: "Derive the effective crane
-- productivity implied by the data for each vessel call, expressed as gross moves
-- per hour worked."
--
--   Gross Moves / Working Hours
--
-- The audit found the denominator already present (core.berthing_record carries
-- cargo_operation_start / cargo_operation_end) and the numerator missing from the
-- ENTIRE schema — no table, column or parser in the repository produced a move
-- count. core.perf_daily_vessel stops at berth_no / berthed_on /
-- expected_completion; core.berthing_record has no volume column.
--
-- This table supplies it. `data_origin` is the honesty switch:
--
--   API      — a move count published by JNPA (authoritative)
--   MANUAL   — keyed in from a terminal report / operator input
--   DERIVED  — counted from core.edi_vessel_container (migration 0125), i.e. the
--              EDI container manifest for the call. A REASONABLE PROXY, not a
--              measurement: it counts manifest lines, so it excludes restows and
--              any box handled outside the manifest. Every what-if answer built
--              on a DERIVED row must declare that (Notice §1.c) — it does; see
--              services/cargo/simulation/crane_productivity.py.
--
-- IDENTITY, and why there are two keys:
--   core.edi_vessel_container identifies a call by VCN (vessel call number) and
--   carries NO vessel name or voyage number. core.berthing_record identifies a
--   call by (terminal, voyage_number, vessel_name) and carries NO VCN. There is
--   no reliable join between them in this database, so this table accepts EITHER
--   identity and the service resolves a call in that order:
--       berthing_record_id -> vcn -> (terminal, voyage_number, vessel_name)
--   A call that matches none of them is reported as "productivity not derivable"
--   rather than being given an invented number.
--
-- Additive: reads core.edi_vessel_container, writes only itself. No existing
-- table or column is touched.
-- ============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.vessel_call_moves (
    id                 bigserial PRIMARY KEY,
    berthing_record_id bigint REFERENCES core.berthing_record(id) ON DELETE SET NULL,

    -- identity A: the EDI side (VCN). Present on DERIVED rows.
    vcn                text,
    -- identity B: the berthing side. Present on API / MANUAL rows.
    terminal           text,
    vessel_name        text,
    voyage_number      text,
    via_no             text,

    -- the numerator
    discharge_moves    integer CHECK (discharge_moves IS NULL OR discharge_moves >= 0),
    load_moves         integer CHECK (load_moves      IS NULL OR load_moves      >= 0),
    restow_moves       integer CHECK (restow_moves    IS NULL OR restow_moves    >= 0),
    gross_moves        integer GENERATED ALWAYS AS (
                           COALESCE(discharge_moves, 0)
                         + COALESCE(load_moves, 0)
                         + COALESCE(restow_moves, 0)
                       ) STORED,

    -- equipment actually deployed on the call. NULL = not reported; the
    -- productivity figure is then per-VESSEL rather than per-crane, and says so.
    cranes_deployed    integer CHECK (cranes_deployed IS NULL OR cranes_deployed > 0),

    data_origin        text NOT NULL DEFAULT 'DERIVED'
                       CHECK (data_origin IN ('API','MANUAL','DERIVED')),
    source_note        text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    -- at least one usable identity
    CONSTRAINT ck_vcm_identity CHECK (
        vcn IS NOT NULL OR berthing_record_id IS NOT NULL
        OR (terminal IS NOT NULL AND vessel_name IS NOT NULL
            AND voyage_number IS NOT NULL))
);

-- Partial uniques: one row per VCN, and one per berthing-call natural key. Both
-- partial so a row that carries only the other identity does not collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_vcm_vcn
    ON core.vessel_call_moves (vcn) WHERE vcn IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_vcm_call
    ON core.vessel_call_moves (terminal, voyage_number, vessel_name)
    WHERE terminal IS NOT NULL AND voyage_number IS NOT NULL AND vessel_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vcm_berthing ON core.vessel_call_moves (berthing_record_id);
CREATE INDEX IF NOT EXISTS idx_vcm_origin   ON core.vessel_call_moves (data_origin);

DROP TRIGGER IF EXISTS trg_vessel_call_moves_updated_at ON core.vessel_call_moves;
CREATE TRIGGER trg_vessel_call_moves_updated_at
    BEFORE UPDATE ON core.vessel_call_moves
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- --------------------------------------------------------- DERIVED population
-- One row per VCN present in the EDI manifest. Counted, never invented: a VCN
-- with no manifest lines produces no row.
--
-- Direction rule: use the explicit `direction` column when the importer recovered
-- one from the filename; otherwise attribute by document type — COARRI is the
-- discharge/handling report, COPRAR the load order. Restows are not represented
-- in either document, so restow_moves stays NULL (it is not zero — it is unknown).
INSERT INTO core.vessel_call_moves
    (vcn, terminal, discharge_moves, load_moves, data_origin, source_note)
SELECT e.vcn,
       max(e.terminal_code),
       count(*) FILTER (WHERE upper(COALESCE(e.direction, '')) = 'DISCHARGE'
                           OR (e.direction IS NULL AND e.doc_type = 'COARRI')),
       count(*) FILTER (WHERE upper(COALESCE(e.direction, '')) = 'LOAD'
                           OR (e.direction IS NULL AND e.doc_type = 'COPRAR')),
       'DERIVED',
       'counted from core.edi_vessel_container (migration 0125) — manifest line '
       'count per VCN used as a proxy for moves; excludes restows'
  FROM core.edi_vessel_container e
 WHERE e.vcn IS NOT NULL AND btrim(e.vcn) <> ''
 GROUP BY e.vcn
HAVING count(*) > 0
 ON CONFLICT DO NOTHING;

COMMENT ON TABLE core.vessel_call_moves IS
    'Gross moves per vessel call — the numerator of the JNPA II-B crane '
    'productivity formula. data_origin=DERIVED means counted from the EDI '
    'manifest by VCN, and must be declared as an assumption by any what-if answer.';
COMMENT ON COLUMN core.vessel_call_moves.vcn IS
    'Vessel call number as published on the EDI COARRI/COPRAR header. The only '
    'call identity core.edi_vessel_container carries.';

COMMIT;
