"""Schema lock-step for the Transporters & Drivers Data Upload sub-module.

Asserts that migration 0035_transporters_drivers_upload.sql and the boot-time
bootstrap gateway/td_upload_ext._DDL define the SAME ledger objects, so a dev/mock DB
(which runs _DDL at boot) can never drift from a migrated production DB. Mirrors
tests/test_cfs_ecy_upload_schema.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

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
    from gateway.td_upload_ext import _DDL
    pat = re.compile(r"CREATE (?:TABLE (?:IF NOT EXISTS )?|(?:OR REPLACE )?VIEW )((?:core|mart)\.\w+)", re.I)
    src = "\n".join(_DDL) if not isinstance(_DDL, str) else _DDL
    return {m.lower() for m in pat.findall(src)}


def test_migration_and_ext_define_same_objects():
    """v3: every object the (retired) boot DDL would create must be defined by the
    canonical infra/postgres/v3 runbook — the ext copy can never drift ahead."""
    v3 = _v3_objects()
    ext_objs = _ext_objects()
    assert ext_objs, "no CREATE TABLE/VIEW found in ext _DDL"
    missing = sorted(ext_objs - v3)
    assert not missing, f"objects in ext _DDL missing from infra/postgres/v3: {missing}"


def test_expected_upload_objects_present():
    from gateway.td_upload_ext import _DDL
    objs = _objects("\n".join(_DDL))
    for name in ("core.td_import_file", "core.td_import_error"):
        assert name in objs, f"missing Transporter/Driver upload object: {name}"


def test_ext_adds_import_file_id_to_both_masters():
    from gateway.td_upload_ext import _DDL
    ddl = "\n".join(_DDL).lower()
    assert "alter table core.transporter" in ddl and "import_file_id" in ddl
    assert "alter table core.driver" in ddl


def test_status_vocabulary_is_the_reusable_set():
    from gateway.td_upload_ext import _DDL
    ddl = "\n".join(_DDL)
    for s in ("PENDING", "SUCCESS", "PARTIAL", "FAILED", "SKIPPED_DUPLICATE"):
        assert s in ddl, f"missing import_status value {s}"
