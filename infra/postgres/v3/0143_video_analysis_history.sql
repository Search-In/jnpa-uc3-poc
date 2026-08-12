-- ============================================================
-- 0143  core.video_analysis — durable Video Analytics history
--
-- The Video Analytics workbench listed only the analyses held in the gateway
-- PROCESS (an in-memory OrderedDict): the history vanished on every gateway
-- restart, on every container restart, and was invisible to any other worker.
-- Operators reported "the history is missing" because, past a restart, it was.
--
-- This table is the durable record of the analyses THIS system performed. It is
-- the gateway's own operational metadata — what was uploaded, when, by which
-- account, against which camera, what the vendor answered — NOT a copy of the
-- vendor's detections.
--
-- SCOPE — OPERATIONAL METADATA ONLY. Deliberately absent, and to stay absent:
--   * face embeddings / face templates
--   * face or person crops, thumbnails, any image bytes
--   * person identities, names or face-similarity scores (the I-07 analyser's
--     person payload)
-- Those carry a DPDP retention decision that has not been made, so none of it is
-- written here. Person-centric results stay where they are today: fetched live
-- from SecureVision per analysis, held nowhere. Because this table holds no
-- personal data, it needs no retention rule beyond ordinary operational history.
--
-- Deletes are SOFT (deleted_at): removing the analysis upstream must not erase
-- the audit trail that it existed and was deleted. The list endpoint hides
-- soft-deleted rows by default, which is exactly the behaviour the in-memory
-- registry had.
--
-- Additive: no existing table, column or row is touched.
-- Rollback:  DROP TABLE IF EXISTS core.video_analysis;
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS core.video_analysis (
    analysis_id              text PRIMARY KEY,
    -- camera attribution
    securevision_camera_code text,
    jnpa_camera_id           text,
    camera_mapped            boolean     NOT NULL DEFAULT false,
    -- clip + analysis outcome (operational only)
    filename                 text,
    frames_sampled           integer,
    detection_pass_count     integer,
    zones_loaded             integer,
    status                   text        NOT NULL DEFAULT 'COMPLETED',
    processing_ms            integer,
    source                   text        NOT NULL DEFAULT 'securevision',
    -- audit
    uploaded_by              text,
    uploaded_at              timestamptz NOT NULL DEFAULT now(),
    deleted_at               timestamptz,
    created_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_video_analysis_status
        CHECK (status IN ('COMPLETED', 'FAILED', 'DELETED'))
);

COMMENT ON TABLE core.video_analysis IS
    'Video Analytics history: operational metadata for clips analysed through '
    'this gateway. Contains NO biometric, face or person-recognition data.';

-- Newest-first listing (the workbench default) and camera-scoped history.
CREATE INDEX IF NOT EXISTS idx_video_analysis_uploaded_at
    ON core.video_analysis (uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_analysis_camera
    ON core.video_analysis (jnpa_camera_id, uploaded_at DESC);
-- Partial index over the rows the default listing actually scans.
CREATE INDEX IF NOT EXISTS idx_video_analysis_live
    ON core.video_analysis (uploaded_at DESC)
    WHERE deleted_at IS NULL;

COMMIT;
