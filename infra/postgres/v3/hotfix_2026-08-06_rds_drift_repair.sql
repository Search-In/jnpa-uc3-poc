-- ============================================================================
-- hotfix 2026-08-06 — repair RDS drift where the ledger RECORDED a migration
--                     that never actually executed
--
-- WHY THIS EXISTS
-- ---------------
-- `core.schema_migrations` on the live RDS was adopted by baseline, so a
-- recorded version is NOT evidence the SQL ran (0127's header says as much).
-- Verified against production on 2026-08-06: 0102_arch_extensions is recorded
-- as applied, but its ENTIRE customs block never executed — all 11 customs
-- tables are missing every column it adds, and all 12 of its sequences are
-- absent. Because scripts/migrate.py skips versions already in the ledger, a
-- normal `make migrate` will never fix this. Hence a hand-applied hotfix.
--
-- Observed production symptoms this repairs:
--   * customs: 399/399 API records FAILED  ("column message_id does not exist")
--   * shipping-lines: 94 IAL/EAL records FAILED at bind —
--     `invalid input for query argument $6: True (expected str, got bool)`
--     because core.advance_list_container.container_valid_iso is `text` on RDS
--     while 0102 declares it `boolean` and the importer passes a bool.
--   * shipping-lines importer's ON CONFLICT (import_file_id, row_sha256) has no
--     arbiter index — 0104 created `uq_alc_file_rowsha`, which is also absent.
--   * performance: terminal seed aborts on the missing uq_perf_terminals_code.
--
-- HOW TO RUN
-- ----------
--   * Run as the schema OWNER (the master DSN). The app role has no DDL rights
--     and is not owner of several objects (docs/RDS_SECURITY.md §3) — running
--     this as the app role will fail on the first ALTER, harmlessly, inside the
--     transaction below.
--   * Idempotent: safe to re-run. Every column uses ADD COLUMN IF NOT EXISTS,
--     every constraint is DO-block guarded, every index uses IF NOT EXISTS.
--   * Single transaction: any failure rolls the whole thing back, leaving the
--     database exactly as it was.
--   * This file is deliberately named hotfix_* so scripts/migrate.py ignores it
--     (see migrate.py:127) — it is NOT a migration and takes no ledger row.
--
-- NOT COVERED HERE (deliberate — see the notes at the bottom):
--   migrations 0125/0126/0127, which are genuinely unapplied and must go
--   through `scripts/migrate.py` so they are recorded properly.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Customs family — 0102_arch_extensions.sql lines 96–188, made idempotent.
--    Legacy row ids + message lineage back to core.customs_message.
-- ---------------------------------------------------------------------------

-- core.igm ------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.igm_id_seq;
ALTER TABLE core.igm
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.igm_id_seq'),
    ADD COLUMN IF NOT EXISTS message_id bigint,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.igm ADD CONSTRAINT uq_igm_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE core.igm ADD CONSTRAINT igm_message_id_fkey
        FOREIGN KEY (message_id) REFERENCES core.customs_message(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- core.igm_line -------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.igm_line_id_seq;
ALTER TABLE core.igm_line
    ADD COLUMN IF NOT EXISTS id             bigint DEFAULT nextval('core.igm_line_id_seq'),
    ADD COLUMN IF NOT EXISTS created_at     timestamptz DEFAULT now(),
    ADD COLUMN IF NOT EXISTS importer_state text,
    ADD COLUMN IF NOT EXISTS be_regularised text;
DO $$ BEGIN
    ALTER TABLE core.igm_line ADD CONSTRAINT uq_igm_line_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

-- core.igm_line_container ---------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.igm_line_container_id_seq;
ALTER TABLE core.igm_line_container
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.igm_line_container_id_seq'),
    ADD COLUMN IF NOT EXISTS iso_valid  boolean,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.igm_line_container
        ADD CONSTRAINT uq_igm_line_container_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

-- core.bill_of_entry_ooc ----------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.bill_of_entry_ooc_id_seq;
ALTER TABLE core.bill_of_entry_ooc
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.bill_of_entry_ooc_id_seq'),
    ADD COLUMN IF NOT EXISTS message_id bigint,
    ADD COLUMN IF NOT EXISTS ooc_type   text,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.bill_of_entry_ooc ADD CONSTRAINT uq_ooc_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE core.bill_of_entry_ooc ADD CONSTRAINT bill_of_entry_ooc_message_id_fkey
        FOREIGN KEY (message_id) REFERENCES core.customs_message(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- core.ooc_item -------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.ooc_item_id_seq;
ALTER TABLE core.ooc_item
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.ooc_item_id_seq'),
    ADD COLUMN IF NOT EXISTS iso_valid  boolean,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.ooc_item ADD CONSTRAINT uq_ooc_item_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

-- core.smtp_permit ----------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.smtp_permit_id_seq;
ALTER TABLE core.smtp_permit
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.smtp_permit_id_seq'),
    ADD COLUMN IF NOT EXISTS message_id bigint,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.smtp_permit ADD CONSTRAINT uq_smtp_permit_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE core.smtp_permit ADD CONSTRAINT smtp_permit_message_id_fkey
        FOREIGN KEY (message_id) REFERENCES core.customs_message(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- core.smtp_container -------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.smtp_container_id_seq;
ALTER TABLE core.smtp_container
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.smtp_container_id_seq'),
    ADD COLUMN IF NOT EXISTS line_no    integer,
    ADD COLUMN IF NOT EXISTS subline_no integer,
    ADD COLUMN IF NOT EXISTS iso_valid  boolean,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.smtp_container ADD CONSTRAINT uq_smtp_container_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

-- core.leo ------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.leo_id_seq;
ALTER TABLE core.leo
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.leo_id_seq'),
    ADD COLUMN IF NOT EXISTS message_id bigint,
    ADD COLUMN IF NOT EXISTS action     text,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.leo ADD CONSTRAINT uq_leo_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE core.leo ADD CONSTRAINT leo_message_id_fkey
        FOREIGN KEY (message_id) REFERENCES core.customs_message(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- core.shipping_bill --------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.shipping_bill_id_seq;
ALTER TABLE core.shipping_bill
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.shipping_bill_id_seq'),
    ADD COLUMN IF NOT EXISTS message_id bigint,
    ADD COLUMN IF NOT EXISTS action     text,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.shipping_bill ADD CONSTRAINT uq_shipping_bill_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE core.shipping_bill ADD CONSTRAINT shipping_bill_message_id_fkey
        FOREIGN KEY (message_id) REFERENCES core.customs_message(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- core.rms_scan_report ------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.rms_scan_report_id_ext_seq;
ALTER TABLE core.rms_scan_report
    ADD COLUMN IF NOT EXISTS id             bigint DEFAULT nextval('core.rms_scan_report_id_ext_seq'),
    ADD COLUMN IF NOT EXISTS message_id     bigint,
    ADD COLUMN IF NOT EXISTS customs_house  text,
    ADD COLUMN IF NOT EXISTS igm_date       date,
    ADD COLUMN IF NOT EXISTS igm_date_raw   text,
    ADD COLUMN IF NOT EXISTS subject        text,
    ADD COLUMN IF NOT EXISTS any_selected   boolean,
    ADD COLUMN IF NOT EXISTS selected_count integer,
    ADD COLUMN IF NOT EXISTS created_at     timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.rms_scan_report ADD CONSTRAINT uq_rms_scan_report_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE core.rms_scan_report ADD CONSTRAINT rms_scan_report_message_id_fkey
        FOREIGN KEY (message_id) REFERENCES core.customs_message(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- core.rms_scan_container ---------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS core.rms_scan_container_id_seq;
ALTER TABLE core.rms_scan_container
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.rms_scan_container_id_seq'),
    ADD COLUMN IF NOT EXISTS igm_no     bigint,
    ADD COLUMN IF NOT EXISTS iso_valid  boolean,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
DO $$ BEGIN
    ALTER TABLE core.rms_scan_container ADD CONSTRAINT uq_rms_scan_container_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

-- 0102 also indexes RMS by igm_no; harmless and cheap to assert here.
CREATE INDEX IF NOT EXISTS idx_rms_scan_container_igm
    ON core.rms_scan_container (igm_no) WHERE igm_no IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 2. Shipping lines — the IAL/EAL import blockers.
-- ---------------------------------------------------------------------------

-- 2a. container_valid_iso: text on RDS, boolean per 0102:199 and per the
--     importer (services/shipping_lines/parsers/common.py:94 returns bool).
--     Verified 2026-08-06: all 8,878 existing rows are NULL, so no data can be
--     lost here — the CASE is defensive in case values appear before this runs.
--     Guarded so a re-run against an already-boolean column is a no-op.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'core'
                  AND table_name   = 'advance_list_container'
                  AND column_name  = 'container_valid_iso'
                  AND data_type   <> 'boolean') THEN
        EXECUTE $ddl$
            ALTER TABLE core.advance_list_container
                ALTER COLUMN container_valid_iso TYPE boolean
                USING (CASE lower(btrim(container_valid_iso))
                            WHEN 'true'  THEN true  WHEN 't' THEN true
                            WHEN 'yes'   THEN true  WHEN 'y' THEN true
                            WHEN '1'     THEN true
                            WHEN 'false' THEN false WHEN 'f' THEN false
                            WHEN 'no'    THEN false WHEN 'n' THEN false
                            WHEN '0'     THEN false
                            ELSE NULL END)
        $ddl$;
    END IF;
END $$;

-- NOTE on a deliberate omission: 0102 also creates core.advance_list_container_id_seq
-- and hangs a DEFAULT + uq_alc_ext_id off an `id` column. That column already
-- exists here (0202 created it, without the default) and NOTHING reads it — the
-- table's real key is al_id, and the importer never inserts `id`. Backfilling it
-- to satisfy a UNIQUE constraint would rewrite all 8,878 rows for no functional
-- gain, so it is intentionally left alone. Do not "complete" it without a reason.

-- 2b. The arbiter index the importer's ON CONFLICT needs (0104:39-40).
--     NULLs are distinct in a UNIQUE index, so the 8,878 legacy rows that hold
--     (NULL, NULL) do not collide; every API-imported row carries both values.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alc_file_rowsha
    ON core.advance_list_container (import_file_id, row_sha256);
CREATE INDEX IF NOT EXISTS idx_alc_import_file
    ON core.advance_list_container (import_file_id);


-- ---------------------------------------------------------------------------
-- 3. Performance — the terminal seed's ON CONFLICT target
--    (gateway/performance_ext.py:43,53). Verified: 8 rows, 0 duplicate codes.
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    ALTER TABLE core.perf_terminal ADD CONSTRAINT uq_perf_terminals_code UNIQUE (code);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

COMMIT;


-- ============================================================================
-- POST-CHECKS — run these after COMMIT; all three should report zero problems.
-- ============================================================================
-- \echo '-- missing customs columns (expect 0 rows):'
-- SELECT t.table_name, c.needed
--   FROM (VALUES ('igm','message_id'), ('leo','message_id'),
--                ('shipping_bill','message_id'), ('rms_scan_report','message_id'),
--                ('smtp_permit','message_id'), ('bill_of_entry_ooc','message_id'))
--        AS c(table_name, needed)
--   JOIN information_schema.tables t
--     ON t.table_schema = 'core' AND t.table_name = c.table_name
--  WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns k
--                     WHERE k.table_schema = 'core' AND k.table_name = c.table_name
--                       AND k.column_name = c.needed);
--
-- \echo '-- container_valid_iso must be boolean:'
-- SELECT data_type FROM information_schema.columns
--  WHERE table_schema='core' AND table_name='advance_list_container'
--    AND column_name='container_valid_iso';
--
-- \echo '-- arbiter index must exist:'
-- SELECT indexname FROM pg_indexes
--  WHERE schemaname='core' AND indexname='uq_alc_file_rowsha';


-- ============================================================================
-- STILL OUTSTANDING after this hotfix — NOT done here, on purpose:
--
--   1. Migrations 0125 / 0126 / 0127 are genuinely unapplied (absent from the
--      ledger, not merely unexecuted). Apply them the normal way so they are
--      recorded:  python scripts/migrate.py --dsn <MASTER DSN>
--      0127 is what gives mart.v_shipping_line_container its data_origin
--      column; without it the shipping-lines LIVE/DEMO filter cannot narrow.
--      0126 is what lets the 103 UNROUTED COPARN records route.
--
--   2. Re-route the already-downloaded corpus once the schema is right — no
--      re-download, it replays from the raw store:
--        POST /api/integrations/jnpa/replay
--             {"statuses": ["UNROUTED", "FAILED", "REJECTED"]}
--
--   3. NOT a schema problem, listed so it is not mistaken for one:
--      * 370 FORM13 + 288 EIR + 134 PIN gate-documents records are UNROUTED —
--        no handler consumed them. core.gate_capture tracks provenance via
--        `source_mode`, not data_origin; this needs a routing change, not DDL.
--      * ~963 nlp-marine REJECTED are largely the keyless live CALINF records
--        (empty IMONumber/VoyageNumber) — a JNPA-side content defect, rejected
--        by design.
--      * core.berthing_report and core.gate_document are LEGACY tables that no
--        current service reads (berthing uses core.berthing_record*, gate-docs
--        use core.eir / pin_ticket / gate_capture). Their lack of data_origin
--        is harmless — do not "fix" it.
-- ============================================================================
