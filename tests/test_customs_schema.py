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

_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+((?:core|mart)\.\w+)", re.IGNORECASE)
_VIEW = re.compile(r"CREATE OR REPLACE VIEW\s+((?:core|mart)\.\w+)", re.IGNORECASE)


def _objects(text: str) -> set[str]:
    return {m.lower() for m in _TABLE.findall(text)} | {m.lower() for m in _VIEW.findall(text)}



# schema-v3: the boot-time DDL is retired (JNPA_RUNTIME_DDL gate); the canonical
# definitions live in infra/postgres/v3/. The drift test now asserts every object
# the ext DDL would create is defined by the v3 runbook (ext subset-of v3).
def _v3_objects() -> set[str]:
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / "infra" / "postgres" / "v3"
    text = "\n".join(p.read_text() for p in sorted(root.glob("*.sql")))
    pat_t = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?((?:core|mart|staging)\.\w+)", re.I)
    pat_v = re.compile(r"CREATE (?:OR REPLACE )?VIEW ((?:core|mart)\.\w+)", re.I)
    pat_a = re.compile(r"ALTER TABLE ((?:core|mart)\.\w+)", re.I)
    return ({m.lower() for m in pat_t.findall(text)}
            | {m.lower() for m in pat_v.findall(text)}
            | {m.lower() for m in pat_a.findall(text)})


def _ext_objects() -> set[str]:
    pat = re.compile(r"CREATE (?:TABLE (?:IF NOT EXISTS )?|(?:OR REPLACE )?VIEW )((?:core|mart)\.\w+)", re.I)
    try:
        src = "\n".join(_DDL)
    except TypeError:
        src = str(_DDL)
    return {m.lower() for m in pat.findall(src)}


def test_migration_and_ext_define_same_objects():
    """v3: every object the (retired) boot DDL would create must be defined by the
    canonical infra/postgres/v3 runbook — the ext copy can never drift ahead."""
    v3 = _v3_objects()
    ext_objs = _ext_objects()
    assert ext_objs, "no CREATE TABLE/VIEW found in ext _DDL"
    missing = sorted(ext_objs - v3)
    assert not missing, f"objects in ext _DDL missing from infra/postgres/v3: {missing}"


def test_expected_customs_tables_present():
    objs = _objects("\n".join(_DDL))
    for name in (
        "core.customs_message", "core.customs_import_error",
        "core.igm", "core.igm_line", "core.igm_line_container",
        "core.bill_of_entry_ooc", "core.ooc_item", "core.ooc_item",
        "core.smtp_permit", "core.smtp_container",
        "core.rms_scan_report", "core.rms_scan_container",
        "core.shipping_bill", "core.leo", "core.customs_event",
        "mart.v_customs_container_status",
    ):
        assert name in objs, f"missing customs object: {name}"
