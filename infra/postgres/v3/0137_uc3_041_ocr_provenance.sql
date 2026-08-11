-- 0137 — UC3-041: make an OCR record's provenance survive verification.
--
-- The defect this fixes: POST /api/ocr/documents/{id}/verify merged an
-- operator's field corrections into core.document_ocr.fields but left `source`
-- and `confidence` untouched. A record whose values had been supplied by a human
-- therefore kept reporting source='MOCK' at confidence 0.75 — and, worse, any
-- MOCK field the operator did NOT correct survived under a VERIFIED status,
-- badged identically to the corrected ones. Mock output was being carried
-- forward as though a real read had produced it.
--
-- The fix needs somewhere to record WHO supplied WHICH values, so:
--
--   corrected_fields  the keys the operator actually overwrote. A field in this
--                     list is human-supplied; a field absent from it still comes
--                     from whatever rung `source` names. That distinction is the
--                     whole point — without it the two are indistinguishable
--                     once they share a row.
--   verified_by       the operator who verified.
--   verified_at       when.
--
-- `source` keeps its existing vocabulary (OCR_SERVICE / OCR / MOCK) and still
-- describes the EXTRACTION rung, which is a historical fact and must not be
-- rewritten by a later verification. The verification is recorded alongside it
-- rather than on top of it.
--
-- Fully ADDITIVE and idempotent.
BEGIN;

ALTER TABLE core.document_ocr
    ADD COLUMN IF NOT EXISTS corrected_fields jsonb,
    ADD COLUMN IF NOT EXISTS verified_by      text,
    ADD COLUMN IF NOT EXISTS verified_at      timestamptz;

COMMENT ON COLUMN core.document_ocr.corrected_fields IS
    'Field keys an operator overwrote at verification. A key listed here is '
    'human-supplied; a key not listed still originates from the rung named by '
    '"source". Without this the two are indistinguishable in the same jsonb.';

COMMENT ON COLUMN core.document_ocr.source IS
    'The EXTRACTION rung that produced the fields: OCR_SERVICE (Tesseract via '
    'ingest/eir_ocr, real), OCR (in-process Tesseract, real) or MOCK '
    '(deterministic stand-in, never a real read). A historical fact — verification '
    'records itself in corrected_fields/verified_by and never rewrites this.';

COMMIT;
