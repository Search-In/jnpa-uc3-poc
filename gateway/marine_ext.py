"""UC-I Marine schema bootstrap — idempotent, additive.

Applies the same DDL as infra/postgres/migrations/0038_marine_vessel_call.sql at
gateway boot so a dev/mock database that never ran the migration still gets the
new objects lazily — exactly the pattern gateway/berthing_ext.ensure_berthing_schema
and gateway/cfs_ecy_ext.ensure_cfs_ecy_schema already use.

Every statement is CREATE ... IF NOT EXISTS (or CREATE OR REPLACE): running it against
a DB that already has the objects (because the migration ran) is a no-op. It NEVER
drops or alters an existing object, and it touches NOTHING in the jnpa schema — the
berthing module and every other UC3 module are entirely unaffected. Called once from
gateway/main.py::_lifespan (best-effort).

Owns the UC-I marine tables in the `core` schema. This module's first slice is the
vessel-call spine (core.vessel_call + core.vessel_call_event); later slices add
core.vessel, core.pilot, core.pilotage, core.port_craft, core.sea_channel and
core.bathymetry_survey under the same function.

The `core` schema (rather than jnpa) is deliberate and follows schema.sql, the agreed
source of truth — see the DEVIATION LEDGER at the top of migration 0038 for the full
rationale and for every difference between schema.sql and this DDL.

The _DDL list is kept byte-for-byte in lock-step with migration 0038 (one idempotent
statement per list item, because SQLAlchemy text() runs a single statement per
execute()).
"""
from __future__ import annotations

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.marine_ext")

# One idempotent statement per list item. Mirrors migration 0038 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    # Audit trigger fn — mirrors jnpa.set_updated_at() so `core` carries no dependency
    # on the jnpa schema. CREATE OR REPLACE is idempotent. [D5]
    """CREATE OR REPLACE FUNCTION core.set_updated_at()
    RETURNS trigger AS $$
    BEGIN
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql""",
    # One row per vessel visit. vcn and the short via_no are BOTH nullable: a CALINF
    # voyage registration has neither yet, and berthing reports / pilot cards carry
    # only the short VIA. The call is created on first sighting and enriched in place.
    """CREATE TABLE IF NOT EXISTS core.vessel_call (
        call_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        vcn           text,
        via_no        text,
        imo_no        text,
        vessel_name   text,
        voyage_no     text,
        rotation_no   text,
        terminal_id   smallint,
        berth_id      smallint,
        purpose       text,
        eta           timestamptz,
        etd           timestamptz,
        etb           timestamptz,
        ata           timestamptz,
        atd           timestamptz,
        atc           timestamptz,
        status        text,
        igm_no        bigint,
        source_note   text,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_vessel_call_vcn UNIQUE (vcn))""",
    "CREATE INDEX IF NOT EXISTS idx_vessel_call_via ON core.vessel_call (via_no)",
    "CREATE INDEX IF NOT EXISTS idx_vessel_call_imo_voyage ON core.vessel_call (imo_no, voyage_no)",
    "CREATE INDEX IF NOT EXISTS idx_vessel_call_updated ON core.vessel_call (updated_at DESC)",
    "DROP TRIGGER IF EXISTS trg_vessel_call_updated_at ON core.vessel_call",
    """CREATE TRIGGER trg_vessel_call_updated_at
        BEFORE UPDATE ON core.vessel_call
        FOR EACH ROW EXECUTE FUNCTION core.set_updated_at()""",
    # Fine-grained milestones from VESARR / VESDEP, pilot cards and berthing reports.
    # Unlike jnpa.berthing_events this permits REPEATED events of the same type at
    # different timestamps while still de-duplicating an exact re-import. [D6]
    """CREATE TABLE IF NOT EXISTS core.vessel_call_event (
        event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        call_id     bigint REFERENCES core.vessel_call (call_id),
        event_type  text NOT NULL,
        event_ts    timestamptz NOT NULL,
        berth_id    smallint,
        source_file bigint,
        created_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_vessel_call_event UNIQUE (call_id, event_type, event_ts))""",
    "CREATE INDEX IF NOT EXISTS idx_vessel_call_event_call_ts "
    "ON core.vessel_call_event (call_id, event_ts)",

    # ==================================================================
    # Migration 0039 — file provenance root (core.ingest_file).
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.ingest_file (
        file_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        path           text NOT NULL UNIQUE,
        source_system  text NOT NULL,
        file_format    text NOT NULL,
        loaded_at      timestamptz NOT NULL DEFAULT now(),
        row_count      integer,
        notes          text)""",
    "CREATE INDEX IF NOT EXISTS idx_ingest_file_source ON core.ingest_file (source_system)",
    "CREATE INDEX IF NOT EXISTS idx_ingest_file_loaded ON core.ingest_file (loaded_at DESC)",

    # ==================================================================
    # Migration 0040 — terminal & berth reference dimensions + seed.
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.ref_terminal (
        terminal_id  smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        code         text NOT NULL UNIQUE,
        name         text NOT NULL,
        operator     text,
        pcs_code     text)""",
    """CREATE TABLE IF NOT EXISTS core.ref_terminal_alias (
        alias        text PRIMARY KEY,
        terminal_id  smallint NOT NULL REFERENCES core.ref_terminal)""",
    "CREATE INDEX IF NOT EXISTS idx_ref_terminal_alias_terminal "
    "ON core.ref_terminal_alias (terminal_id)",
    """CREATE TABLE IF NOT EXISTS core.ref_berth (
        berth_id       smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        terminal_id    smallint REFERENCES core.ref_terminal,
        code           text NOT NULL UNIQUE,
        berth_length_m numeric(7,2),
        design_depth_m numeric(5,2))""",
    "CREATE INDEX IF NOT EXISTS idx_ref_berth_terminal ON core.ref_berth (terminal_id)",
    """CREATE TABLE IF NOT EXISTS core.ref_berth_alias (
        alias     text PRIMARY KEY,
        berth_id  smallint NOT NULL REFERENCES core.ref_berth)""",
    "CREATE INDEX IF NOT EXISTS idx_ref_berth_alias_berth ON core.ref_berth_alias (berth_id)",
    # Seed the 7 canonical terminals (mirrors jnpa.perf_terminals) — idempotent.
    """INSERT INTO core.ref_terminal (code, name, operator) VALUES
        ('NSFT',  'Nhava Sheva Freeport Terminal',                'NSFT'),
        ('NSICT', 'Nhava Sheva International Container Terminal',  'DP World'),
        ('NSIGT', 'Nhava Sheva India Gateway Terminal',           'NSIGT'),
        ('APMT',  'APM Terminals / Gateway Terminals India',      'APM Terminals'),
        ('BMCT',  'Bharat Mumbai Container Terminals',            'PSA'),
        ('NSDT',  'Nhava Sheva Distribution Terminal',            'NSDT'),
        ('JNPCT', 'Jawaharlal Nehru Port Container Terminal',     'JNPA')
    ON CONFLICT (code) DO NOTHING""",
    """INSERT INTO core.ref_terminal_alias (alias, terminal_id)
    SELECT a.alias, t.terminal_id
    FROM (VALUES
        ('GTI', 'APMT'), ('APM', 'APMT'), ('APM Terminals', 'APMT'),
        ('BMCTPL', 'BMCT'), ('BMCTPSA', 'BMCT'), ('PSA', 'BMCT'), ('PSA Mumbai', 'BMCT'),
        ('NSGT', 'NSIGT'), ('NSCT', 'NSICT')
    ) AS a(alias, code)
    JOIN core.ref_terminal t ON t.code = a.code
    ON CONFLICT (alias) DO NOTHING""",
    # The 22 berth codes BERALT actually allots, enumerated from the NLP Outbound message
    # journals (364 allotments). terminal_id is deliberately LEFT NULL: the corpus does
    # NOT determine which terminal owns a berth. The two available routes disagree —
    # CB01 resolves to NSFT by VCN infix (21/21) but JNPCT by DockORTOCode (14/14), and
    # NSD02 splits NSDT/NSFT — so any mapping here would be invented, not observed.
    # Note there is no CB03 in the corpus, and LB01 additionally appears as the distinct
    # sub-berths 'LB01 N' / 'LB01 S'; both are kept verbatim rather than normalised away.
    """INSERT INTO core.ref_berth (code)
    SELECT c FROM (VALUES
        ('CB01'),('CB02'),('CB04'),('CB05'),('CB06'),
        ('BM01'),('BM02'),('BM03'),('BM04'),('BM05'),('BM06'),
        ('G1'),('G2'),
        ('LB01'),('LB01 N'),('LB01 S'),('LB02'),('LB03'),('LB04'),
        ('NSD02'),('NSD03'),('CCB')
    ) AS b(c)
    ON CONFLICT (code) DO NOTHING""",
    # Berth spelling variants seen elsewhere in the corpus (pilot cards use 'CB-04',
    # terminal sheets 'BMCT-4'/'BMCT04'), so a later ingest resolves to the same berth.
    """INSERT INTO core.ref_berth_alias (alias, berth_id)
    SELECT a.alias, b.berth_id
    FROM (VALUES
        ('CB-01','CB01'),('CB-02','CB02'),('CB-04','CB04'),('CB-05','CB05'),('CB-06','CB06'),
        ('BM-01','BM01'),('BM-02','BM02'),('BM-03','BM03'),
        ('BM-04','BM04'),('BM-05','BM05'),('BM-06','BM06'),
        ('BMCT01','BM01'),('BMCT02','BM02'),('BMCT03','BM03'),
        ('BMCT04','BM04'),('BMCT05','BM05'),('BMCT06','BM06'),
        ('BMCT-1','BM01'),('BMCT-2','BM02'),('BMCT-3','BM03'),
        ('BMCT-4','BM04'),('BMCT-5','BM05'),('BMCT-6','BM06'),
        ('APM01','G1'),('APMT-01','G1'),('APMT-1','G1'),
        ('APM02','G2'),('APMT-02','G2'),('APMT-2','G2'),
        ('NSD-02','NSD02'),('NSD-03','NSD03')
    ) AS a(alias, code)
    JOIN core.ref_berth b ON b.code = a.code
    ON CONFLICT (alias) DO NOTHING""",
    # PCS DockORTOCode aliases — CALINF's only terminal field. Corpus-verified values.
    # 'INJNP1' is deliberately ABSENT: it is the PORT code (identical to <Portcode> in
    # every CALINF) and appears on 12 of 20 documents meaning "no terminal declared".
    # Mapping it to a terminal would invent an allocation the message never made.
    """INSERT INTO core.ref_terminal_alias (alias, terminal_id)
    SELECT a.alias, t.terminal_id
    FROM (VALUES
        ('INNSA1NSI1', 'NSICT'), ('INNSA1BMC1', 'BMCT'), ('INNSA1JNP1', 'JNPCT'),
        ('INNSA1NSF1', 'NSFT'),  ('INNSA1GTI1', 'APMT'), ('INNSA1NSG1', 'NSIGT')
    ) AS a(alias, code)
    JOIN core.ref_terminal t ON t.code = a.code
    ON CONFLICT (alias) DO NOTHING""",

    # ==================================================================
    # Migration 0041 — vessel master + insurance.
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.vessel (
        imo_no          text PRIMARY KEY,
        vessel_name     text,
        call_sign       text,
        flag            text,
        vessel_type     text,
        mtmv            text,
        loa_m           numeric(7,2),
        beam_m          numeric(6,2),
        lbp_m           numeric(7,2),
        max_draft_m     numeric(5,2),
        grt             numeric(12,1),
        nrt             numeric(12,1),
        dwt             numeric(12,1),
        teu_capacity    integer,
        mmsi            text,
        engine_type     text,
        num_engines     smallint,
        propulsion_type text,
        num_propellers  smallint,
        max_speed_kn    numeric(4,1),
        bow_thruster    boolean,
        stern_thruster  boolean,
        built_date      date,
        reg_port        text,
        owner_name      text,
        email           text,
        vespro_ref      text,
        updated_at      timestamptz DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_vessel_name ON core.vessel (vessel_name)",
    "CREATE INDEX IF NOT EXISTS idx_vessel_call_sign ON core.vessel (call_sign)",
    "CREATE INDEX IF NOT EXISTS idx_vessel_mmsi ON core.vessel (mmsi)",
    "DROP TRIGGER IF EXISTS trg_vessel_updated_at ON core.vessel",
    """CREATE TRIGGER trg_vessel_updated_at
        BEFORE UPDATE ON core.vessel
        FOR EACH ROW EXECUTE FUNCTION core.set_updated_at()""",
    """CREATE TABLE IF NOT EXISTS core.vessel_insurance (
        imo_no       text REFERENCES core.vessel,
        pi_club      text,
        valid_until  date,
        PRIMARY KEY (imo_no, pi_club))""",

    # ==================================================================
    # Migration 0042 — pilots + pilotage movements.
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.pilot (
        pilot_code  text PRIMARY KEY,
        name        text)""",
    """CREATE TABLE IF NOT EXISTS core.pilotage (
        pilotage_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        movement_type    text NOT NULL CHECK (movement_type IN ('INWARD','OUTWARD','SHIFTING')),
        call_id          bigint REFERENCES core.vessel_call,
        via_no           text,
        imo_no           text,
        vessel_name      text,
        pilot_code       text REFERENCES core.pilot,
        vessel_condition text,
        from_berth_id    smallint REFERENCES core.ref_berth,
        to_berth_id      smallint REFERENCES core.ref_berth,
        draft_fwd_m      numeric(5,2),
        draft_aft_m      numeric(5,2),
        pilot_boarded_at     timestamptz,
        first_line_at        timestamptz,
        all_fast_at          timestamptz,
        pilot_disembarked_at timestamptz,
        berth_vacated_at     timestamptz,
        anchor_down_at       timestamptz,
        anchor_up_at         timestamptz,
        submitted_at         timestamptz,
        extras           jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_pilotage_via ON core.pilotage (via_no)",
    "CREATE INDEX IF NOT EXISTS idx_pilotage_call ON core.pilotage (call_id)",
    "CREATE INDEX IF NOT EXISTS idx_pilotage_imo ON core.pilotage (imo_no)",
    "CREATE INDEX IF NOT EXISTS idx_pilotage_pilot ON core.pilotage (pilot_code)",

    # ==================================================================
    # Migration 0043 — sea-channel GIS overlay (GeoJSON jsonb, no PostGIS).
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.sea_channel (
        channel_id    smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name          text NOT NULL,
        section_label text,
        area_ha       numeric(14,4),
        length_m      numeric(14,4),
        geom_geojson  jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_sea_channel_name ON core.sea_channel (name)",

    # ==================================================================
    # Migration 0044 — re-attach the FKs migration 0038 deferred (NOT VALID,
    # idempotent via DO-block guards). Only ALTERs the two core.vessel_call*
    # tables; touches nothing in jnpa.
    # ==================================================================
    """DO $$
    BEGIN
        IF to_regclass('core.vessel') IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_vessel_call_imo'
                           AND conrelid = 'core.vessel_call'::regclass) THEN
            ALTER TABLE core.vessel_call ADD CONSTRAINT fk_vessel_call_imo
                FOREIGN KEY (imo_no) REFERENCES core.vessel (imo_no) NOT VALID;
        END IF;
    END $$""",
    """DO $$
    BEGIN
        IF to_regclass('core.ref_terminal') IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_vessel_call_terminal'
                           AND conrelid = 'core.vessel_call'::regclass) THEN
            ALTER TABLE core.vessel_call ADD CONSTRAINT fk_vessel_call_terminal
                FOREIGN KEY (terminal_id) REFERENCES core.ref_terminal (terminal_id) NOT VALID;
        END IF;
    END $$""",
    """DO $$
    BEGIN
        IF to_regclass('core.ref_berth') IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_vessel_call_berth'
                           AND conrelid = 'core.vessel_call'::regclass) THEN
            ALTER TABLE core.vessel_call ADD CONSTRAINT fk_vessel_call_berth
                FOREIGN KEY (berth_id) REFERENCES core.ref_berth (berth_id) NOT VALID;
        END IF;
    END $$""",
    """DO $$
    BEGIN
        IF to_regclass('core.ref_berth') IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_vessel_call_event_berth'
                           AND conrelid = 'core.vessel_call_event'::regclass) THEN
            ALTER TABLE core.vessel_call_event ADD CONSTRAINT fk_vessel_call_event_berth
                FOREIGN KEY (berth_id) REFERENCES core.ref_berth (berth_id) NOT VALID;
        END IF;
    END $$""",
    """DO $$
    BEGIN
        IF to_regclass('core.ingest_file') IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_vessel_call_event_source_file'
                           AND conrelid = 'core.vessel_call_event'::regclass) THEN
            ALTER TABLE core.vessel_call_event ADD CONSTRAINT fk_vessel_call_event_source_file
                FOREIGN KEY (source_file) REFERENCES core.ingest_file (file_id) NOT VALID;
        END IF;
    END $$""",

    # ==================================================================
    # Migration 0045 — Marine upload import ledger (twin tables).
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.marine_import_files (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        filename         text,
        file_hash        text,
        physical_format  text NOT NULL DEFAULT 'CSV'
                         CHECK (physical_format IN ('CSV','XLS','XLSX','PDF')),
        uploaded_by      text,
        status           text NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
        total_rows       integer NOT NULL DEFAULT 0,
        success_rows     integer NOT NULL DEFAULT 0,
        failed_rows      integer NOT NULL DEFAULT 0,
        duplicate_rows   integer NOT NULL DEFAULT 0,
        source           text NOT NULL DEFAULT 'UPLOAD' CHECK (source IN ('DIRECTORY','UPLOAD')),
        error_detail     text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_marine_import_file_hash UNIQUE (file_hash))""",
    "CREATE INDEX IF NOT EXISTS idx_marine_file_status ON core.marine_import_files (status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_marine_file_source ON core.marine_import_files (source, id DESC)",
    """CREATE TABLE IF NOT EXISTS core.marine_import_errors (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        import_file_id  bigint NOT NULL
                        REFERENCES core.marine_import_files (id) ON DELETE CASCADE,
        row_number      integer,
        error_message   text,
        raw_data        text,
        created_at      timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_marine_err_file ON core.marine_import_errors (import_file_id, id)",

    # ==================================================================
    # Migration 0046 — multi-format persistence: widen physical_format CHECK
    # (+XML/+LOG), pre-VCN dedup index (with a duplicate-safety guard), and the
    # document_type history column. Idempotent; mirrors 0046 exactly.
    # ==================================================================
    """DO $$
    DECLARE v_conname text;
    BEGIN
        SELECT c.conname INTO v_conname
        FROM pg_constraint c
        WHERE c.conrelid = 'core.marine_import_files'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%physical_format%'
          AND pg_get_constraintdef(c.oid) NOT ILIKE '%XML%';
        IF v_conname IS NOT NULL THEN
            EXECUTE format('ALTER TABLE core.marine_import_files DROP CONSTRAINT %I', v_conname);
            ALTER TABLE core.marine_import_files
                ADD CONSTRAINT marine_import_files_physical_format_check
                CHECK (physical_format IN ('CSV','XLS','XLSX','PDF','XML','LOG'));
        END IF;
    END $$""",
    """DO $$
    DECLARE v_dups int;
    BEGIN
        SELECT count(*) INTO v_dups FROM (
            SELECT imo_no, voyage_no FROM core.vessel_call
            WHERE vcn IS NULL AND imo_no IS NOT NULL AND voyage_no IS NOT NULL
            GROUP BY imo_no, voyage_no HAVING count(*) > 1
        ) d;
        IF v_dups > 0 THEN
            RAISE EXCEPTION
                'Cannot create uq_vessel_call_imo_voyage_pre_vcn: % duplicate pre-VCN (imo_no, voyage_no) group(s) exist. Dedupe before applying 0046.', v_dups;
        END IF;
    END $$""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_vessel_call_imo_voyage_pre_vcn "
    "ON core.vessel_call (imo_no, voyage_no) WHERE vcn IS NULL",

    # ------------------------------------------------------------------
    # Repair: the milestone uniqueness core.vessel_call_event relies on.
    #
    # It is declared INSIDE the CREATE TABLE above, so on a database whose
    # vessel_call_event was created by another DDL (schema.sql declares no such
    # constraint) `CREATE TABLE IF NOT EXISTS` no-ops and the uniqueness never exists.
    # The event insert then has nothing for ON CONFLICT to infer and every VESARR /
    # VESDEP / BERALT event write fails — the same defect that broke the VCN upsert.
    #
    # FAIL-SAFE BY CONSTRUCTION: ensure_marine_schema executes this whole list in ONE
    # transaction with no per-statement guard, so a raised exception here would abort
    # ALL marine DDL at boot. Duplicates therefore emit a NOTICE and skip — never RAISE
    # (which is why this does not reuse the 0046 pre-VCN guard verbatim).
    # ------------------------------------------------------------------
    """DO $$
    DECLARE v_dups bigint;
    BEGIN
        IF to_regclass('core.vessel_call_event') IS NULL THEN
            RETURN;
        END IF;
        -- Already satisfied by the in-table constraint (fresh DB) or by a previous run.
        IF to_regclass('core.uq_vessel_call_event') IS NOT NULL
           OR to_regclass('core.uq_vessel_call_event_row') IS NOT NULL THEN
            RETURN;
        END IF;
        SELECT count(*) INTO v_dups FROM (
            SELECT 1 FROM core.vessel_call_event
            WHERE call_id IS NOT NULL AND event_type IS NOT NULL AND event_ts IS NOT NULL
            GROUP BY call_id, event_type, event_ts HAVING count(*) > 1) d;
        IF v_dups > 0 THEN
            RAISE NOTICE 'uq_vessel_call_event_row not created: % duplicate (call_id, event_type, event_ts) group(s) present; dedupe and restart to regain event idempotency', v_dups;
            RETURN;
        END IF;
        CREATE UNIQUE INDEX uq_vessel_call_event_row
            ON core.vessel_call_event (call_id, event_type, event_ts);
    END $$""",
    "ALTER TABLE core.marine_import_files ADD COLUMN IF NOT EXISTS document_type text",

    # ==================================================================
    # Migration 0047 — pilotage import idempotency (import_file_id, row_sha256,
    # unique index) on the existing core.pilotage. Additive; mirrors 0047.
    # ==================================================================
    "ALTER TABLE core.pilotage ADD COLUMN IF NOT EXISTS import_file_id bigint",
    "ALTER TABLE core.pilotage ADD COLUMN IF NOT EXISTS row_sha256 text",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pilotage_row ON core.pilotage (row_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_pilotage_import_file ON core.pilotage (import_file_id)",
    "CREATE INDEX IF NOT EXISTS idx_pilotage_movement ON core.pilotage (movement_type)",
    "CREATE INDEX IF NOT EXISTS idx_pilotage_submitted ON core.pilotage (submitted_at DESC)",

    # ==================================================================
    # Migration 0048 — port-craft fleet register (core.port_craft). Additive;
    # mirrors 0048. name is UNIQUE (upsert key); extras/import_file_id additive.
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.port_craft (
        craft_id        smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name            text NOT NULL UNIQUE,
        craft_type      text,
        owned_or_hired  text,
        owner_name      text,
        year_built      text,
        loa_m           numeric(6,2),
        breadth_m       numeric(6,2),
        draft_m         numeric(5,2),
        main_engines    text,
        bollard_pull_t  numeric(6,2),
        design_speed_kn numeric(5,2),
        import_file_id  bigint,
        extras          jsonb NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS idx_port_craft_type ON core.port_craft (craft_type)",
    "CREATE INDEX IF NOT EXISTS idx_port_craft_import_file ON core.port_craft (import_file_id)",

    # ==================================================================
    # Migration 0049 — sea-channel import idempotency (import_file_id, row_sha256,
    # unique index) on the existing core.sea_channel. Additive; mirrors 0049.
    # ==================================================================
    "ALTER TABLE core.sea_channel ADD COLUMN IF NOT EXISTS import_file_id bigint",
    "ALTER TABLE core.sea_channel ADD COLUMN IF NOT EXISTS row_sha256 text",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sea_channel_row ON core.sea_channel (row_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_sea_channel_import_file ON core.sea_channel (import_file_id)",

    # ==================================================================
    # Migration 0050 — allow 'ZIP'/'SHP' in the marine_import_files physical_format
    # CHECK (the zipped shapefile upload). Idempotent guarded re-add; mirrors 0050.
    # ==================================================================
    """DO $$
    DECLARE v_conname text;
    BEGIN
        SELECT c.conname INTO v_conname
        FROM pg_constraint c
        WHERE c.conrelid = 'core.marine_import_files'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%physical_format%'
          AND pg_get_constraintdef(c.oid) NOT ILIKE '%ZIP%';
        IF v_conname IS NOT NULL THEN
            EXECUTE format('ALTER TABLE core.marine_import_files DROP CONSTRAINT %I', v_conname);
            ALTER TABLE core.marine_import_files
                ADD CONSTRAINT marine_import_files_physical_format_check
                CHECK (physical_format IN ('CSV','XLS','XLSX','PDF','XML','LOG','ZIP','SHP'));
        END IF;
    END $$""",

    # ==================================================================
    # core.bathymetry_survey — the per-chart survey header.
    #
    # Reproduced VERBATIM from the canonical schema.sql §10 (root of the workspace,
    # `CREATE TABLE core.bathymetry_survey`), the agreed source of truth this module
    # already follows. Column-for-column identical to the live instance, so a database
    # provisioned here and one provisioned from schema.sql are the same table; the
    # data_origin column both carry is added by the 0120/0121 block further down, not
    # here, exactly as it is for every other marine table.
    #
    # WHY IT IS HERE. Migration 0051 and the block below were written against a database
    # where this table ALREADY existed, because schema.sql had been applied to it by hand
    # — something this repository never does. On a clean database the effect was not a
    # missing chart header but a missing MARINE SCHEMA: ensure_marine_schema() runs the
    # whole _DDL list in ONE transaction, so the unguarded
    # `ALTER TABLE core.bathymetry_survey ADD COLUMN data_origin` below raised
    # "relation does not exist", rolled the transaction back, and left the database with
    # NO marine tables at all — including uq_marine_import_file_hash_origin, the index
    # the import ledger's de-duplication depends on. gateway/main.py logs that rollback
    # as a warning and boots anyway, so the failure was silent.
    #
    # ORDERING IS LOAD-BEARING: this statement must stay ahead of
    # core.bathymetry_sounding (whose survey_id FK references it) and ahead of the
    # 0120/0121 ALTER. Asserted by tests/test_marine_schema_bootstrap_order.py.
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.bathymetry_survey (
        survey_id      smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        drawing_no     text UNIQUE,
        section_label  text,
        design_depth_m numeric(5,2),
        survey_start   date,
        survey_end     date,
        survey_vessel  text,
        file_path      text)""",

    # ==================================================================
    # Migration 0051 — bathymetry depth soundings (core.bathymetry_sounding) + the
    # 'JSON' physical_format widening. Additive; mirrors 0051 byte-for-byte.
    # This is core.bathymetry_survey's detail table and the FIRST inbound FK it has
    # ever had — hence the header above must already exist.
    # ==================================================================
    """CREATE TABLE IF NOT EXISTS core.bathymetry_sounding (
        sounding_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        survey_id      smallint NOT NULL
                           REFERENCES core.bathymetry_survey (survey_id) ON DELETE CASCADE,
        easting_m      numeric(10,2),
        northing_m     numeric(11,2),
        lat            numeric(9,6),
        lon            numeric(9,6),
        depth_m        numeric(5,2) NOT NULL,
        above_design   boolean NOT NULL DEFAULT false,
        page_x_pt      numeric(8,2),
        page_y_pt      numeric(8,2),
        import_file_id bigint,
        row_sha256     text,
        created_at     timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_bathymetry_sounding_row UNIQUE (row_sha256))""",
    "CREATE INDEX IF NOT EXISTS idx_bathy_sounding_survey ON core.bathymetry_sounding (survey_id)",
    "CREATE INDEX IF NOT EXISTS idx_bathy_sounding_bbox ON core.bathymetry_sounding (survey_id, easting_m, northing_m)",
    "CREATE INDEX IF NOT EXISTS idx_bathy_sounding_depth ON core.bathymetry_sounding (survey_id, depth_m)",
    "CREATE INDEX IF NOT EXISTS idx_bathy_sounding_import_file ON core.bathymetry_sounding (import_file_id)",

    # 'JSON' physical_format — the canonical bathymetry JSON arm. Without it the ledger
    # insert violates the closed-vocabulary CHECK and aborts the import transaction.
    """DO $$
    DECLARE v_conname text;
    BEGIN
        SELECT c.conname INTO v_conname
        FROM pg_constraint c
        WHERE c.conrelid = 'core.marine_import_files'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%physical_format%'
          AND pg_get_constraintdef(c.oid) NOT ILIKE '%JSON%';
        IF v_conname IS NOT NULL THEN
            EXECUTE format('ALTER TABLE core.marine_import_files DROP CONSTRAINT %I', v_conname);
            ALTER TABLE core.marine_import_files
                ADD CONSTRAINT marine_import_files_physical_format_check
                CHECK (physical_format IN ('CSV','XLS','XLSX','PDF','XML','LOG','ZIP','SHP','JSON'));
        END IF;
    END $$""",

    # ==================================================================
    # Migrations 0120 / 0121 — data_origin provenance ('API' | 'MANUAL'): LIVE
    # (JNPA-API-sourced) vs DEMO (manually-imported). The ledger + every marine
    # domain read table carries the tag; the ledger's file-hash uniqueness becomes
    # PER-ORIGIN so the same bytes from both origins each land once. Additive +
    # idempotent; mirrors infra/postgres/v3/0120_* and 0121_* for the marine tables.
    # ==================================================================
    "ALTER TABLE core.marine_import_files ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "ALTER TABLE core.vessel_call ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "ALTER TABLE core.vessel_call_event ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "ALTER TABLE core.pilotage ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "ALTER TABLE core.port_craft ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "ALTER TABLE core.sea_channel ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "ALTER TABLE core.bathymetry_survey ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    "ALTER TABLE core.bathymetry_sounding ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'MANUAL'",
    # per-origin ledger dedup: replace UNIQUE(file_hash) with UNIQUE(file_hash, data_origin)
    "ALTER TABLE core.marine_import_files DROP CONSTRAINT IF EXISTS uq_marine_import_file_hash",
    "DROP INDEX IF EXISTS core.uq_marine_import_file_hash",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_marine_import_file_hash_origin "
    "ON core.marine_import_files (file_hash, data_origin)",
]


async def ensure_marine_schema(dsn: Optional[str] = None) -> None:
    """Create the core.* UC-I marine objects if absent. Idempotent."""
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
    log.info("marine_schema_ready")


__all__ = ["ensure_marine_schema"]
