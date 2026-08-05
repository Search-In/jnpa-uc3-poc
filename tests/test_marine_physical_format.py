"""Regression guard — the ledger's physical_format must always satisfy its CHECK.

WHY THIS FILE EXISTS
--------------------
core.marine_import_files.physical_format is constrained:

    CHECK (physical_format IN ('CSV','XLS','XLSX','PDF','XML','LOG','ZIP','SHP','JSON'))

Adding the 'JOURNAL' routing format (PCS message journals) without translating it made
every BERALT upload die at the ledger INSERT with CheckViolationError on
marine_import_files_physical_format_check — AFTER validation had already reported
valid=90 / importable=90, so the parser looked fine and the failure surfaced as a bare
status=FAILED.

The column means CONTAINER, not routing: `_physical_format` already translates 'SHP' to
'ZIP' for the same reason. These tests assert every routing format detect_format() can
emit maps to a CHECK-legal container, so a NEW format can never reintroduce this.

Pure: fixtures are bytes built in-memory, no DB and no corpus.
"""
from __future__ import annotations

import pytest

from services.marine.parsers.envelope import detect_format
from services.marine.upload_service import _physical_format

#: The constraint's accepted set — kept in sync with gateway/marine_ext.py.
ALLOWED = {"CSV", "XLS", "XLSX", "PDF", "XML", "LOG", "ZIP", "SHP", "JSON"}

JOURNAL_CSV = (
    b"NLP Outbound Data Report,,,,,,,,\r\n"
    b"From Date: ,'10/07/2026 00:00',,,,,,,\r\n"
    b"To Date: ,'15/07/2026 00:00',,,,,,,\r\n"
    b",,,,,,,,\r\n,,,,,,,,\r\n,,,,,,,,\r\n"
    b"COMMON_REF_NO,IMO_NUMBER,VIA_NO,VOYAGE_NO,VESSEL_NAME,MESSAGE_TYPE,RESPONSE_DATE,REQUEST,RESPONSE\r\n"
    b'2026070254753806,9680956,S6544,346,BOONYA NAREE,BERALT,15/07/2026 20:58,'
    b'"{""ReqBody"":{ ""XML"":""<BerthAllotment><DocumentHeader><DocumentReference>'
    b'<DocumentType>BERALT</DocumentType></DocumentReference></DocumentHeader></BerthAllotment>""}}",ok\r\n'
)
TEMPLATE_CSV = b"VCN,VIA,IMO,Vessel Name\r\nINNSA1NF0R2968,S0776,9291987,TSS AMBER\r\n"
PCS_XML = b"<?xml version='1.0'?><BerthApplication><DocumentType>BERMAN</DocumentType></BerthApplication>"
LOG_FILE = b'{"ReqBody":{ "XML":"<VesselArrival><DocumentType>VESARR</DocumentType></VesselArrival>"}}'
PDF_FILE = b"%PDF-1.4\n%stub"
BARE_SHP = b"\x00\x00\x27\x0a" + b"\x00" * 32


def _shapefile_zip() -> bytes:
    """A REAL zip carrying a .shp member.

    detect_format peeks the zip entries to tell a shapefile bundle from an .xlsx, so a
    bare 'PK\\x03\\x04' + padding is not enough — it is an unreadable zip and correctly
    falls through to XLSX. The bundle has to be genuine for this case to exercise the
    SHP -> ZIP translation at all.
    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sea_channel.shp", b"\x00\x00\x27\x0a" + b"\x00" * 32)
        z.writestr("sea_channel.dbf", b"stub")
    return buf.getvalue()


ZIP_FILE = _shapefile_zip()
XLS_FILE = b"\xd0\xcf\x11\xe0" + b"\x00" * 32
JSON_FILE = b'{"survey": {"drawing_no": "X"}, "soundings": []}'

CASES = [
    ("journal.csv", JOURNAL_CSV, "JOURNAL", "CSV"),
    ("template.csv", TEMPLATE_CSV, "CSV", "CSV"),
    ("BERMAN_1.xml", PCS_XML, "XML", "XML"),
    ("VESARR.log", LOG_FILE, "LOG", "LOG"),
    ("crafts.pdf", PDF_FILE, "PDF", "PDF"),
    ("channels.zip", ZIP_FILE, "SHP", "ZIP"),
    ("channel.shp", BARE_SHP, "SHP", "SHP"),
    ("legacy.xls", XLS_FILE, "XLSX", "XLSX"),
    ("bathy.json", JSON_FILE, "JSON", "JSON"),
]


class TestLedgerValueAlwaysSatisfiesTheCheck:
    @pytest.mark.parametrize("name,body,routing,physical", CASES,
                             ids=[c[0] for c in CASES])
    def test_routing_and_physical_format(self, name, body, routing, physical):
        assert detect_format(name, body) == routing, f"{name}: routing format changed"
        assert _physical_format(name, body) == physical, f"{name}: ledger value changed"

    @pytest.mark.parametrize("name,body,routing,physical", CASES,
                             ids=[c[0] for c in CASES])
    def test_ledger_value_is_check_legal(self, name, body, routing, physical):
        got = _physical_format(name, body)
        assert got in ALLOWED, (
            f"{name} -> physical_format={got!r} violates "
            f"marine_import_files_physical_format_check. Translate it in "
            f"_physical_format (as SHP->ZIP and JOURNAL->CSV are) rather than widening "
            f"the constraint — the column means container, not routing format.")


class TestJournalSpecifically:
    """The regression that broke BERALT."""

    def test_journal_routes_as_journal_but_is_ledgered_as_csv(self):
        assert detect_format("j.csv", JOURNAL_CSV) == "JOURNAL"   # parsing unchanged
        assert _physical_format("j.csv", JOURNAL_CSV) == "CSV"    # ledger legal

    def test_journal_is_never_written_to_the_ledger(self):
        assert "JOURNAL" not in ALLOWED
        assert _physical_format("j.csv", JOURNAL_CSV) != "JOURNAL"

    def test_template_csv_is_untouched_by_the_translation(self):
        """The fix must not make an ordinary vessel-call template CSV look like a journal
        or vice versa — they share the .csv extension and differ only by content."""
        assert detect_format("t.csv", TEMPLATE_CSV) == "CSV"
        assert _physical_format("t.csv", TEMPLATE_CSV) == "CSV"


class TestConstraintSetStaysInSync:
    """If someone widens the CHECK, ALLOWED above must be updated with it."""

    def test_allowed_matches_the_ddl(self):
        from gateway.marine_ext import _DDL
        checks = [s for s in _DDL if "marine_import_files_physical_format_check" in s]
        assert checks, "CHECK constraint DDL not found"
        newest = checks[-1]
        for fmt in ALLOWED:
            assert f"'{fmt}'" in newest, f"{fmt} missing from the DDL CHECK"
