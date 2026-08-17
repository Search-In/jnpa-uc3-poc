-- 0143  Container free-time allowance.  GAP-FLOW-05 / flow F-05
--
-- What F-05 asked for was a CHARGE clock: free-day expiry driving a cost.
-- That cannot be built from this corpus, and the reason is worth stating in the
-- schema rather than only in a ticket:
--
--   * No tariff exists anywhere. Not one file carries a demurrage or detention
--     RATE — searched across all 449. Without a rate there is no amount, and a
--     screen showing a rupee figure would be showing a number we invented.
--   * No commencement RULE is stated either. Whether free time runs from
--     discharge, from entry inwards, or from out-of-charge changes every figure,
--     and no supplied document says which.
--
-- What the corpus DOES carry is the ALLOWANCE, written by the shipper into the
-- free-text goods description: "14 FREE DAYS AT POD", "14 DAYS FREE TIME
-- COMBINED DEMURRAGE AND DETENTION". Measured 17-Aug: 250 of 4,276 IGM lines
-- state one — 627 of 12,235 containers, 5.1% — with 14 days dominant (209
-- lines), then 21 (33), 15 (5), 12 and 4.
--
-- So this table holds the allowance and its evidence, and the clock built on it
-- reports DAYS USED AGAINST THE ALLOWANCE, never money. The commencement basis
-- is recorded per row so the figure can be recomputed if JNPA states a different
-- rule.
--
-- Purely additive: one new table.

CREATE TABLE IF NOT EXISTS core.container_free_time (
    id              bigserial PRIMARY KEY,
    container_no    text        NOT NULL,
    igm_no          bigint,
    line_no         integer,

    free_days       integer     NOT NULL,
    -- The exact substring the number was read from, kept verbatim. This is an
    -- extraction from prose, not a field: the phrase is the evidence, and
    -- without it nobody can check the number.
    extracted_from  text        NOT NULL,

    -- Which timestamp the clock starts from, named rather than assumed.
    commencement_basis text     NOT NULL DEFAULT 'IGM_ENTRY_INWARD',
    commenced_at    timestamptz,

    provenance      text        NOT NULL DEFAULT 'DOCUMENT_EVIDENCED',
    data_origin     text        NOT NULL DEFAULT 'REAL',
    source_file     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_container_free_time UNIQUE (container_no, igm_no, line_no)
);

COMMENT ON TABLE core.container_free_time IS
  'Free-day allowance per container, extracted from the free-text goods '
  'description on the IGM line (no structured field exists). Covers ~5% of '
  'containers — the rest state no term. NO TARIFF EXISTS in the corpus, so a '
  'charge cannot be computed from this; the clock reports days used against the '
  'allowance only.';

CREATE INDEX IF NOT EXISTS idx_container_free_time_container
    ON core.container_free_time (container_no);
CREATE INDEX IF NOT EXISTS idx_container_free_time_igm
    ON core.container_free_time (igm_no, line_no);
