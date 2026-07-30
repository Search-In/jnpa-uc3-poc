-- 0040_marine_reference.sql — UC-I Marine: terminal & berth reference dimensions.
-- Additive + idempotent.
--
-- APPLY: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0040_marine_reference.sql
--
-- The canonical terminal/berth dimensions plus their alias tables — the normalization
-- workhorses (schema.sql §1, §4). The alias tables absorb the 5+ code spellings seen
-- across the corpus (BMCTPL/NSGT/NSCT/'PSA Mumbai'/PCS codes; APM01/APMT-01/APMT-1,
-- CB04/CB-04, SWB-2, LB-01), so every marine source joins to one terminal_id / berth_id.
--
-- STRICTLY ADDITIVE. Creates ONLY new core.* objects; touches NOTHING in jnpa. These
-- become the parents for the FKs migration 0038 deferred (vessel_call.terminal_id,
-- vessel_call.berth_id, vessel_call_event.berth_id) — re-attached in migration 0044.
--
-- SOURCE OF TRUTH: schema.sql §1 (ref_terminal, ref_terminal_alias, ref_berth,
-- ref_berth_alias). Columns, types, UNIQUE and the within-file FKs are verbatim.
-- FK relationships (enforced INLINE — all parents are in this same file):
--   ref_terminal_alias.terminal_id -> ref_terminal
--   ref_berth.terminal_id          -> ref_terminal   (NULLable: port berths LB-01/SWB-2)
--   ref_berth_alias.berth_id       -> ref_berth
--
-- Deviations: [D1] core schema; [D9] IDENTITY PKs — both per migration 0038's ledger.
-- numeric(7,2)/numeric(5,2) kept verbatim from schema.sql.
--
-- SEED: the 7 canonical JNPA container terminals + their known aliases are seeded here,
-- mirroring the existing jnpa.perf_terminals seed (migration 0028) — the same reference
-- data, not newly synthesised. pcs_code is left NULL (the exact per-terminal PCS codes
-- are not asserted here). Berths are NOT seeded: no reliable berth register (length/
-- depth) is available yet; ref_berth is populated by a later berth-register import.
-- All seeds are ON CONFLICT DO NOTHING (idempotent).
--
-- ROLLBACK (reverse order):
--   DROP TABLE core.ref_berth_alias; DROP TABLE core.ref_berth;
--   DROP TABLE core.ref_terminal_alias; DROP TABLE core.ref_terminal;

CREATE SCHEMA IF NOT EXISTS core;

-- ------------------------------------------------------------------ terminals
CREATE TABLE IF NOT EXISTS core.ref_terminal (
    terminal_id  smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code         text NOT NULL UNIQUE,        -- NSFT,NSICT,NSIGT,APMT,BMCT,NSDT,JNPCT
    name         text NOT NULL,
    operator     text,                        -- 'DP World','PSA','APM Terminals','JNPA',...
    pcs_code     text);                        -- INNSA1JNP1 / INNSA1BMC1 / INNSA1NSI1 ...

CREATE TABLE IF NOT EXISTS core.ref_terminal_alias (
    alias        text PRIMARY KEY,
    terminal_id  smallint NOT NULL REFERENCES core.ref_terminal);

CREATE INDEX IF NOT EXISTS idx_ref_terminal_alias_terminal
    ON core.ref_terminal_alias (terminal_id);

-- ------------------------------------------------------------------ berths
CREATE TABLE IF NOT EXISTS core.ref_berth (
    berth_id       smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    terminal_id    smallint REFERENCES core.ref_terminal,   -- NULL for port berths
    code           text NOT NULL UNIQUE,      -- APM01,BMCT01..05,CB01..CB06,LB01,SWB2,...
    berth_length_m numeric(7,2),
    design_depth_m numeric(5,2));

CREATE INDEX IF NOT EXISTS idx_ref_berth_terminal ON core.ref_berth (terminal_id);

CREATE TABLE IF NOT EXISTS core.ref_berth_alias (
    alias     text PRIMARY KEY,
    berth_id  smallint NOT NULL REFERENCES core.ref_berth);

CREATE INDEX IF NOT EXISTS idx_ref_berth_alias_berth ON core.ref_berth_alias (berth_id);

-- ------------------------------------------------------------------ seed: terminals
-- Mirrors jnpa.perf_terminals (migration 0028). Idempotent on the code UNIQUE key.
INSERT INTO core.ref_terminal (code, name, operator) VALUES
    ('NSFT',  'Nhava Sheva Freeport Terminal',                'NSFT'),
    ('NSICT', 'Nhava Sheva International Container Terminal',  'DP World'),
    ('NSIGT', 'Nhava Sheva India Gateway Terminal',           'NSIGT'),
    ('APMT',  'APM Terminals / Gateway Terminals India',      'APM Terminals'),
    ('BMCT',  'Bharat Mumbai Container Terminals',            'PSA'),
    ('NSDT',  'Nhava Sheva Distribution Terminal',            'NSDT'),
    ('JNPCT', 'Jawaharlal Nehru Port Container Terminal',     'JNPA')
ON CONFLICT (code) DO NOTHING;

-- ------------------------------------------------------------------ seed: terminal aliases
-- terminal_id resolved at load time (IDENTITY), so the seed never hard-codes an id.
INSERT INTO core.ref_terminal_alias (alias, terminal_id)
SELECT a.alias, t.terminal_id
FROM (VALUES
    ('GTI',           'APMT'),
    ('APM',           'APMT'),
    ('APM Terminals', 'APMT'),
    ('BMCTPL',        'BMCT'),
    ('BMCTPSA',       'BMCT'),
    ('PSA',           'BMCT'),
    ('PSA Mumbai',    'BMCT'),
    ('NSGT',          'NSIGT'),
    ('NSCT',          'NSICT')
) AS a(alias, code)
JOIN core.ref_terminal t ON t.code = a.code
ON CONFLICT (alias) DO NOTHING;
