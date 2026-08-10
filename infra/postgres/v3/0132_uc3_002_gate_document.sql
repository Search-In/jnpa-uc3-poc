-- ============================================================================
-- 0132  UC3-002 — make core.gate_document loadable with the REAL corpus.
--
-- core.gate_document already exists (it comes from the customer's own
-- schema.sql, section 8) and already carries every column UC3-002 needs,
-- including image_file for the WhatsApp scan. Three things were missing before
-- the 12 real documents could be loaded and served:
--
--   1. core.ref_terminal was EMPTY, so gate_document.terminal_id (FK -> that
--      table) could not be populated at all. The 8 terminals below are the
--      customer's own reference list, verbatim.
--   2. There was NO unique key on gate_document, so a re-run of the importer
--      would silently duplicate all 12 rows. doc_variant is the corpus's own
--      per-document identity (the parsed filename stem), which makes
--      (doc_category, doc_variant) the natural key.
--   3. There was no provenance marker, so the 12 REAL customer documents were
--      indistinguishable from anything else written later. data_origin mirrors
--      the vocabulary migrations 0120/0121 established for core.eir /
--      core.pin_ticket, with 'REAL' added for corpus-sourced records.
--
-- Fully ADDITIVE and idempotent: no column dropped, no CHECK changed, no
-- existing row rewritten, safe to re-run. Nothing outside gate_document and the
-- (empty) ref_terminal table is touched.
-- ============================================================================
BEGIN;

-- -------------------------------------------------------------- 1. terminals
-- Reference data, exactly as supplied by the customer. ON CONFLICT on the
-- UNIQUE code keeps a re-run a no-op and never renumbers an existing terminal
-- (terminal_id is GENERATED ALWAYS, hence OVERRIDING SYSTEM VALUE).
INSERT INTO core.ref_terminal (terminal_id, code, name, operator, pcs_code)
OVERRIDING SYSTEM VALUE
VALUES
  (1, 'NSFT',  'Nhava Sheva Freeport Terminal',                    'J M Baxi Ports (Freeport)', 'INNSA1JNP1'),
  (2, 'NSICT', 'Nhava Sheva International Container Terminal',     'DP World',                  'INNSA1NSI1'),
  (3, 'NSIGT', 'Nhava Sheva India Gateway Terminal',               'DP World',                  'INNSA1NSG1'),
  (4, 'GTI',   'Gateway Terminals India',                          'APM Terminals (Maersk)',    'INNSA1GTI1'),
  (5, 'APMT',  'APM Terminals Mumbai (GTI berth ops)',             'APM Terminals',             'INNSA1APM1'),
  (6, 'BMCT',  'Bharat Mumbai Container Terminals',                'PSA',                       'INNSA1BMC1'),
  (7, 'NSDT',  'Nhava Sheva Distribution Terminal (shallow water)','JNPA',                      'INNSA1NSD1'),
  (8, 'JNPCT', 'Jawaharlal Nehru Port Container Terminal',         'JNPA',                      'INNSA1JNP1')
ON CONFLICT (code) DO NOTHING;

-- Keep the identity sequence ahead of the explicit ids we just forced in, so a
-- later plain INSERT does not collide. Guarded: only advances, never rewinds.
SELECT setval(pg_get_serial_sequence('core.ref_terminal', 'terminal_id'),
              GREATEST((SELECT max(terminal_id) FROM core.ref_terminal), 1), true);

-- ------------------------------------------------------------- 2. provenance
-- NULL on every pre-existing row (= "origin not recorded"), so nothing already
-- stored is reinterpreted. The importer stamps 'REAL' on the 12 corpus rows.
ALTER TABLE core.gate_document
    ADD COLUMN IF NOT EXISTS data_origin text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'gate_document_data_origin_check') THEN
        ALTER TABLE core.gate_document
            ADD CONSTRAINT gate_document_data_origin_check
            CHECK (data_origin IS NULL
                   OR data_origin IN ('REAL', 'API', 'MANUAL', 'SIM'));
    END IF;
END $$;

COMMENT ON COLUMN core.gate_document.data_origin IS
    'Provenance of the record: REAL = parsed verbatim from a customer source '
    'document; API/MANUAL = ingested later (see 0120/0121); SIM = generated. '
    'NULL on rows written before this migration.';

COMMENT ON COLUMN core.gate_document.image_file IS
    'Bucket-relative object key of the original scan, i.e. the path segment of '
    'GET /api/evidence/{image_file}. Same convention as '
    'core.gate_capture.object_path (migration 0117).';

-- ------------------------------------------------------------- 3. idempotency
-- doc_variant is the corpus filename stem (eir3_gateway_maersk, ticket2, ...) —
-- one physical document, one value. Partial so any future row that leaves
-- doc_variant NULL is unaffected rather than blocked.
CREATE UNIQUE INDEX IF NOT EXISTS uq_gate_document_category_variant
    ON core.gate_document (doc_category, doc_variant)
    WHERE doc_variant IS NOT NULL;

-- ------------------------------------------------------------- 4. query paths
-- UC3-002's evaluator search is by truck, then by date; the existing indexes
-- cover container_no / vehicle_no / (doc_category, terminal_id) but not time,
-- and not the driver-licence lookup the truck-visit screen offers.
CREATE INDEX IF NOT EXISTS idx_gate_document_doc_ts
    ON core.gate_document (doc_ts DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_gate_document_driver_licence
    ON core.gate_document (driver_licence)
    WHERE driver_licence IS NOT NULL;

COMMIT;
