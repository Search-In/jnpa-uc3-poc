-- ============================================================================
-- 0104  Fix-ups discovered during code migration (all additive / idempotent).
-- Run AFTER 0202. Captures every statement applied during module migration so
-- a fresh environment reaches the exact production-validated state.
-- ============================================================================

-- transporter.contact carries the legacy jsonb payload
ALTER TABLE core.transporter ALTER COLUMN contact TYPE jsonb USING contact::jsonb;

-- upload-ledger linkage (Transporters & Drivers upload module)
ALTER TABLE core.transporter ADD COLUMN IF NOT EXISTS import_file_id bigint
    REFERENCES core.td_import_file(id);
ALTER TABLE core.driver ADD COLUMN IF NOT EXISTS import_file_id bigint
    REFERENCES core.td_import_file(id);
UPDATE core.transporter t SET import_file_id = j.import_file_id
FROM jnpa.transporters j WHERE j.id = t.id AND j.import_file_id IS NOT NULL
  AND t.import_file_id IS NULL;
UPDATE core.driver d SET import_file_id = j.import_file_id
FROM jnpa.driver_master j WHERE j.id = d.id AND j.import_file_id IS NOT NULL
  AND d.import_file_id IS NULL;

-- driver upsert arbiter: managed rows (legacy id range) are unique per licence
CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_licence_norm
    ON core.driver (licence_no_norm) WHERE id < 100000000;
-- sequences must mint ids inside the managed range
SELECT setval('core.driver_id_seq',
              coalesce((SELECT max(id) FROM core.driver WHERE id < 100000000),0)+1, false);
SELECT setval('core.transporter_id_seq',
              coalesce((SELECT max(id) FROM core.transporter WHERE id < 100000000),0)+1, false);

-- customs
ALTER TABLE core.smtp_permit ADD COLUMN IF NOT EXISTS customs_house text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_rms_scan_report_igm
    ON core.rms_scan_report (igm_no) WHERE igm_no IS NOT NULL;

-- shipping lines: legacy dedup semantics are per-file, not global
DROP INDEX IF EXISTS core.uq_alc_row_sha;
DROP INDEX IF EXISTS core.uq_alc_row_sha256;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alc_file_rowsha
    ON core.advance_list_container (import_file_id, row_sha256);

-- delivery-order lines: flat-legacy fidelity + content-level dedup key
ALTER TABLE core.delivery_order_line
    ADD COLUMN IF NOT EXISTS common_ref_number text,
    ADD COLUMN IF NOT EXISTS vcn text,
    ADD COLUMN IF NOT EXISTS imo_number text,
    ADD COLUMN IF NOT EXISTS shipping_agent_code text,
    ADD COLUMN IF NOT EXISTS final_pod text,
    ADD COLUMN IF NOT EXISTS container_valid_iso boolean,
    ADD COLUMN IF NOT EXISTS import_file_id bigint REFERENCES core.sl_import_file(id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dol_legacy_dedup ON core.delivery_order_line
    (coalesce(common_ref_number,''), container_no, coalesce(gate_pass_no,''));
UPDATE core.delivery_order_line l SET
    common_ref_number = j.common_ref_number, vcn = j.vcn, imo_number = j.imo_number,
    shipping_agent_code = j.shipping_agent_code, final_pod = j.final_pod,
    container_valid_iso = j.container_valid_iso, import_file_id = j.import_file_id
FROM jnpa.sl_delivery_orders j
WHERE j.id = l.id AND l.common_ref_number IS NULL;
