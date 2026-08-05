-- 0033_shipping_lines_upload.sql
-- Shipping Lines — reusable Data Upload sub-module (UC-II module 4).
--
-- PURELY ADDITIVE. Lets CONTROL_ROOM / CUSTOMS / ADMIN users upload future
-- IAL / EAL / EDO files (CSV/XLS/XLSX) through the UI without developer help.
-- It REUSES the existing shipping-line pipeline end to end:
--   * jnpa.sl_import_files   — import ledger / upload history   (this migration adds
--                              uploaded_by + source so uploads are attributable)
--   * jnpa.sl_import_errors  — per-row validation / import errors (reused as-is)
--   * jnpa.sl_events         — append-only upload event log       (reused as-is)
--   * sl_advance_containers / sl_delivery_orders / shipping_lines — the same target
--     tables, written via the SAME ShippingLinesRepository.persist() (sha256 file
--     dedup + row_sha256 ON CONFLICT DO NOTHING — idempotent, duplicate-safe).
--
-- NO new tables. NO change to any cargo/customs/gate table. Both ADD COLUMNs are
-- nullable / defaulted, so the existing directory importer and the 8,882 already
-- imported rows are unaffected. Idempotent (ADD COLUMN IF NOT EXISTS). The identical
-- ALTERs are embedded in gateway/shipping_lines_ext.py (asserted in lock-step by
-- tests/test_shipping_lines_schema.py).

-- Who uploaded the file (audit). NULL for the directory importer / pre-existing rows.
ALTER TABLE jnpa.sl_import_files ADD COLUMN IF NOT EXISTS uploaded_by text;

-- How the file entered the system: the batch directory importer vs a UI upload.
ALTER TABLE jnpa.sl_import_files ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'DIRECTORY';

-- Constrain the new column to the two known ingest paths (idempotent; only added if absent).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sl_import_files_source'
    ) THEN
        ALTER TABLE jnpa.sl_import_files
            ADD CONSTRAINT chk_sl_import_files_source CHECK (source IN ('DIRECTORY', 'UPLOAD'));
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_sl_file_source ON jnpa.sl_import_files (source, id DESC);
