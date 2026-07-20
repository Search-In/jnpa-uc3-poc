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

_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(jnpa\.\w+)", re.IGNORECASE)
_VIEW = re.compile(r"CREATE OR REPLACE VIEW\s+(jnpa\.\w+)", re.IGNORECASE)


def _objects(text: str) -> set[str]:
    return {m.lower() for m in _TABLE.findall(text)} | {m.lower() for m in _VIEW.findall(text)}


def test_migration_and_ext_define_same_objects():
    from gateway.td_upload_ext import _DDL
    mig = (REPO_ROOT / "infra" / "postgres" / "migrations"
           / "0035_transporters_drivers_upload.sql").read_text()
    migration_objs = _objects(mig)
    ext_objs = _objects("\n".join(_DDL))
    assert migration_objs == ext_objs, (
        f"schema drift between migration 0035 and td_upload_ext._DDL:\n"
        f"  only in migration: {sorted(migration_objs - ext_objs)}\n"
        f"  only in _DDL:      {sorted(ext_objs - migration_objs)}")


def test_expected_upload_objects_present():
    from gateway.td_upload_ext import _DDL
    objs = _objects("\n".join(_DDL))
    for name in ("jnpa.td_import_files", "jnpa.td_import_errors"):
        assert name in objs, f"missing Transporter/Driver upload object: {name}"


def test_ext_adds_import_file_id_to_both_masters():
    from gateway.td_upload_ext import _DDL
    ddl = "\n".join(_DDL).lower()
    assert "alter table jnpa.transporters" in ddl and "import_file_id" in ddl
    assert "alter table jnpa.driver_master" in ddl


def test_status_vocabulary_is_the_reusable_set():
    from gateway.td_upload_ext import _DDL
    ddl = "\n".join(_DDL)
    for s in ("PENDING", "SUCCESS", "PARTIAL", "FAILED", "SKIPPED_DUPLICATE"):
        assert s in ddl, f"missing import_status value {s}"
