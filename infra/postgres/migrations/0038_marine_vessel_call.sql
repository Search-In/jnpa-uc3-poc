-- 0038_marine_vessel_call.sql — UC-I Marine: the vessel-call spine. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0038_marine_vessel_call.sql
--
-- One row per vessel visit (core.vessel_call) plus its fine-grained actuals
-- (core.vessel_call_event: anchored / pilot boarded / first line / all fast / berthed /
-- ops start / ops end / sailed / departed). Sourced from the NLP-Marine PCS message
-- family (CALINF -> BERMAN -> BERALT / PLTMEM -> VESARR -> VESDEP -> CALINV), the
-- per-terminal berthing reports, the pilot cards and TOS File 02.
--
-- STRICTLY ADDITIVE. This migration creates ONLY new objects in the NEW `core` schema.
-- It NEVER drops, alters or reads jnpa.berthing_reports / jnpa.berthing_events /
-- jnpa.berthing_import_* — nor ANY other jnpa.* object. The berthing module keeps
-- writing its own rows exactly as it does today. A later migration will add a
-- non-breaking soft link (berthing_reports.id -> vessel_call.call_id BY VALUE, no FK).
--
-- Vessel Call is a SEPARATE lifecycle entity from a berthing report:
--   * a berthing report row is a terminal-side daily snapshot of a call at a berth;
--   * a vessel call is the whole visit, and exists BEFORE a terminal is assigned
--     (a CALINF voyage registration carries neither VCN nor terminal), which the
--     jnpa.berthing_reports natural key (terminal, voyage_number, vessel_name)
--     structurally cannot represent.
--
-- ---------------------------------------------------------------------------
-- SOURCE OF TRUTH: Data_Schema_v3 / schema.sql, section 3 "VESSEL-CALL SPINE (UC-I)".
-- Table names, column names, data types, nullability and relationships are taken
-- from schema.sql verbatim. The deviations below are the ONLY differences, and each
-- is either mechanically required or an explicitly approved additive extension.
--
-- DEVIATION LEDGER
--
-- [D1] SCHEMA NAMESPACE — `core`, per schema.sql, NOT the jnpa schema used by the
--      other 161 UC3 tables. Deliberate: schema.sql's design is three-layer
--      (staging -> core -> mart) and the remaining UC-I tables (core.vessel,
--      core.ref_berth, core.pilotage, core.sea_channel, ...) plus the FK parents
--      referenced below all live in `core`. Technically free: jnpa_shared.db.get_engine()
--      sets no search_path, and every UC3 repository already fully qualifies its
--      table names, so a second schema changes nothing for existing code.
--      OPS NOTE: grants / backup / restore scope must now include `core` as well
--      as `jnpa`; infra/postgres/init.sql does not create it, this migration does.
--
-- [D2] DEFERRED FOREIGN KEYS — schema.sql declares six FKs on these two tables.
--      Only one has an existing parent, so the other five are declared as plain
--      columns (exact schema.sql name + type) and their REFERENCES clauses are
--      deferred to the migration that creates each parent:
--        core.vessel_call.imo_no       -> core.vessel        (deferred)
--        core.vessel_call.terminal_id  -> core.ref_terminal  (deferred)
--        core.vessel_call.berth_id     -> core.ref_berth     (deferred)
--        core.vessel_call_event.berth_id    -> core.ref_berth    (deferred)
--        core.vessel_call_event.source_file -> core.ingest_file (deferred)
--        core.vessel_call_event.call_id     -> core.vessel_call  (KEPT — parent is here)
--      Each will be re-attached with ALTER TABLE ... ADD CONSTRAINT once its parent
--      lands. No data-model divergence: only enforcement is deferred. This mirrors
--      migration 0036, where jnpa.berthing_reports.import_file_id is likewise a
--      plain bigint and the vessel/voyage link is documented as BY VALUE (no FK).
--
-- [D3] NAMED INDEXES + IF NOT EXISTS — schema.sql writes `CREATE INDEX ON core.x (y)`
--      with no name and no guard. Postgres auto-names such an index, and re-running
--      the statement raises "relation already exists", which would crash
--      gateway/marine_ext.ensure_marine_schema() on the second boot. Naming them and
--      adding IF NOT EXISTS is mechanically required. Index COLUMNS are unchanged
--      from schema.sql (lines 256, 257, 270).
--
-- [D4] NAMED UNIQUE CONSTRAINT — schema.sql writes an inline `vcn text UNIQUE`;
--      this uses the UC3 `CONSTRAINT uq_<name> UNIQUE (...)` form. Identical
--      semantics, including Postgres' multi-NULL behaviour, which is what allows a
--      pre-VCN call (CALINF) to exist and later be promoted when BERMAN assigns one.
--
-- [D5] AUDIT COLUMNS (ADDED) — created_at / updated_at + a BEFORE UPDATE trigger,
--      per the UC3 convention on every mutable table (jnpa.cargo, jnpa.berthing_reports,
--      jnpa.transporters, ...). schema.sql omits them here but uses the same idiom on
--      core.vessel (updated_at timestamptz DEFAULT now()), so this stays inside the
--      document's own conventions. updated_at is also required as the default list
--      sort key, matching services/berthing/repository.py.
--      core.set_updated_at() is created here rather than reusing jnpa.set_updated_at()
--      so the `core` schema is self-contained and nothing in `jnpa` is touched.
--
-- [D6] EVENT UNIQUENESS (ADDED) — uq_vessel_call_event UNIQUE (call_id, event_type,
--      event_ts). schema.sql declares no constraint, which would duplicate every
--      event on each re-import of the same VESARR/VESDEP log and corrupt turnaround
--      time. jnpa.berthing_events swings the other way with UNIQUE (berthing_id,
--      event_type) — one row per type per call — which makes shifting and repeat
--      anchorings unrepresentable. Including event_ts gives idempotent ingestion
--      (exact re-import collapses on ON CONFLICT DO NOTHING) while still allowing
--      genuine repeats at different times. Both columns are NOT NULL, so there is no
--      NULL-skipping hole in the constraint.
--
-- [D7] EXTRA INDEX (ADDED) — idx_vessel_call_updated on (updated_at DESC), required
--      by the default list ordering. schema.sql's two indexes are kept verbatim.
--
-- [D8] status LEFT FREE-TEXT — schema.sql gives `status text` with no CHECK, and that
--      is followed here rather than copying the 7-state CHECK on jnpa.berthing_reports.
--      The vessel-call vocabulary spans PCS message states, berthing-report sections
--      and pilot-card movement types; constraining it before the parsers exist would
--      be inventing a model. A CHECK is added once the real vocabulary is known.
--
-- [D9] IDENTITY COLUMNS — `bigint GENERATED ALWAYS AS IDENTITY` per schema.sql, not
--      the `bigserial` used by all 161 jnpa.* tables. Deliberate: this is a new schema
--      adopting schema.sql's DDL idiom wholesale rather than producing a hybrid that
--      matches neither document. GENERATED ALWAYS also blocks accidental explicit-id
--      inserts; a writer that genuinely needs to force an id must say
--      OVERRIDING SYSTEM VALUE.
--
-- [D10] via_no IS INDEXED, NOT UNIQUE — short VIA numbers (S0561, OSV0266) recycle
--      across years, so they cannot carry a uniqueness constraint. Matches schema.sql,
--      which indexes via_no (line 256) but declares UNIQUE only on vcn.
--
-- ROLLBACK: DROP TABLE core.vessel_call_event; DROP TABLE core.vessel_call;
--           (events first). Nothing in the jnpa schema is affected.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS core;

-- ------------------------------------------------------------------ audit trigger fn
-- Mirrors jnpa.set_updated_at() (init.sql) so the `core` schema carries no dependency
-- on the jnpa schema. CREATE OR REPLACE is idempotent. See [D5].
CREATE OR REPLACE FUNCTION core.set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ------------------------------------------------------------------ vessel calls
-- One row per vessel visit. vcn and the short via_no are BOTH nullable: a CALINF
-- voyage registration has neither yet, and berthing reports / pilot cards carry only
-- the short VIA. The call is created on first sighting and enriched in place as later
-- messages arrive.
CREATE TABLE IF NOT EXISTS core.vessel_call (
    call_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- [D9]
    vcn           text,                      -- INNSA1BM0R3119 (full PCS VCN)
    via_no        text,                      -- S0561 / OSV0266 / R#### (last 5 of the VCN)
    imo_no        text,                      -- FK -> core.vessel deferred [D2]
    vessel_name   text,
    voyage_no     text,
    rotation_no   text,
    terminal_id   smallint,                  -- FK -> core.ref_terminal deferred [D2]
    berth_id      smallint,                  -- FK -> core.ref_berth deferred [D2]
    purpose       text,
    eta           timestamptz,
    etd           timestamptz,
    etb           timestamptz,
    ata           timestamptz,               -- actual arrival / alongside
    atd           timestamptz,               -- actual departure
    atc           timestamptz,               -- actual completion of ops
    status        text,                      -- free-text by design [D8]
    igm_no        bigint,                    -- soft link to the customs manifest; no FK
    source_note   text,
    created_at    timestamptz NOT NULL DEFAULT now(),                -- [D5]
    updated_at    timestamptz NOT NULL DEFAULT now(),                -- [D5]
    -- Multiple NULLs are permitted by Postgres, which is exactly what lets a
    -- pre-VCN (CALINF-seeded) call exist until BERMAN assigns one. [D4]
    CONSTRAINT uq_vessel_call_vcn UNIQUE (vcn));

CREATE INDEX IF NOT EXISTS idx_vessel_call_via         ON core.vessel_call (via_no);
CREATE INDEX IF NOT EXISTS idx_vessel_call_imo_voyage  ON core.vessel_call (imo_no, voyage_no);
CREATE INDEX IF NOT EXISTS idx_vessel_call_updated     ON core.vessel_call (updated_at DESC);

DROP TRIGGER IF EXISTS trg_vessel_call_updated_at ON core.vessel_call;
CREATE TRIGGER trg_vessel_call_updated_at
    BEFORE UPDATE ON core.vessel_call
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- ------------------------------------------------------------------ call actuals
-- Fine-grained milestones from VESARR / VESDEP, pilot cards and berthing reports.
-- Unlike jnpa.berthing_events this permits REPEATED events of the same type at
-- different timestamps (shifting, a second anchoring) while still de-duplicating an
-- exact re-import. See [D6].
CREATE TABLE IF NOT EXISTS core.vessel_call_event (
    event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,     -- [D9]
    call_id     bigint REFERENCES core.vessel_call (call_id),        -- only FK kept [D2]
    event_type  text NOT NULL,               -- 'ANCHORED','PILOT_BOARDED','FIRST_LINE',
                                             -- 'ALL_FAST','BERTHED','OPS_START','OPS_END',
                                             -- 'SAILED','DEPARTED',...
    event_ts    timestamptz NOT NULL,
    berth_id    smallint,                    -- FK -> core.ref_berth deferred [D2]
    source_file bigint,                      -- FK -> core.ingest_file deferred [D2]
    created_at  timestamptz NOT NULL DEFAULT now(),                  -- [D5]
    -- No ON DELETE clause: schema.sql specifies none, so NO ACTION applies and a call
    -- that still has actuals cannot be silently deleted.
    CONSTRAINT uq_vessel_call_event UNIQUE (call_id, event_type, event_ts));

CREATE INDEX IF NOT EXISTS idx_vessel_call_event_call_ts
    ON core.vessel_call_event (call_id, event_ts);
