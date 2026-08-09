"""The UC-I loader's quarantine report must match the table it reads.

REGRESSION: the first version of ``quarantine_report()`` selected ``e.error_code`` and
``e.error_detail``. Neither column exists — migration 0045 declares
``id, import_file_id, row_number, error_message, raw_data, created_at``, and
``VesselCallRepository._err_row`` flattens a ParseResult error into that shape. Because
the query sits inside ``try/except -> return []``, the mistake did not raise: it silently
reported NO data-quality findings at all, which is precisely the evidence UC1-002 exists
to produce.

These tests need no database. They compare the SQL against the migration that owns the
table, so the two cannot drift apart again.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.ingest_uc1_corpus import _QUARANTINE_SQL, _print_report

_MIGRATION = (Path(__file__).resolve().parents[1]
              / "infra" / "postgres" / "migrations" / "0045_marine_import_ledger.sql")


def _declared_columns(table: str) -> set[str]:
    """Column names from the CREATE TABLE block for ``table`` in migration 0045."""
    sql = _MIGRATION.read_text(encoding="utf-8")
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\);",
                  sql, re.S | re.I)
    assert m, f"{table} not found in {_MIGRATION.name}"
    cols: set[str] = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        # Column lines open with the name; continuation lines (REFERENCES ...) do not.
        c = re.match(r"^([a-z_][a-z0-9_]*)\s+[a-z]", line, re.I)
        if c and c.group(1).upper() not in {"REFERENCES", "CONSTRAINT", "PRIMARY", "UNIQUE"}:
            cols.add(c.group(1))
    assert cols, "parsed no columns - the DDL shape changed"
    return cols


class TestQuarantineSqlMatchesTheSchema:
    def test_every_referenced_column_exists(self):
        declared = _declared_columns("core.marine_import_errors")
        used = set(re.findall(r"\be\.([a-z_][a-z0-9_]*)", _QUARANTINE_SQL))
        assert used, "the query no longer aliases the errors table as `e`"
        missing = used - declared
        assert not missing, (
            f"quarantine_report() reads columns core.marine_import_errors does not have: "
            f"{sorted(missing)}; declared: {sorted(declared)}")

    def test_the_columns_that_caused_the_bug_are_not_referenced(self):
        """error_code / error_detail are ParseResult keys, NOT columns. The repository
        folds them into error_message before the insert."""
        for ghost in ("error_code", "error_detail", "raw_value", "column_name"):
            assert f"e.{ghost}" not in _QUARANTINE_SQL

    def test_the_error_message_and_raw_data_columns_are_both_read(self):
        """UC1-002 wants the finding AND the offending value, so both must be projected."""
        assert "e.error_message" in _QUARANTINE_SQL
        assert "e.raw_data" in _QUARANTINE_SQL


class TestReportConsumesWhatTheQueryProduces:
    """The renderer and the SELECT must not drift apart - a KeyError here would only
    surface on a live run, after the ingest had already happened."""

    def test_renderer_reads_only_projected_aliases(self, capsys):
        aliases = set(re.findall(r"\bAS\s+([a-z_][a-z0-9_]*)", _QUARANTINE_SQL, re.I))
        assert {"filename", "label", "n", "sample", "sample_raw"} <= aliases

        row = {a: (1 if a in {"n", "first_row", "last_row"} else f"<{a}>") for a in aliases}
        _print_report({"per_file": [], "status_counts": {}, "totals": {}},
                      {"files": 0, "rows": 0, "per_terminal": {}},
                      None, None, {}, [row], dry_run=False, elapsed=0.0)
        out = capsys.readouterr().out
        assert "<filename>" in out and "<label>" in out and "<sample>" in out

    def test_renderer_survives_a_file_level_finding(self, capsys):
        """row_number is nullable: a whole-file rejection carries no row."""
        row = {"filename": "bad.xml", "label": "read_error", "n": 1,
               "first_row": None, "last_row": None,
               "sample": "could not read file", "sample_raw": None}
        _print_report({"per_file": [], "status_counts": {}, "totals": {}},
                      {"files": 0, "rows": 0, "per_terminal": {}},
                      None, None, {}, [row], dry_run=False, elapsed=0.0)
        assert "file-level" in capsys.readouterr().out
