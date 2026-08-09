"""The marine bootstrap DDL must be self-contained and correctly ordered.

REGRESSION. ``ensure_marine_schema`` runs the whole ``_DDL`` list in ONE transaction with
no per-statement error handling — deliberately, so a schema fault fails loudly instead of
leaving a half-built database. That makes ORDER and COMPLETENESS load-bearing: a single
statement touching a table the list never creates rolls back everything before it.

That is exactly what happened. ``ALTER TABLE core.bathymetry_survey ADD COLUMN
data_origin`` sat in the list while nothing in this repository created that table; the
statement only ever succeeded because the shared RDS instance had schema.sql applied to it
by hand. On a clean database the ALTER raised "relation does not exist", the transaction
rolled back, and the database came up with NO marine tables at all — including
``uq_marine_import_file_hash_origin``, the unique index the import ledger's file-level
de-duplication depends on. ``gateway/main.py`` logs the rollback as a warning and boots
anyway, so nothing surfaced.

These tests are static: they read ``_DDL`` and need no database.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gateway.marine_ext import _DDL

#: The canonical schema, at the workspace root. Absent in a stripped checkout.
_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "schema.sql"

_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(core\.\w+)", re.I)
_ALTER_RE = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(core\.\w+)", re.I)
_REFS_RE = re.compile(r"REFERENCES\s+(core\.\w+)", re.I)


def _first_create_index() -> dict[str, int]:
    """table -> index of the _DDL statement that creates it."""
    out: dict[str, int] = {}
    for i, stmt in enumerate(_DDL):
        for t in _CREATE_RE.findall(stmt):
            out.setdefault(t.lower(), i)
    return out


class TestBathymetrySurvey:
    """The specific defect."""

    def test_the_survey_header_is_created_by_the_bootstrap(self):
        assert "core.bathymetry_survey" in _first_create_index(), (
            "core.bathymetry_survey is referenced by _DDL but created by nothing; a clean "
            "database rolls the whole marine schema back")

    def test_it_is_created_before_the_sounding_table_that_references_it(self):
        created = _first_create_index()
        assert created["core.bathymetry_survey"] < created["core.bathymetry_sounding"], (
            "core.bathymetry_sounding.survey_id is a hard FK to core.bathymetry_survey, so "
            "the header must be created first")

    def test_it_is_created_before_any_alter_touching_it(self):
        created = _first_create_index()["core.bathymetry_survey"]
        alters = [i for i, s in enumerate(_DDL)
                  if "core.bathymetry_survey" in [t.lower() for t in _ALTER_RE.findall(s)]]
        assert alters, "expected the 0120/0121 data_origin ALTER to still be present"
        assert min(alters) > created

    def test_data_origin_is_NOT_duplicated_into_the_create(self):
        """data_origin belongs to the 0120/0121 block, which owns it for every marine
        table. Adding it to the CREATE too would fork that rule."""
        stmt = _DDL[_first_create_index()["core.bathymetry_survey"]]
        assert "data_origin" not in stmt


@pytest.mark.skipif(not _SCHEMA_SQL.is_file(), reason=f"canonical schema absent: {_SCHEMA_SQL}")
class TestMatchesTheCanonicalSchema:
    """The definition is REPRODUCED from schema.sql §10, never invented. If the canonical
    table changes, this fails rather than letting the two drift apart silently."""

    @staticmethod
    def _columns(block: str) -> list[tuple[str, str]]:
        body = block[block.index("(") + 1:]
        cols: list[tuple[str, str]] = []
        for raw in body.splitlines():
            line = re.sub(r"--.*$", "", raw).strip().rstrip(",").rstrip(")").strip()
            if not line or line.upper().startswith(("CONSTRAINT", "PRIMARY KEY", "UNIQUE (")):
                continue
            m = re.match(r"^(\w+)\s+(.+)$", line)
            if m:
                cols.append((m.group(1).lower(), " ".join(m.group(2).split()).lower()))
        return cols

    def test_columns_and_types_match_schema_sql(self):
        canonical = re.search(r"CREATE TABLE core\.bathymetry_survey\s*\(.*?\);",
                              _SCHEMA_SQL.read_text(encoding="utf-8"), re.S)
        assert canonical, "core.bathymetry_survey not found in schema.sql"
        ours = _DDL[_first_create_index()["core.bathymetry_survey"]]
        assert self._columns(ours) == self._columns(canonical.group(0))


class TestBootstrapIsSelfContained:
    """The general form of the bug — the guard that catches the NEXT one."""

    def test_every_altered_table_is_created_by_the_same_list(self):
        created = _first_create_index()
        offenders: list[tuple[int, str]] = []
        for i, stmt in enumerate(_DDL):
            for t in {t.lower() for t in _ALTER_RE.findall(stmt)}:
                if t not in created or created[t] > i:
                    offenders.append((i, t))
        assert not offenders, (
            f"_DDL alters tables it never creates (or creates too late): {offenders}. "
            f"One transaction wraps the whole list, so this rolls back the entire schema.")

    def test_every_referenced_table_is_created_first(self):
        created = _first_create_index()
        offenders: list[tuple[int, str]] = []
        for i, stmt in enumerate(_DDL):
            for t in {t.lower() for t in _REFS_RE.findall(stmt)}:
                if t not in created or created[t] > i:
                    offenders.append((i, t))
        assert not offenders, f"_DDL declares FKs to tables not yet created: {offenders}"


# ---------------------------------------------------------------- RC3 - berthing ledger
class TestBerthingBootstrapCarriesDataOrigin:
    """RC3. services/berthing/repository.py de-dupes on (file_hash, data_origin), but
    berthing_ext mirrored migrations 0036/0037 and stopped there - it never mirrored 0120.
    On a database this repository provisions, the column did not exist and the FIRST
    berthing import died with UndefinedColumnError. It only ever worked because the shared
    instance had 0120 applied to it directly.
    """

    @staticmethod
    def _ddl():
        from gateway.berthing_ext import _DDL
        return _DDL

    def test_data_origin_is_added_to_the_import_ledger(self):
        joined = "\n".join(self._ddl())
        assert re.search(
            r"ALTER TABLE core\.berthing_import_file\s+ADD COLUMN IF NOT EXISTS "
            r"data_origin text NOT NULL DEFAULT 'MANUAL'", joined), \
            "berthing_ext does not mirror migration 0120's data_origin column"

    def test_the_per_origin_unique_index_replaces_the_single_origin_one(self):
        joined = "\n".join(self._ddl())
        assert "uq_berthing_import_file_hash_origin" in joined
        assert re.search(r"ON core\.berthing_import_file \(file_hash, data_origin\)", joined)
        # the old single-column constraint/index must be dropped, exactly as 0120 does
        assert "DROP CONSTRAINT IF EXISTS uq_berthing_import_file_hash" in joined
        assert "DROP INDEX IF EXISTS core.uq_berthing_import_file_hash" in joined

    def test_the_alter_follows_the_create(self):
        """Same single-transaction rule as the marine list: altering a table this list has
        not yet created rolls the whole bootstrap back."""
        ddl = self._ddl()
        created = next(i for i, s in enumerate(ddl)
                       if "CREATE TABLE IF NOT EXISTS core.berthing_import_file" in s)
        altered = next(i for i, s in enumerate(ddl)
                       if "ALTER TABLE core.berthing_import_file" in s)
        assert created < altered

    def test_the_berthing_list_is_self_contained(self):
        """Generalised guard, matching the marine one."""
        ddl = self._ddl()
        created: dict[str, int] = {}
        for i, s in enumerate(ddl):
            for t in _CREATE_RE.findall(s):
                created.setdefault(t.lower(), i)
        offenders = []
        for i, s in enumerate(ddl):
            for t in {t.lower() for t in _ALTER_RE.findall(s)} | {t.lower() for t in _REFS_RE.findall(s)}:
                if t not in created or created[t] > i:
                    offenders.append((i, t))
        assert not offenders, f"berthing _DDL touches tables it never creates: {offenders}"
