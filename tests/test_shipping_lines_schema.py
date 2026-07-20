"""Guard against drift between the shipping-lines migration and the boot-time DDL.

The gateway image does not ship infra/, so gateway.shipping_lines_ext embeds the
DDL as a _DDL list mirroring infra/postgres/migrations/0032_shipping_lines.sql. This
test asserts both define the SAME set of tables + views, so the two copies can never
silently diverge. Pure — no DB required.
"""
from __future__ import annotations

import re
from pathlib import Path

from gateway.shipping_lines_ext import _DDL

_MIGRATION = (Path(__file__).resolve().parents[1]
              / "infra" / "postgres" / "migrations" / "0032_shipping_lines.sql")

_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(jnpa\.\w+)", re.IGNORECASE)
_VIEW = re.compile(r"CREATE OR REPLACE VIEW\s+(jnpa\.\w+)", re.IGNORECASE)


def _objects(text: str) -> set[str]:
    return {m.lower() for m in _TABLE.findall(text)} | {m.lower() for m in _VIEW.findall(text)}


def test_migration_and_ext_define_same_objects():
    migration_objs = _objects(_MIGRATION.read_text())
    ext_objs = _objects("\n".join(_DDL))
    assert migration_objs, "no CREATE TABLE/VIEW found in migration 0032"
    assert migration_objs == ext_objs, (
        f"schema drift between 0032_shipping_lines.sql and shipping_lines_ext._DDL:\n"
        f"  only in migration: {sorted(migration_objs - ext_objs)}\n"
        f"  only in _DDL:      {sorted(ext_objs - migration_objs)}")


def test_expected_shipping_lines_objects_present():
    objs = _objects("\n".join(_DDL))
    for name in (
        "jnpa.shipping_lines", "jnpa.sl_import_files", "jnpa.sl_import_errors",
        "jnpa.sl_advance_containers", "jnpa.sl_delivery_orders", "jnpa.sl_events",
        "jnpa.v_shipping_line_container",
    ):
        assert name in objs, f"missing shipping-lines object: {name}"
