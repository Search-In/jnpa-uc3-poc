"""Tests for the Shipping Lines Data-Upload sub-module (module 4).

Pure parser/validation tests (no DB) + a service-level test with a fake repository
that asserts the validate (dry-run) and import (persist + PARTIAL) orchestration and
re-upload idempotency — mirroring tests/test_customs_api.py's fake-repo approach.
"""
from __future__ import annotations

import asyncio
import csv
import io

import pytest

from services.shipping_lines import upload_parsers as P
from services.shipping_lines.upload_service import ShippingLinesUploadService


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode()


# --------------------------------------------------------------- templates
def test_templates_have_required_and_are_reparseable():
    for lt in ("IAL", "EAL", "EDO"):
        t = P.template_csv(lt)
        header, rows = P.read_rows_from_bytes(t.encode(), f"{lt}.csv")
        # the '#' guidance line is skipped; the example row remains
        assert rows, f"{lt} template should carry an example row"
        for label in P._REQUIRED[lt]:
            assert label in header, f"{lt} template missing required column {label}"


# --------------------------------------------------------------- required columns
def test_missing_required_column_is_rejected_with_friendly_error():
    content = _csv([["Cntr", "ISO"], ["ABCD1234567", "2210"]])
    header, rows = P.read_rows_from_bytes(content, "bad.csv")
    res = P.parse("EAL", header, rows)
    assert res.rejected
    details = " ".join(e["error_detail"] for e in res.errors)
    assert "Gross Weight column not found" in details
    assert "please download the latest template" in details.lower()


# --------------------------------------------------------------- dynamic mapping
def test_alias_variations_map_to_container_number():
    for head in ("Container No", "Container Number", "Cntr No", "CNTR_NO"):
        content = _csv([[head, "ISO Code", "Gross Weight", "Shipping Line", "Category"],
                        ["MSCU1234565", "2210", "19880", "MSC", "IMPORT"]])
        header, rows = P.read_rows_from_bytes(content, "u.csv")
        res = P.parse("IAL", header, rows)
        assert not res.rejected, f"header '{head}' should satisfy Container Number"
        assert len(res.records) == 1
        assert res.records[0]["container_no"] == "MSCU1234565"


# --------------------------------------------------------------- row validation
def test_invalid_weight_and_empty_required_and_duplicate():
    content = _csv([
        ["Container Number", "ISO Code", "Gross Weight", "Shipping Line", "Category"],
        ["MSCU1234565", "2210", "19880", "MSC", "IMPORT"],       # valid
        ["TEMU1234561", "4510", "22.5", "ONE", "IMPORT"],        # valid (MT -> kg)
        ["BADWEIGHT00", "2210", "not-a-number", "MSC", "IMPORT"],  # invalid weight
        ["NOLINE00000", "2210", "1000", "", "IMPORT"],           # empty required (line)
        ["MSCU1234565", "2210", "19880", "MSC", "IMPORT"],       # duplicate of row 1
    ])
    header, rows = P.read_rows_from_bytes(content, "u.csv")
    res = P.parse("IAL", header, rows)
    assert len(res.records) == 2
    assert res.invalid_count == 2                      # bad weight + empty line
    assert res.duplicate_count == 1
    codes = {e["error_code"] for e in res.errors}
    assert "invalid_weight" in codes and "empty_required" in codes
    # MT normalisation
    assert res.records[1]["gross_weight_kg"] == 22500.0


def test_weight_magnitude_inference_matches_directory_importer():
    content = _csv([["Container Number", "ISO Code", "Gross Weight", "Shipping Line", "Category"],
                    ["MSCU1234565", "2210", "19880", "MSC", "IMPORT"]])
    header, rows = P.read_rows_from_bytes(content, "u.csv")
    res = P.parse("EAL", header, rows)
    assert res.records[0]["gross_weight_kg"] == 19880.0      # KG kept, not *1000


# --------------------------------------------------------------- EDO flat template
def test_edo_flat_template_maps_delivery_orders():
    content = _csv([["Container Number", "Gate Pass No", "Vehicle No", "Shipping Agent"],
                    ["SAJU2031655", "16494337", "MH43U7042", "UNF"],
                    ["NOPASS00000", "", "MH01AA1111", "MSC"]])     # missing gate pass
    header, rows = P.read_rows_from_bytes(content, "edo.csv")
    res = P.parse("EDO", header, rows)
    assert res.target == "delivery"
    assert len(res.records) == 1
    assert res.records[0]["gate_pass_no"] == "16494337"
    assert res.invalid_count == 1


# --------------------------------------------------------------- service orchestration
class _FakeRepo:
    """In-memory stand-in — records persist calls; simulates sha256 dedup."""

    def __init__(self):
        self.persisted = []
        self.row_errors = {}
        self.partial = {}
        self.events = []
        self._sha = {}
        self._next = 100

    async def persist(self, parsed, *, source_file, source_sha256, physical_format,
                      file_size=None, uploaded_by=None, source="DIRECTORY"):
        if source_sha256 in self._sha:
            fid = self._sha[source_sha256]
            return {"file_id": fid, "list_type": parsed.header["list_type"], "terminal": "OTHER",
                    "import_status": "SKIPPED_DUPLICATE", "record_count": parsed.record_count,
                    "imported_count": 0, "error_count": 0, "duplicate": True}
        fid = self._next
        self._next += 1
        self._sha[source_sha256] = fid
        n = len(parsed.containers) + len(parsed.delivery_orders)
        self.persisted.append((fid, source, uploaded_by, n))
        return {"file_id": fid, "list_type": parsed.header["list_type"], "terminal": "OTHER",
                "import_status": "SUCCESS", "record_count": parsed.record_count,
                "imported_count": n, "error_count": 0, "duplicate": False}

    async def add_row_errors(self, file_id, errors):
        self.row_errors[file_id] = list(errors)

    async def mark_partial(self, file_id, *, error_count):
        self.partial[file_id] = error_count

    async def record_event(self, *a, **k):
        self.events.append((a, k))

    async def record_rejected_upload(self, **k):
        fid = self._next
        self._next += 1
        return fid


def test_service_validate_is_dry_run_and_import_is_partial_then_idempotent():
    repo = _FakeRepo()
    svc = ShippingLinesUploadService(repository=repo)
    good = _csv([
        ["Container Number", "ISO Code", "Gross Weight", "Shipping Line", "Category"],
        ["MSCU1234565", "2210", "19880", "MSC", "IMPORT"],
        ["BADWEIGHT00", "2210", "nan", "MSC", "IMPORT"],       # invalid -> skipped
    ])

    async def run():
        v = await svc.validate("IAL", good, "future.csv", "tester")
        assert v["status"] == "VALIDATED" and v["valid"] is True
        assert v["summary"]["valid"] == 1 and v["summary"]["invalid"] == 1
        assert not repo.persisted, "validate must NOT persist (dry-run)"

        r1 = await svc.import_file("IAL", good, "future.csv", "tester")
        assert r1["status"] == "PARTIAL"        # 1 valid imported, 1 invalid skipped
        assert r1["imported"] == 1 and r1["invalid"] == 1
        assert repo.partial and repo.row_errors  # PARTIAL flag + errors recorded

        r2 = await svc.import_file("IAL", good, "future.csv", "tester")
        assert r2["status"] == "SKIPPED_DUPLICATE"   # same bytes -> idempotent
        assert len(repo.persisted) == 1              # only the first import wrote rows

    asyncio.run(run())


def test_service_rejects_bad_template_without_persisting():
    repo = _FakeRepo()
    svc = ShippingLinesUploadService(repository=repo)
    bad = _csv([["Cntr", "ISO"], ["ABCD1234567", "2210"]])

    async def run():
        r = await svc.import_file("EAL", bad, "bad.csv", "tester")
        assert r["status"] == "REJECTED"
        assert not repo.persisted

    asyncio.run(run())
