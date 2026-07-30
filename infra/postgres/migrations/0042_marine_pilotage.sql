-- 0042_marine_pilotage.sql — UC-I Marine: pilots + pilotage movements. Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0042_marine_pilotage.sql
--
-- core.pilot — the pilot roster. core.pilotage — one row per pilotage movement
-- (INWARD/OUTWARD/SHIFTING) from Pilot_card_data.xlsx, carrying the marine-side actuals
-- (anchor down/up, pilot boarded, first line, all fast, disembark, berth vacated).
-- These feed the UC-I turnaround / pre-berthing-delay KPIs alongside vessel_call_event.
--
-- STRICTLY ADDITIVE. Creates ONLY new core.* objects; touches NOTHING in jnpa.
--
-- SOURCE OF TRUTH: schema.sql §2 (core.pilot) + §3 (core.pilotage). Columns, types, the
-- movement_type CHECK, the via_no index and every FK are verbatim.
-- FK relationships (all parents already exist -> enforced INLINE):
--   pilotage.call_id       -> core.vessel_call  (migration 0038)
--   pilotage.pilot_code    -> core.pilot        (this file)
--   pilotage.from_berth_id -> core.ref_berth    (migration 0040)
--   pilotage.to_berth_id   -> core.ref_berth    (migration 0040)
-- NOTE: pilotage.imo_no and pilotage.via_no are PLAIN text columns in schema.sql (no
-- REFERENCES) — deliberately soft, so a pilot card for a vessel not yet in core.vessel,
-- or a recycled short VIA, still loads. This matches schema.sql exactly.
--
-- Deviations: [D1] core schema; [D9] bigint IDENTITY PK on pilotage, per schema.sql.
-- No created_at/updated_at is added — schema.sql declares none for these tables and the
-- source of truth is followed literally. numeric(5,2) drafts verbatim.
--
-- ROLLBACK: DROP TABLE core.pilotage; DROP TABLE core.pilot;

CREATE SCHEMA IF NOT EXISTS core;

-- ------------------------------------------------------------------ pilots
CREATE TABLE IF NOT EXISTS core.pilot (
    pilot_code  text PRIMARY KEY,             -- 'JP 91' ...
    name        text);

-- ------------------------------------------------------------------ pilotage movements
CREATE TABLE IF NOT EXISTS core.pilotage (
    pilotage_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    movement_type    text NOT NULL CHECK (movement_type IN ('INWARD','OUTWARD','SHIFTING')),
    call_id          bigint REFERENCES core.vessel_call,
    via_no           text,
    imo_no           text,                     -- soft column (no FK), per schema.sql
    vessel_name      text,
    pilot_code       text REFERENCES core.pilot,
    vessel_condition text,                     -- LOADED / BALLAST
    from_berth_id    smallint REFERENCES core.ref_berth,
    to_berth_id      smallint REFERENCES core.ref_berth,
    draft_fwd_m      numeric(5,2),
    draft_aft_m      numeric(5,2),
    pilot_boarded_at     timestamptz,
    first_line_at        timestamptz,
    all_fast_at          timestamptz,
    pilot_disembarked_at timestamptz,
    berth_vacated_at     timestamptz,
    anchor_down_at       timestamptz,
    anchor_up_at         timestamptz,
    submitted_at         timestamptz,
    extras           jsonb);                   -- sheet-specific columns

CREATE INDEX IF NOT EXISTS idx_pilotage_via   ON core.pilotage (via_no);
CREATE INDEX IF NOT EXISTS idx_pilotage_call  ON core.pilotage (call_id);
CREATE INDEX IF NOT EXISTS idx_pilotage_imo   ON core.pilotage (imo_no);
CREATE INDEX IF NOT EXISTS idx_pilotage_pilot ON core.pilotage (pilot_code);
