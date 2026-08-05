-- 0025_transporter_master_fields.sql
-- Transport Master (UC-III) — extend the EXISTING jnpa.transporters entity with
-- the official TransporterDetails.xlsx fields. Purely additive + backward
-- compatible: every statement is ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT
-- EXISTS, so re-running is a no-op and NOTHING existing is dropped or altered.
--
-- Rationale: a first-class transporter entity already exists (migration 0024:
-- jnpa.transporters + transporter_vehicles + transporter_blacklist), wired to the
-- blacklist lifecycle, gate/driver validation, WS alerts, audit and the admin UI.
-- We therefore EXTEND it rather than fork a second "transport_master" table.
--
-- New columns map 1:1 to the source dataset columns:
--   company_id            -> source_company_id  (external stable key; unique)
--   user_user_id          -> source_user_id
--   contactPersonName     -> contact_person
--   designation           -> designation
--   email                 -> email
--   mobile_number         -> mobile
--   address               -> address
--   company_document1     -> doc_type
--   company_document_file1-> doc_file
-- (company_name maps to the existing `name`; the legacy `contact` jsonb and all
--  existing columns are left untouched and are still populated by the importer.)

SET search_path TO jnpa, public;

ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS source_company_id bigint;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS source_user_id    bigint;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS contact_person    text;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS designation       text;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS email             text;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS mobile            text;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS address           text;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS doc_type          text;
ALTER TABLE jnpa.transporters ADD COLUMN IF NOT EXISTS doc_file          text;

-- External stable identity from the source system. Nullable so the pre-existing
-- operator-entered rows (which have no source id) remain valid; Postgres permits
-- multiple NULLs under a UNIQUE index, and the importer upserts on this key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_transporters_source_company_id
    ON jnpa.transporters (source_company_id);

CREATE INDEX IF NOT EXISTS idx_transporters_mobile ON jnpa.transporters (mobile);
CREATE INDEX IF NOT EXISTS idx_transporters_email  ON jnpa.transporters (lower(email));
