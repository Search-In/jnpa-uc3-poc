-- ============================================================================
-- 0139 — Email ingestion ledger (UC3 Email Processing page)
--
-- Tracks emails read from the configured admin mailbox (subject prefix "JNPA")
-- and the outcome of processing each one. Modelled on the existing import
-- ledgers (core.gate_doc_import_file / core.rail_import_file, 0112 / 0119): a
-- file-level row with counters + a status enum, plus a child error table.
--
-- SCOPE — this migration adds ONLY the ledger. It creates no master table and
-- alters none: processing routes attachments into the EXISTING masters through
-- the existing upload services (core.vessel_call, core.pilotage, core.port_craft,
-- core.eir, core.pin_ticket, core.gate_capture, ...).
--
-- SECURITY — no mailbox credential is stored here. The IMAP host/user/password
-- live in environment variables only (EMAIL_HOST / EMAIL_USER / EMAIL_PASSWORD),
-- and nothing in this schema can hold them. Attachment BYTES are deliberately
-- not persisted either: they are re-read from the mailbox on demand, so a
-- compromised database never yields message content.
-- ============================================================================
BEGIN;

-- ------------------------------------------------------------------ messages
CREATE TABLE IF NOT EXISTS core.email_message (
    id                  bigserial PRIMARY KEY,
    -- RFC-822 Message-ID. The idempotency key: re-polling the mailbox must never
    -- create a second row for the same email (same role as source_sha256 in
    -- core.gate_doc_import_file).
    message_id          text NOT NULL,
    -- IMAP UID + the mailbox it was seen in, so attachment bytes can be re-read
    -- on demand without persisting them here.
    imap_uid            text,
    mailbox             text NOT NULL DEFAULT 'INBOX',
    subject             text,
    sender              text,
    recipients          text,
    cc                  text,
    received_at         timestamptz,
    -- Short plain-text summary for the list view; body_text is the full body
    -- shown on the detail view.
    body_preview        text,
    body_text           text,
    attachment_count    integer NOT NULL DEFAULT 0,
    processing_status   text NOT NULL DEFAULT 'UNPROCESSED'
                        CHECK (processing_status IN
                               ('UNPROCESSED','PROCESSING','PROCESSED','FAILED','NEEDS_REVIEW')),
    -- Outcome of the last Process run.
    detected_type       text,
    target_master_table text,
    records_detected    integer NOT NULL DEFAULT 0,
    records_imported    integer NOT NULL DEFAULT 0,
    records_failed      integer NOT NULL DEFAULT 0,
    error_detail        text,
    processed_at        timestamptz,
    processed_by        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_email_message_msgid UNIQUE (message_id)
);
CREATE INDEX IF NOT EXISTS idx_email_message_status
    ON core.email_message (processing_status, id DESC);
CREATE INDEX IF NOT EXISTS idx_email_message_received
    ON core.email_message (received_at DESC);

-- --------------------------------------------------------------- attachments
CREATE TABLE IF NOT EXISTS core.email_attachment (
    id                  bigserial PRIMARY KEY,
    email_id            bigint NOT NULL
                        REFERENCES core.email_message(id) ON DELETE CASCADE,
    filename            text NOT NULL,
    content_type        text,
    size_bytes          bigint NOT NULL DEFAULT 0,
    -- SHA-256 of the attachment bytes. Reuses the same duplicate-detection idea
    -- the upload ledgers already apply to uploaded files.
    sha256              text,
    -- Filled by the classifier on Process.
    detected_format     text,
    detected_document_type text,
    target_master_table text,
    process_status      text NOT NULL DEFAULT 'UNPROCESSED'
                        CHECK (process_status IN
                               ('UNPROCESSED','PROCESSED','FAILED','NEEDS_REVIEW','UNSUPPORTED')),
    records_detected    integer NOT NULL DEFAULT 0,
    records_imported    integer NOT NULL DEFAULT 0,
    records_failed      integer NOT NULL DEFAULT 0,
    error_detail        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_email_attachment_email
    ON core.email_attachment (email_id, id);
-- One row per distinct attachment per email: re-polling the same message must
-- not duplicate its attachment rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_attachment_email_sha
    ON core.email_attachment (email_id, sha256) WHERE sha256 IS NOT NULL;

-- -------------------------------------------------------------------- errors
CREATE TABLE IF NOT EXISTS core.email_processing_error (
    id             bigserial PRIMARY KEY,
    email_id       bigint NOT NULL REFERENCES core.email_message(id) ON DELETE CASCADE,
    attachment_id  bigint REFERENCES core.email_attachment(id) ON DELETE CASCADE,
    record_ref     text,
    error_code     text NOT NULL,
    error_detail   text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_email_proc_error_email
    ON core.email_processing_error (email_id, id);

COMMIT;
