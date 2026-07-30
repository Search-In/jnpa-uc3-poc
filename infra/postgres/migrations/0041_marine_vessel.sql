-- 0041_marine_vessel.sql — UC-I Marine: vessel master + insurance. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0041_marine_vessel.sql
--
-- core.vessel — the UC-I vessel master (from VESPRO, enriched by pilot cards & IGM
-- headers) carrying the particulars the DUKC / tidal-window and port-craft-assignment
-- KPIs need (drafts, LOA/beam, thrusters). core.vessel_insurance — P&I cover per vessel.
--
-- STRICTLY ADDITIVE. Creates ONLY new core.* objects; touches NOTHING in jnpa. This
-- becomes the parent for the FK migration 0038 deferred (vessel_call.imo_no) —
-- re-attached in migration 0044.
--
-- SOURCE OF TRUTH: schema.sql §2 (core.vessel, core.vessel_insurance). All columns,
-- types and the composite PK are verbatim. imo_no is kept TEXT (dirty values exist in
-- the corpus: PAN codes, short numbers). vessel_insurance.imo_no -> vessel is enforced
-- INLINE (parent is in this same file); PK (imo_no, pi_club) per schema.sql.
--
-- Deviations: [D1] core schema; [D9] IDENTITY not used here (both PKs are natural text /
-- composite). numeric precisions verbatim. [D5] the set_updated_at trigger is added on
-- core.vessel — schema.sql already gives it `updated_at`; the trigger makes it
-- authoritative, matching the jnpa convention. It reuses core.set_updated_at(), created
-- in migration 0038 (this migration depends on 0038 having run — the normal sequential
-- order; ensure_marine_schema creates the function before this trigger).
--
-- ROLLBACK: DROP TABLE core.vessel_insurance; DROP TABLE core.vessel;
--           (only safe before 0044 attaches vessel_call.imo_no; drop that FK first.)

CREATE SCHEMA IF NOT EXISTS core;

-- ------------------------------------------------------------------ vessel master
CREATE TABLE IF NOT EXISTS core.vessel (
    imo_no          text PRIMARY KEY,          -- dirty values exist -> kept text
    vessel_name     text,
    call_sign       text,
    flag            text,
    vessel_type     text,                      -- PCS numeric codes + pilot free-text variants
    mtmv            text,                      -- MT / MV
    loa_m           numeric(7,2),
    beam_m          numeric(6,2),
    lbp_m           numeric(7,2),
    max_draft_m     numeric(5,2),
    grt             numeric(12,1),
    nrt             numeric(12,1),
    dwt             numeric(12,1),
    teu_capacity    integer,
    mmsi            text,
    engine_type     text,
    num_engines     smallint,
    propulsion_type text,
    num_propellers  smallint,
    max_speed_kn    numeric(4,1),
    bow_thruster    boolean,
    stern_thruster  boolean,
    built_date      date,
    reg_port        text,
    owner_name      text,                      -- truncated to 25 chars at source
    email           text,                      -- PII
    vespro_ref      text,                      -- CommonRefNumber of the VESPRO message
    updated_at      timestamptz DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_vessel_name      ON core.vessel (vessel_name);
CREATE INDEX IF NOT EXISTS idx_vessel_call_sign ON core.vessel (call_sign);
CREATE INDEX IF NOT EXISTS idx_vessel_mmsi      ON core.vessel (mmsi);

DROP TRIGGER IF EXISTS trg_vessel_updated_at ON core.vessel;
CREATE TRIGGER trg_vessel_updated_at
    BEFORE UPDATE ON core.vessel
    FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- ------------------------------------------------------------------ insurance
CREATE TABLE IF NOT EXISTS core.vessel_insurance (
    imo_no       text REFERENCES core.vessel,
    pi_club      text,
    valid_until  date,
    PRIMARY KEY (imo_no, pi_club));
