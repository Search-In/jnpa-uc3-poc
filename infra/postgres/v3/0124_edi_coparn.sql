-- 0124  COPARN (Empty Container Release Order) support for the EDI vessel tables
--
-- The LIVE dt.jnpa.in corpus serves COPARN documents (<ContainerRelease>
-- XML, ~1,250 containers each, release/pickup timestamps + depot codes)
-- under the SHIPPING-LINES group — a message type absent from the sample
-- pack, discovered as 103 UNROUTED records on the first live backfill.
-- They now route to services.edi_vessel (0123 tables); this migration
-- widens those tables: feed/doc CHECKs gain 'COPARN' and the item columns
-- gain the release-order fields (+ voyage, which COPRAR headers also carry).
--
-- Idempotent; safe on a database whose 0123 tables were created either by
-- the migration or by the gateway's boot DDL.

BEGIN;

ALTER TABLE core.edi_vessel_container
    ADD COLUMN IF NOT EXISTS voyage text,
    ADD COLUMN IF NOT EXISTS release_ts timestamptz,
    ADD COLUMN IF NOT EXISTS pickup_ts timestamptz,
    ADD COLUMN IF NOT EXISTS depot_code text;

ALTER TABLE core.edi_vessel_container
    DROP CONSTRAINT IF EXISTS edi_vessel_container_doc_check;
ALTER TABLE core.edi_vessel_container
    ADD CONSTRAINT edi_vessel_container_doc_check
        CHECK (doc_type = ANY (ARRAY['COARRI'::text, 'COPRAR'::text,
                                     'COPARN'::text]));

ALTER TABLE core.edi_import_file
    DROP CONSTRAINT IF EXISTS edi_import_file_feed_check;
ALTER TABLE core.edi_import_file
    ADD CONSTRAINT edi_import_file_feed_check
        CHECK (feed = ANY (ARRAY['COARRI'::text, 'COPRAR'::text,
                                 'COPARN'::text]));

COMMIT;
