-- ============================================================================
-- 0117_gate_capture_evidence.sql — evidence object reference on gate captures
-- ----------------------------------------------------------------------------
-- Audit finding G-2: core.gate_capture held 808 rows carrying document metadata
-- only. No row referenced the stored evidence object, so GET /api/evidence/{path}
-- (gateway/routers/evidence.py — the same-origin MinIO proxy) had nothing in the
-- database to point it at: the upload -> OCR -> EVIDENCE ARTEFACT leg of the gate
-- document workflow could not be resolved from stored data.
--
-- This adds the missing reference, following the pattern already used by
-- gateway/routers/violations.py (_store_evidence), which stores the frame under
-- the `evidence` bucket and hands back the gateway proxy path
-- "/api/evidence/{object_name}" — never the internal minio:9000 URL, which a
-- browser cannot reach.
--
--   object_path   the bucket-relative object key, e.g. "form13/F13000001.jpg".
--                 This is EXACTLY the value GET /api/evidence/{object_path}
--                 expects, so a stored capture is directly resolvable.
--   evidence_uri  the client-facing URL ("/api/evidence/<object_path>"), stored
--                 so a dashboard/report can render the link without rebuilding it.
--   object_name   the original filename as supplied, kept for provenance.
--
-- Fully ADDITIVE and backward compatible: three NULLable columns, one partial
-- index, no column dropped, no CHECK changed, no existing row invalidated. Rows
-- written before this migration keep NULL (= "no evidence object on record").
-- Idempotent — safe to re-run.
-- ============================================================================
BEGIN;

ALTER TABLE core.gate_capture
    ADD COLUMN IF NOT EXISTS object_path  text,
    ADD COLUMN IF NOT EXISTS evidence_uri text,
    ADD COLUMN IF NOT EXISTS object_name  text;

COMMENT ON COLUMN core.gate_capture.object_path IS
    'Bucket-relative evidence object key; the path segment of GET /api/evidence/{object_path}.';
COMMENT ON COLUMN core.gate_capture.evidence_uri IS
    'Client-facing gateway proxy URL for the evidence object (/api/evidence/<object_path>).';
COMMENT ON COLUMN core.gate_capture.object_name IS
    'Original filename of the uploaded evidence, kept for provenance.';

-- Partial: only captures that actually carry an object are indexed, so the index
-- stays small on a table that is mostly document-only captures.
CREATE INDEX IF NOT EXISTS idx_gate_capture_object_path
    ON core.gate_capture (object_path)
    WHERE object_path IS NOT NULL;

COMMIT;
