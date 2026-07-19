"""Guard against drift between the customs migration and the boot-time DDL.

The gateway image does not ship infra/, so gateway.customs_ext embeds the DDL as a
_DDL list mirroring infra/postgres/migrations/0031_customs.sql. This test asserts
both define the SAME set of tables + views, so the two copies can never silently
diverge (the exact hazard the POC-3 audit flagged for the older *_ext modules).
Pure — no DB required.
"""
from __future__ import annotations

import re
from pathlib import Path

from gateway.customs_ext import _DDL

_MIGRATION = (Path(__file__).resolve().parents[1]
              / "infra" / "postgres" / "migrations" / "0031_customs.sql")

_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(jnpa\.\w+)", re.IGNORECASE)
_VIEW = re.compile(r"CREATE OR REPLACE VIEW\s+(jnpa\.\w+)", re.IGNORECASE)


def _objects(text: str) -> set[str]:
    return {m.lower() for m in _TABLE.findall(text)} | {m.lower() for m in _VIEW.findall(text)}


def test_migration_and_ext_define_same_objects():
    migration_objs = _objects(_MIGRATION.read_text())
    ext_objs = _objects("\n".join(_DDL))
    assert migration_objs, "no CREATE TABLE/VIEW found in migration 0031"
    assert migration_objs == ext_objs, (
        f"schema drift between 0031_customs.sql and customs_ext._DDL:\n"
        f"  only in migration: {sorted(migration_objs - ext_objs)}\n"
        f"  only in _DDL:      {sorted(ext_objs - migration_objs)}")


def test_expected_customs_tables_present():
    objs = _objects("\n".join(_DDL))
    for name in (
        "jnpa.customs_messages", "jnpa.customs_import_errors",
        "jnpa.customs_igm_vessel", "jnpa.customs_igm_cargo_line", "jnpa.customs_igm_container",
        "jnpa.customs_ooc", "jnpa.customs_ooc_container", "jnpa.customs_ooc_item",
        "jnpa.customs_smtp", "jnpa.customs_smtp_line",
        "jnpa.customs_rms_scanlist", "jnpa.customs_rms_container",
        "jnpa.customs_shipping_bill", "jnpa.customs_leo", "jnpa.customs_events",
        "jnpa.v_customs_container_status",
    ):
        assert name in objs, f"missing customs object: {name}"
