-- 0141  ICD daily report — FPD pendency + rake movements.  GAP-ETL-04 / GAP-ETL-07
--
-- The 14 ICD daily-report PDFs were ledgered UNSUPPORTED_FORMAT and never read.
-- They are the corpus's only daily rail-pendency series: 7 terminals x 3
-- carrier series x ~30 destination codes x 14 days.
--
-- Purely additive: two new tables, no change to any existing object.

CREATE TABLE IF NOT EXISTS core.icd_fpd_pendency (
    id             bigserial PRIMARY KEY,
    import_file_id bigint,
    report_date    date        NOT NULL,
    terminal       text        NOT NULL,   -- NSFT | NSDT | NSICT | NSIGT | GTICT | BMCT | JNPORT
    series         text        NOT NULL,   -- CONCOR | OTHER_CARRIER | TOTAL
    fpd_code       text        NOT NULL,   -- TKD, MBD, ... plus OTHR and the printed TOTAL
    teu            integer     NOT NULL,
    source_file    text,
    page_no        integer,
    -- Provenance vocabulary, as everywhere else: these are JNPA documents.
    data_origin    text        NOT NULL DEFAULT 'REAL',
    extra          jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_icd_fpd_pendency UNIQUE (report_date, terminal, series, fpd_code)
);

COMMENT ON TABLE core.icd_fpd_pendency IS
  'Destination(FPD)-wise pendency in TEUs from the ICD daily report PDFs. '
  'Values are stored EXACTLY as printed. 20 of 2,940 cells do not satisfy '
  'CONCOR + OTHER_CARRIER = TOTAL — a defect in the source report (always the '
  'PDD column at NSICT/GTICT); the import ledger carries a warning per cell.';

CREATE INDEX IF NOT EXISTS idx_icd_fpd_pendency_date
    ON core.icd_fpd_pendency (report_date DESC);
CREATE INDEX IF NOT EXISTS idx_icd_fpd_pendency_terminal
    ON core.icd_fpd_pendency (terminal, report_date DESC);

CREATE TABLE IF NOT EXISTS core.icd_rake_movement (
    id             bigserial PRIMARY KEY,
    import_file_id bigint,
    report_date    date        NOT NULL,
    rake_id        text        NOT NULL,   -- R261746
    track          text,                   -- T1 | T2
    placed_at      timestamptz,            -- resolved from day-of-month + report month
    placed_raw     text,                   -- as printed, e.g. "30 13:10"
    discharge      jsonb,                  -- {"N": 8, "NG": 20, "G": 9, "B": 28, "NF": 7}
    source_file    text,
    page_no        integer,
    data_origin    text        NOT NULL DEFAULT 'REAL',
    extra          jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_icd_rake_movement UNIQUE (report_date, rake_id, track, placed_raw)
);

COMMENT ON TABLE core.icd_rake_movement IS
  'Rake placement and discharge composition from the ICD daily report PDFs. '
  'The source prints only a day-of-month for placement; the month and year come '
  'from the report date, and a placement dated after the report date is read as '
  'the previous month (rakes placed late on the preceding day).';

CREATE INDEX IF NOT EXISTS idx_icd_rake_movement_date
    ON core.icd_rake_movement (report_date DESC);
CREATE INDEX IF NOT EXISTS idx_icd_rake_movement_rake
    ON core.icd_rake_movement (rake_id);

-- Added after the first apply: the shared rail persist path writes an `extra`
-- envelope for every feed, and this table was defined without one.
ALTER TABLE core.icd_rake_movement ADD COLUMN IF NOT EXISTS extra jsonb;
