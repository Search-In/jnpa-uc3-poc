"""Customs module schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/migrations/0031_customs.sql at gateway
boot so a dev/mock database that never ran the migration still gets the customs
tables + view lazily — exactly the pattern gateway/uc3_ext.ensure_uc3_schema and
gateway/cfs_ecy_ext.ensure_cfs_ecy_schema already use (the gateway image does not
ship infra/, so the DDL is embedded here rather than read from the .sql file).

Every statement is CREATE ... IF NOT EXISTS / CREATE OR REPLACE VIEW: running it
against a DB that already has the objects (because the migration ran) is a no-op.
It DROPS/ALTERS nothing existing and touches no cargo / gate / auth tables — the
customs rows soft-link to core.cargo BY VALUE (container_no), never by FK.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs).
Also reused by scripts/import_customs.py so the importer is self-contained.

The _DDL list below MUST stay in lock-step with migration 0031; the test
tests/test_customs_schema.py asserts both define the same table/view set.
"""
from __future__ import annotations

import os

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.customs_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0031 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS core",
    # -------------------------------------------------- import ledger / envelope
    """CREATE TABLE IF NOT EXISTS core.customs_message (
        id               bigserial PRIMARY KEY,
        message_type     text NOT NULL
                         CHECK (message_type IN ('CHPOI03','CHPOI10','CHPOI13','RMS','LEO','SHIPPING_BILL')),
        module           text NOT NULL
                         CHECK (module IN ('IGM','OOC','SMTP','RMS','LEO','SHIPPING_BILL')),
        control_number   text,
        sender_id        text,
        receiver_id      text,
        message_id_code  text,
        sent_ts          timestamptz,
        primary_ref      text,
        source_file      text NOT NULL,
        source_sha256    text NOT NULL,
        file_size_bytes  bigint,
        record_count     integer NOT NULL DEFAULT 0,
        imported_count   integer NOT NULL DEFAULT 0,
        error_count      integer NOT NULL DEFAULT 0,
        import_status    text NOT NULL DEFAULT 'PENDING'
                         CHECK (import_status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
        error_detail     text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_customs_message_sha UNIQUE (source_sha256))""",
    "CREATE INDEX IF NOT EXISTS idx_customs_msg_module ON core.customs_message (module, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_customs_msg_type   ON core.customs_message (message_type, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_customs_msg_ref    ON core.customs_message (primary_ref)",
    "CREATE INDEX IF NOT EXISTS idx_customs_msg_status ON core.customs_message (import_status, id DESC)",
    """CREATE TABLE IF NOT EXISTS core.customs_import_error (
        id           bigserial PRIMARY KEY,
        message_id   bigint NOT NULL REFERENCES core.customs_message(id) ON DELETE CASCADE,
        record_ref   text,
        error_code   text NOT NULL,
        error_detail text,
        created_at   timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_customs_import_err_msg ON core.customs_import_error (message_id, id)",
    # -------------------------------------------------------------------- IGM
    """CREATE TABLE IF NOT EXISTS core.igm (
        id                     bigserial PRIMARY KEY,
        message_id             bigint NOT NULL REFERENCES core.customs_message(id) ON DELETE CASCADE,
        customs_house_code     text,
        igm_no                 text NOT NULL,
        igm_date               date,
        imo_code               text,
        vessel_code            text,
        voyage_no              text,
        shipping_line_code     text,
        shipping_agent_code    text,
        master_name            text,
        port_of_arrival        text,
        vessel_type            text,
        total_no_of_lines      integer,
        brief_cargo_desc       text,
        expected_arrival       timestamptz,
        entry_inward           timestamptz,
        terminal_operator_code text,
        created_at             timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_igm_vessel UNIQUE (igm_no, igm_date))""",
    "CREATE INDEX IF NOT EXISTS idx_igm_vessel_msg ON core.igm (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_igm_vessel_igm ON core.igm (igm_no)",
    """CREATE TABLE IF NOT EXISTS core.igm_line (
        id                  bigserial PRIMARY KEY,
        vessel_id           bigint NOT NULL REFERENCES core.igm(id) ON DELETE CASCADE,
        igm_no              text NOT NULL,
        igm_date            date,
        line_no             integer NOT NULL,
        subline_no          integer NOT NULL DEFAULT 0,
        bl_no               text,
        bl_date             date,
        house_bl_no         text,
        house_bl_date       date,
        port_of_loading     text,
        port_of_destination text,
        port_of_discharge   text,
        importer_name       text,
        importer_address    text,
        importer_state      text,
        notified_party      text,
        nature_of_cargo     text,
        item_type           text,
        cargo_movement      text,
        no_of_packages      integer,
        type_of_packages    text,
        gross_weight        numeric,
        unit_of_weight      text,
        goods_description   text,
        mlo_code            text,
        be_regularised      text,
        created_at          timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_igm_line UNIQUE (vessel_id, line_no, subline_no))""",
    "CREATE INDEX IF NOT EXISTS idx_igm_line_vessel ON core.igm_line (vessel_id)",
    "CREATE INDEX IF NOT EXISTS idx_igm_line_igm    ON core.igm_line (igm_no, line_no, subline_no)",
    "CREATE INDEX IF NOT EXISTS idx_igm_line_bl     ON core.igm_line (bl_no)",
    """CREATE TABLE IF NOT EXISTS core.igm_line_container (
        id                   bigserial PRIMARY KEY,
        cargo_line_id        bigint NOT NULL REFERENCES core.igm_line(id) ON DELETE CASCADE,
        igm_no               text NOT NULL,
        line_no              integer NOT NULL,
        subline_no           integer NOT NULL DEFAULT 0,
        container_no         text NOT NULL,
        iso_valid            boolean NOT NULL DEFAULT true,
        seal_no              text,
        container_agent_code text,
        container_status     text,
        no_of_packages       integer,
        container_weight     numeric,
        iso_size_type        text,
        soc_flag             text,
        created_at           timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_igm_container UNIQUE (cargo_line_id, container_no))""",
    "CREATE INDEX IF NOT EXISTS idx_igm_cont_line ON core.igm_line_container (cargo_line_id)",
    "CREATE INDEX IF NOT EXISTS idx_igm_cont_no   ON core.igm_line_container (container_no)",
    "CREATE INDEX IF NOT EXISTS idx_igm_cont_igm  ON core.igm_line_container (igm_no, line_no)",
    # -------------------------------------------------------------------- OOC
    """CREATE TABLE IF NOT EXISTS core.bill_of_entry_ooc (
        id                      bigserial PRIMARY KEY,
        message_id              bigint NOT NULL REFERENCES core.customs_message(id) ON DELETE CASCADE,
        customs_house_code      text,
        igm_no                  text,
        igm_date                date,
        line_no                 integer,
        subline_no              integer NOT NULL DEFAULT 0,
        bill_of_entry_no        text NOT NULL,
        bill_of_entry_date      date,
        document_type           text,
        ie_code                 text,
        importer_name           text,
        importer_address        text,
        importer_city           text,
        pin_code                text,
        cha_code                text,
        out_of_charge_no        text,
        out_of_charge_date      date,
        out_of_charge_type      text,
        nature_of_cargo         text,
        quantity_out_of_charged numeric,
        unit_of_quantity        text,
        no_of_packages          integer,
        country_of_origin       text,
        assessable_value        numeric,
        cif_value               numeric,
        total_customs_duty      numeric,
        created_at              timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_ooc_boe UNIQUE (bill_of_entry_no, line_no, subline_no))""",
    "CREATE INDEX IF NOT EXISTS idx_ooc_msg ON core.bill_of_entry_ooc (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_ooc_igm ON core.bill_of_entry_ooc (igm_no, line_no)",
    "CREATE INDEX IF NOT EXISTS idx_ooc_boe ON core.bill_of_entry_ooc (bill_of_entry_no)",
    "CREATE INDEX IF NOT EXISTS idx_ooc_ooc ON core.bill_of_entry_ooc (out_of_charge_no)",
    """CREATE TABLE IF NOT EXISTS core.ooc_item (
        id               bigserial PRIMARY KEY,
        ooc_id           bigint NOT NULL REFERENCES core.bill_of_entry_ooc(id) ON DELETE CASCADE,
        bill_of_entry_no text NOT NULL,
        container_no     text NOT NULL,
        iso_valid        boolean NOT NULL DEFAULT true,
        created_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_ooc_container UNIQUE (ooc_id, container_no))""",
    "CREATE INDEX IF NOT EXISTS idx_ooc_cont_ooc ON core.ooc_item (ooc_id)",
    "CREATE INDEX IF NOT EXISTS idx_ooc_cont_no  ON core.ooc_item (container_no)",
    """CREATE TABLE IF NOT EXISTS core.ooc_item (
        id               bigserial PRIMARY KEY,
        ooc_container_id bigint NOT NULL REFERENCES core.ooc_item(id) ON DELETE CASCADE,
        invoice_number   text,
        item_sr_no       integer,
        item_description  text,
        hs_classification text,
        cif_value        numeric,
        assessable_value numeric,
        created_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_ooc_item UNIQUE (ooc_container_id, invoice_number, item_sr_no))""",
    "CREATE INDEX IF NOT EXISTS idx_ooc_item_cont ON core.ooc_item (ooc_container_id)",
    "CREATE INDEX IF NOT EXISTS idx_ooc_item_hs   ON core.ooc_item (hs_classification)",
    # -------------------------------------------------------------------- SMTP
    """CREATE TABLE IF NOT EXISTS core.smtp_permit (
        id                     bigserial PRIMARY KEY,
        message_id             bigint NOT NULL REFERENCES core.customs_message(id) ON DELETE CASCADE,
        customs_house_code     text,
        smtp_no                text NOT NULL,
        smtp_date              date,
        igm_no                 text,
        igm_date               date,
        destination_code       text,
        carrier_code           text,
        bond_no                text,
        terminal_operator_code text,
        created_at             timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_smtp UNIQUE (smtp_no))""",
    "CREATE INDEX IF NOT EXISTS idx_smtp_msg  ON core.smtp_permit (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_smtp_igm  ON core.smtp_permit (igm_no)",
    "CREATE INDEX IF NOT EXISTS idx_smtp_bond ON core.smtp_permit (bond_no)",
    """CREATE TABLE IF NOT EXISTS core.smtp_container (
        id               bigserial PRIMARY KEY,
        smtp_id          bigint NOT NULL REFERENCES core.smtp_permit(id) ON DELETE CASCADE,
        smtp_no          text NOT NULL,
        line_no          integer NOT NULL,
        subline_no       integer NOT NULL DEFAULT 0,
        consignee_name   text,
        cargo_desc       text,
        container_no     text NOT NULL,
        iso_valid        boolean NOT NULL DEFAULT true,
        container_type   text,
        seal_no          text,
        no_of_packages   integer,
        unit_of_packages text,
        gross_qty        numeric,
        unit_of_qty      text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_smtp_line UNIQUE (smtp_id, line_no, container_no))""",
    "CREATE INDEX IF NOT EXISTS idx_smtp_line_smtp ON core.smtp_container (smtp_id)",
    "CREATE INDEX IF NOT EXISTS idx_smtp_line_cont ON core.smtp_container (container_no)",
    # -------------------------------------------------------------------- RMS
    """CREATE TABLE IF NOT EXISTS core.rms_scan_report (
        id                  bigserial PRIMARY KEY,
        message_id          bigint NOT NULL REFERENCES core.customs_message(id) ON DELETE CASCADE,
        customs_house       text,
        shipping_line       text,
        shipping_agent      text,
        igm_no              text NOT NULL,
        igm_date            date,
        igm_date_raw        text,
        processing_end_date date,
        vessel_name         text,
        subject             text,
        any_selected        boolean NOT NULL DEFAULT false,
        selected_count      integer NOT NULL DEFAULT 0,
        created_at          timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_rms_scanlist UNIQUE (igm_no))""",
    "CREATE INDEX IF NOT EXISTS idx_rms_scan_msg ON core.rms_scan_report (message_id)",
    """CREATE TABLE IF NOT EXISTS core.rms_scan_container (
        id            bigserial PRIMARY KEY,
        scanlist_id   bigint NOT NULL REFERENCES core.rms_scan_report(id) ON DELETE CASCADE,
        igm_no        text NOT NULL,
        sl_no         integer,
        container_no  text NOT NULL,
        iso_valid     boolean NOT NULL DEFAULT true,
        scan_machine  text,
        scan_location text,
        cfs_name      text,
        goods_desc    text,
        created_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_rms_container UNIQUE (scanlist_id, container_no))""",
    "CREATE INDEX IF NOT EXISTS idx_rms_cont_scan ON core.rms_scan_container (scanlist_id)",
    "CREATE INDEX IF NOT EXISTS idx_rms_cont_no   ON core.rms_scan_container (container_no)",
    # ---------------------------------------------------------- Shipping Bill
    """CREATE TABLE IF NOT EXISTS core.shipping_bill (
        id         bigserial PRIMARY KEY,
        message_id bigint NOT NULL REFERENCES core.customs_message(id) ON DELETE CASCADE,
        sb_no      text NOT NULL,
        sb_date    date,
        site_id    text,
        action     text,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_shipping_bill UNIQUE (sb_no))""",
    "CREATE INDEX IF NOT EXISTS idx_sb_msg  ON core.shipping_bill (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_sb_date ON core.shipping_bill (sb_date)",
    # -------------------------------------------------------------------- LEO
    """CREATE TABLE IF NOT EXISTS core.leo (
        id          bigserial PRIMARY KEY,
        message_id  bigint NOT NULL REFERENCES core.customs_message(id) ON DELETE CASCADE,
        sb_no       text NOT NULL,
        sb_date     date,
        site_id     text,
        rotation_no text,
        leo_date    date,
        action      text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_leo UNIQUE (sb_no, leo_date))""",
    "CREATE INDEX IF NOT EXISTS idx_leo_msg ON core.leo (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_leo_sb  ON core.leo (sb_no)",
    # ---------------------------------------------------------- event log
    """CREATE TABLE IF NOT EXISTS core.customs_event (
        id           bigserial PRIMARY KEY,
        event        text NOT NULL,
        module       text,
        reference    text,
        container_no text,
        payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at   timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_customs_events_id     ON core.customs_event (id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_customs_events_cont   ON core.customs_event (container_no, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_customs_events_event  ON core.customs_event (event, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_customs_events_module ON core.customs_event (module, id DESC)",
    # ---------------------------------------------- derived per-container status
    """CREATE OR REPLACE VIEW mart.v_customs_container_status AS
        WITH cont AS (
            SELECT container_no, igm_no FROM core.igm_line_container
            UNION SELECT container_no, igm_no FROM core.ooc_item oc
                  JOIN core.bill_of_entry_ooc o ON o.id = oc.ooc_id
            UNION SELECT container_no, igm_no FROM core.smtp_container sl
                  JOIN core.smtp_permit s ON s.id = sl.smtp_id
            UNION SELECT container_no, igm_no FROM core.rms_scan_container
        )
        SELECT
            c.container_no,
            max(c.igm_no) AS igm_no,
            EXISTS (SELECT 1 FROM core.igm_line_container ic WHERE ic.container_no = c.container_no) AS declared_igm,
            EXISTS (SELECT 1 FROM core.rms_scan_container rc WHERE rc.container_no = c.container_no) AS rms_selected,
            EXISTS (SELECT 1 FROM core.ooc_item oc WHERE oc.container_no = c.container_no) AS ooc_cleared,
            EXISTS (SELECT 1 FROM core.smtp_container     sl WHERE sl.container_no = c.container_no) AS smtp_bonded
        FROM cont c
        GROUP BY c.container_no""",
]


async def ensure_customs_schema(dsn: Optional[str] = None) -> None:
    """Create the customs tables + view if absent. Idempotent; safe to call every boot."""
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    applied = 0
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
            applied += 1
    log.info("customs_schema_ready", statements=applied, total=len(_DDL))
