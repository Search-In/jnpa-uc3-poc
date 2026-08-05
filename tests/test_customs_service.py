"""Customs service tests — format detection (pure, always runs) + a data-gated
directory-import smoke over the real customer files.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.customs.service import (
    UnknownCustomsFormat,
    detect_parser,
)

DATA_DIR = Path(os.environ.get(
    "CUSTOMS_DATA_DIR", os.path.expanduser("~/Downloads/Digital Twin/data/5- Customs")))


def test_detect_parser_by_filename():
    # These formats are resolved by filename alone (no file access needed).
    assert detect_parser("/x/CHPOI03_1193612.xml")[1] == "IGM"
    assert detect_parser("/x/CHPOI10_9190401.xml")[1] == "OOC"
    assert detect_parser("/x/CHPOI13_2697411.xml")[1] == "SMTP"
    assert detect_parser("/x/RMS/1.txt")[1] == "RMS"


def test_detect_parser_rejects_unknown():
    with pytest.raises(UnknownCustomsFormat):
        detect_parser("/x/random.pdf")
    with pytest.raises(UnknownCustomsFormat):
        detect_parser("/x/notes.csv")


@pytest.mark.skipif(not DATA_DIR.is_dir(), reason=f"customs data dir absent: {DATA_DIR}")
def test_detect_xlsx_leo_vs_shipping_bill():
    # xlsx disambiguation reads the header row of the real files.
    _, leo_mod = detect_parser(str(DATA_DIR / "LEO" / "leodetails.xlsx"))
    _, sb_mod = detect_parser(str(DATA_DIR / "Shipping Bill" / "shippingbill.xlsx"))
    assert leo_mod == "LEO"
    assert sb_mod == "SHIPPING_BILL"


def test_derive_workflow_stages():
    from services.customs.service import CustomsService as CS
    assert CS._derive_workflow({"status": {"ooc_cleared": True, "declared_igm": True}})["import_stage"] == "OUT_OF_CHARGE"
    assert CS._derive_workflow({"status": {"rms_selected": True, "declared_igm": True}})["import_stage"] == "SCAN_SELECTED"
    assert CS._derive_workflow({"status": {"declared_igm": True}})["import_stage"] == "MANIFESTED"
    assert CS._derive_workflow({"status": {"smtp_bonded": True}})["transhipment"] == "BONDED"
    assert CS._derive_workflow({"status": {"ooc_cleared": True}})["cleared_for_release"] is True
    assert CS._derive_workflow({"status": None})["import_stage"] is None


@pytest.mark.skipif(not DATA_DIR.is_dir(), reason=f"customs data dir absent: {DATA_DIR}")
def test_discover_finds_all_customer_files():
    from services.customs.service import CustomsService
    files = CustomsService._discover(str(DATA_DIR))
    names = {os.path.basename(f) for f in files}
    # 4 IGM + 4 OOC + 6 SMTP + 4 RMS + 1 LEO + 1 SB = 20 (dotfiles like .DS_Store excluded)
    assert len(files) == 20
    assert "shippingbill.xlsx" in names and "leodetails.xlsx" in names
    assert not any(n.startswith(".") for n in names)


# --------------------------------------------------------------------------
# Container customs view — additive-field regression (GET /api/customs/
# containers/{cn}). The service enriches the repository view with workflow,
# last_event and import_export; the repository now also carries vessel (with
# voyage) and message_id. These tests assert those fields WITHOUT touching the
# existing backward-compatible keys. An injected fake repository keeps them
# DB-free and deterministic (like tests/test_customs_api.py::FakeCustomsRepo).
# --------------------------------------------------------------------------
class _FakeContainerRepo:
    """Minimal repo stand-in returning a fixed container_customs view + events."""

    def __init__(self, view: dict, events: list | None = None) -> None:
        self._view = view
        self._events = events if events is not None else []

    async def container_customs(self, container_no: str) -> dict:
        return dict(self._view, container_no=container_no)

    async def list_events(self, **kwargs) -> list:
        return list(self._events)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _import_container_view() -> dict:
    """An import container: IGM-declared + RMS-selected, with vessel/message id."""
    return {
        "container_no": "CAIU6709422",
        "status": {"container_no": "CAIU6709422", "igm_no": "1194792",
                   "declared_igm": True, "rms_selected": True,
                   "ooc_cleared": False, "smtp_bonded": False},
        "vessel": {"igm_no": "1194792", "igm_date": "2026-07-01",
                   "vessel_code": "INMAA1", "voyage_no": "V-123",
                   "shipping_line_code": "MAEU", "port_of_arrival": "INNSA1",
                   "expected_arrival": None, "entry_inward": None, "message_id": 42},
        "message_id": 42,
        "igm": [{"igm_no": "1194792", "line_no": 30, "container_no": "CAIU6709422"}],
        "ooc": [], "smtp": [], "rms": [],
    }


def test_container_customs_returns_additive_fields():
    from services.customs.service import CustomsService
    event = {"id": 7, "event": "customs.rms_selected", "module": "RMS",
             "reference": "1191409", "container_no": "CAIU6709422",
             "created_at": "2026-07-02T10:00:00Z"}
    svc = CustomsService(repository=_FakeContainerRepo(_import_container_view(), [event]))

    view = _run(svc.container_customs("CAIU6709422"))

    # New additive fields are present and correctly derived.
    assert view["vessel"]["vessel_code"] == "INMAA1"
    assert view["vessel"]["voyage_no"] == "V-123"          # voyage
    assert view["message_id"] == 42
    assert view["import_export"] == "IMPORT"               # IGM present -> import track
    assert view["last_event"]["event"] == "customs.rms_selected"
    assert view["workflow"]["import_stage"] == "SCAN_SELECTED"

    # Backward compatibility: every pre-existing key is preserved.
    for key in ("container_no", "status", "igm", "ooc", "smtp", "rms"):
        assert key in view


def test_container_customs_transhipment_and_missing_event():
    from services.customs.service import CustomsService
    # SMTP-only box -> transhipment track; no events -> last_event is None.
    smtp_view = {"status": None, "vessel": None, "message_id": None,
                 "igm": [], "ooc": [], "smtp": [{"smtp_no": "2697414"}], "rms": []}
    svc = CustomsService(repository=_FakeContainerRepo(smtp_view, []))
    view = _run(svc.container_customs("SMTP1"))
    assert view["import_export"] == "TRANSHIPMENT"
    assert view["last_event"] is None
    assert view["vessel"] is None and view["message_id"] is None


def test_container_customs_no_documents_import_export_none():
    from services.customs.service import CustomsService
    empty_view = {"status": None, "vessel": None, "message_id": None,
                  "igm": [], "ooc": [], "smtp": [], "rms": []}
    svc = CustomsService(repository=_FakeContainerRepo(empty_view, []))
    view = _run(svc.container_customs("UNKNOWN1"))
    assert view["import_export"] is None
    assert view["workflow"]["import_stage"] is None


def test_derive_import_export_static():
    from services.customs.service import CustomsService as CS
    assert CS._derive_import_export({"igm": [{"x": 1}], "ooc": [], "smtp": []}) == "IMPORT"
    assert CS._derive_import_export({"igm": [], "ooc": [{"x": 1}], "smtp": []}) == "IMPORT"
    assert CS._derive_import_export({"igm": [], "ooc": [], "smtp": [{"x": 1}]}) == "TRANSHIPMENT"
    assert CS._derive_import_export({"igm": [], "ooc": [], "smtp": []}) is None
