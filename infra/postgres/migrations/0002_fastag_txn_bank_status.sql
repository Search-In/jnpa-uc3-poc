-- ===========================================================================
-- Migration 0002 — add provider batch-level fields to fastag_transactions.
--
-- The RC->FASTag Transaction provider payload carries two batch-level (data-level)
-- fields not previously modelled: `bank_name` and `status`. They are confirmed in
-- the provider sample document. Added as nullable columns so existing rows and the
-- existing transaction storage are unaffected.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). Apply to an existing database:
--   psql "$POSTGRES_DSN_PSQL" -v ON_ERROR_STOP=1 \
--        -f infra/postgres/migrations/0002_fastag_txn_bank_status.sql
-- ===========================================================================

ALTER TABLE jnpa.fastag_transactions ADD COLUMN IF NOT EXISTS bank_name text;
ALTER TABLE jnpa.fastag_transactions ADD COLUMN IF NOT EXISTS status    text;
