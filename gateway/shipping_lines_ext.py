"""Shipping Lines module schema bootstrap (idempotent, additive).

Applies the same DDL as infra/postgres/migrations/0032_shipping_lines.sql at
gateway boot so a database that never ran the migration still gets the shipping-
line tables + view lazily — exactly the pattern gateway/customs_ext.ensure_customs_schema
and gateway/cfs_ecy_ext.ensure_cfs_ecy_schema already use (the gateway image does
not ship infra/, so the DDL is embedded here rather than read from the .sql file).

Every statement is CREATE ... IF NOT EXISTS / CREATE OR REPLACE VIEW: running it
against a DB that already has the objects (because the migration ran) is a no-op.
It DROPS/ALTERS nothing existing and touches no cargo / customs / gate / auth
tables — the shipping-line rows soft-link to jnpa.cargo BY VALUE (container_no),
never by FK.

Called once from gateway/main.py::_lifespan (best-effort; a DB blip only logs).
Also reused by scripts/import_shipping_lines.py so the importer is self-contained.

The _DDL list below MUST stay in lock-step with migration 0032; the test
tests/test_shipping_lines_schema.py asserts both define the same table/view set.
"""
from __future__ import annotations

from typing import Optional

from .logging import get_logger

log = get_logger("gateway.shipping_lines_ext")

# One idempotent statement per list item (SQLAlchemy text() runs a single
# statement per execute()). Mirrors migration 0032 exactly.
_DDL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS jnpa",
    # ------------------------------------------------ shipping-line master registry
    """CREATE TABLE IF NOT EXISTS jnpa.shipping_lines (
        line_code   text PRIMARY KEY,
        line_name   text,
        source      text NOT NULL DEFAULT 'ADVANCE_LIST',
        first_seen  timestamptz NOT NULL DEFAULT now(),
        last_seen   timestamptz NOT NULL DEFAULT now())""",
    # ------------------------------------------------ import ledger / file envelope
    """CREATE TABLE IF NOT EXISTS jnpa.sl_import_files (
        id               bigserial PRIMARY KEY,
        list_type        text NOT NULL CHECK (list_type IN ('IAL','EAL','EDO')),
        terminal         text NOT NULL
                         CHECK (terminal IN ('APMT','BMCT','GTI','NSFT','NSICT','NSIGT','OTHER')),
        physical_format  text NOT NULL
                         CHECK (physical_format IN ('CSV','XLS','XLSX','CODECO_XML')),
        source_file      text NOT NULL,
        source_sha256    text NOT NULL,
        file_size_bytes  bigint,
        vessel_visit     text,
        voyage           text,
        line_code        text,
        direction        text,
        record_count     integer NOT NULL DEFAULT 0,
        imported_count   integer NOT NULL DEFAULT 0,
        error_count      integer NOT NULL DEFAULT 0,
        import_status    text NOT NULL DEFAULT 'PENDING'
                         CHECK (import_status IN ('PENDING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
        error_detail     text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_sl_import_file_sha UNIQUE (source_sha256))""",
    "CREATE INDEX IF NOT EXISTS idx_sl_file_list ON jnpa.sl_import_files (list_type, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sl_file_term ON jnpa.sl_import_files (terminal, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sl_file_stat ON jnpa.sl_import_files (import_status, id DESC)",
    # ------------------------------------------------ import row-level errors
    """CREATE TABLE IF NOT EXISTS jnpa.sl_import_errors (
        id             bigserial PRIMARY KEY,
        import_file_id bigint NOT NULL REFERENCES jnpa.sl_import_files(id) ON DELETE CASCADE,
        record_ref     text,
        error_code     text NOT NULL,
        error_detail   text,
        created_at     timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_sl_err_file ON jnpa.sl_import_errors (import_file_id, id)",
    # ------------------------------------------------ IAL/EAL canonical line items
    """CREATE TABLE IF NOT EXISTS jnpa.sl_advance_containers (
        id                  bigserial PRIMARY KEY,
        import_file_id      bigint NOT NULL REFERENCES jnpa.sl_import_files(id) ON DELETE CASCADE,
        list_type           text NOT NULL CHECK (list_type IN ('IAL','EAL')),
        terminal            text NOT NULL,
        container_no        text NOT NULL,
        iso_code            text,
        container_valid_iso boolean NOT NULL DEFAULT false,
        freight_kind        text NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (freight_kind IN ('FULL','EMPTY','UNKNOWN')),
        category            text NOT NULL DEFAULT 'OTHER'
                            CHECK (category IN ('IMPORT','EXPORT','TRANSHIP','OTHER')),
        gross_weight_kg     numeric,
        weight_source_uom   text CHECK (weight_source_uom IN ('KG','MT')),
        pol                 text,
        pod                 text,
        destination         text,
        shipping_line_code  text REFERENCES jnpa.shipping_lines(line_code),
        vessel_visit        text,
        voyage              text,
        bill_of_lading      text,
        seal_no             text,
        reefer_status       text,
        reefer_temp         numeric,
        reefer_uom          text,
        imdg_code           text,
        un_number           text,
        group_code          text,
        client_code         text,
        departure_mode      text,
        nominated_cfs       text,
        iec_code            text,
        gst_no              text,
        commodity_code      text,
        raw                 jsonb NOT NULL DEFAULT '{}'::jsonb,
        row_sha256          text NOT NULL DEFAULT '',
        created_at          timestamptz NOT NULL DEFAULT now())""",
    # Content-hash uniqueness: byte-identical rows collapse (idempotent), but any row
    # that differs in ANY source field persists — normalization never drops a distinct
    # source row (e.g. one container under two operator codes in the same list).
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sl_adv_container ON jnpa.sl_advance_containers "
    "(import_file_id, row_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_sl_adv_container_no ON jnpa.sl_advance_containers (container_no)",
    "CREATE INDEX IF NOT EXISTS idx_sl_adv_bl   ON jnpa.sl_advance_containers (bill_of_lading)",
    "CREATE INDEX IF NOT EXISTS idx_sl_adv_line ON jnpa.sl_advance_containers (shipping_line_code)",
    "CREATE INDEX IF NOT EXISTS idx_sl_adv_term ON jnpa.sl_advance_containers (terminal, list_type)",
    # ------------------------------------------------ EDO / CODECO delivery orders
    """CREATE TABLE IF NOT EXISTS jnpa.sl_delivery_orders (
        id                  bigserial PRIMARY KEY,
        import_file_id      bigint NOT NULL REFERENCES jnpa.sl_import_files(id) ON DELETE CASCADE,
        document_number     text,
        common_ref_number   text,
        message_type        text,
        sender_id           text,
        receiving_party     text,
        vcn                 text,
        imo_number          text,
        call_sign           text,
        stuff_destuff_flag  text,
        shipping_agent_code text,
        vessel_country      text,
        total_containers    integer,
        container_no        text NOT NULL,
        iso_code            text,
        container_valid_iso boolean NOT NULL DEFAULT false,
        equipment_status    text,
        cargo_type          text,
        loading_port        text,
        dest_port           text,
        final_pod           text,
        arrival_ts          timestamptz,
        receipt_date        date,
        delivery_mode       text,
        gate_pass_no        text,
        gate_pass_ts        timestamptz,
        vehicle_no          text,
        gate_number         text,
        ca_code             text,
        con_seal_status     text,
        issued_ts           timestamptz,
        raw_xml             text,
        created_at          timestamptz NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sl_delivery_order ON jnpa.sl_delivery_orders "
    "(COALESCE(common_ref_number, ''), container_no, COALESCE(gate_pass_no, ''))",
    "CREATE INDEX IF NOT EXISTS idx_sl_do_container ON jnpa.sl_delivery_orders (container_no)",
    "CREATE INDEX IF NOT EXISTS idx_sl_do_gatepass  ON jnpa.sl_delivery_orders (gate_pass_no)",
    "CREATE INDEX IF NOT EXISTS idx_sl_do_vehicle   ON jnpa.sl_delivery_orders (vehicle_no)",
    # ------------------------------------------------ append-only event log
    """CREATE TABLE IF NOT EXISTS jnpa.sl_events (
        id           bigserial PRIMARY KEY,
        event        text NOT NULL,
        module       text,
        reference    text,
        container_no text,
        payload      jsonb,
        created_at   timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_sl_events_mod  ON jnpa.sl_events (module, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sl_events_cont ON jnpa.sl_events (container_no)",
    # ------------------------------------------------ per-container rollup view
    #   One row per container: the most-recent advance-list fact FULL-joined to its
    #   most-recent delivery order. Soft, by-value link to jnpa.cargo (container_no).
    """CREATE OR REPLACE VIEW jnpa.v_shipping_line_container AS
        WITH ac AS (
            SELECT DISTINCT ON (container_no)
                container_no, list_type, terminal, shipping_line_code, category,
                freight_kind, gross_weight_kg, weight_source_uom, pol, pod, destination,
                bill_of_lading, vessel_visit, voyage, iso_code, seal_no, reefer_status,
                reefer_temp, id
            FROM jnpa.sl_advance_containers
            ORDER BY container_no, id DESC
        ),
        edo AS (
            SELECT DISTINCT ON (container_no)
                container_no, gate_pass_no, gate_pass_ts, vehicle_no, delivery_mode,
                shipping_agent_code, equipment_status, loading_port, dest_port, final_pod, id
            FROM jnpa.sl_delivery_orders
            ORDER BY container_no, id DESC
        )
        SELECT
            COALESCE(ac.container_no, edo.container_no)      AS container_no,
            ac.list_type, ac.terminal, ac.shipping_line_code, ac.category,
            ac.freight_kind, ac.gross_weight_kg, ac.weight_source_uom, ac.pol, ac.pod,
            ac.destination, ac.bill_of_lading, ac.vessel_visit, ac.voyage, ac.iso_code,
            ac.seal_no, ac.reefer_status, ac.reefer_temp,
            edo.gate_pass_no, edo.gate_pass_ts, edo.vehicle_no, edo.delivery_mode,
            edo.shipping_agent_code, edo.equipment_status,
            (ac.container_no IS NOT NULL) AS in_advance_list,
            (edo.container_no IS NOT NULL) AS has_delivery_order
        FROM ac
        FULL OUTER JOIN edo ON edo.container_no = ac.container_no""",
    # ---------------------------------------- Data Upload sub-module (migration 0033)
    # Additive columns on the existing ledger so UI uploads are attributable. Both are
    # nullable/defaulted — the directory importer and pre-existing rows are unaffected.
    "ALTER TABLE jnpa.sl_import_files ADD COLUMN IF NOT EXISTS uploaded_by text",
    "ALTER TABLE jnpa.sl_import_files ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'DIRECTORY'",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
    "'chk_sl_import_files_source') THEN ALTER TABLE jnpa.sl_import_files ADD CONSTRAINT "
    "chk_sl_import_files_source CHECK (source IN ('DIRECTORY', 'UPLOAD')); END IF; END$$",
    "CREATE INDEX IF NOT EXISTS idx_sl_file_source ON jnpa.sl_import_files (source, id DESC)",
]


async def ensure_shipping_lines_schema(dsn: Optional[str] = None) -> None:
    """Create the shipping-line tables + view if absent. Idempotent; safe to call every boot."""
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    applied = 0
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
            applied += 1
    log.info("shipping_lines_schema_ready", statements=applied, total=len(_DDL))


__all__ = ["ensure_shipping_lines_schema"]
