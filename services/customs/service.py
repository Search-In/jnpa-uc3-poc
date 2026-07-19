"""Customs service — the single import/read entry point.

Thin over :class:`services.customs.repository.CustomsRepository`: it owns format
detection, observability (one structured log line per import), customs-event
emission (only from ACTUAL processing), and the file/directory import
orchestration. Mirrors :mod:`services.cargo.service`: stateless apart from the DSN,
so one shared instance is safe. The repository is dependency-injected so tests can
pass a fake.
"""
from __future__ import annotations

import hashlib
import os
from time import perf_counter
from typing import Any, Callable, Mapping, Optional

from jnpa_shared.logging import get_logger

from .parsers import (
    CustomsParseError,
    ParsedMessage,
    parse_chpoi03,
    parse_chpoi10,
    parse_chpoi13,
    parse_leo_xlsx,
    parse_rms_txt,
    parse_shipping_bill_xlsx,
)
from .repository import CustomsRepository

log = get_logger("services.customs.service")

# Customs lifecycle event names — emitted ONLY on a real, successful import.
EVENT_IGM_FILED = "customs.igm_filed"
EVENT_OOC_ISSUED = "customs.ooc_issued"
EVENT_SMTP_ISSUED = "customs.smtp_issued"
EVENT_RMS_SELECTED = "customs.rms_selected"
EVENT_LEO_GRANTED = "customs.leo_granted"
EVENT_SB_FILED = "customs.shipping_bill_filed"
# Cargo-binding events (emitted when a customs fact moves a real container's status).
EVENT_CARGO_CLEARED = "customs.cargo_cleared"
EVENT_CARGO_SCAN_HOLD = "customs.cargo_scan_hold"

_MODULE_EVENT = {
    "IGM": EVENT_IGM_FILED, "OOC": EVENT_OOC_ISSUED, "SMTP": EVENT_SMTP_ISSUED,
    "RMS": EVENT_RMS_SELECTED, "LEO": EVENT_LEO_GRANTED, "SHIPPING_BILL": EVENT_SB_FILED,
}

# The official customer sub-folder layout under $CUSTOMS_DATA_DIR.
DATA_SUBDIRS = ("IGM", "OOC", "SMTP", "RMS", "LEO", "Shipping Bill")


class UnknownCustomsFormat(Exception):
    """Raised when a file cannot be matched to a known customs format."""


def _xlsx_kind(path: str) -> str:
    """Disambiguate a customs ``.xlsx`` by its header row: LEO carries a Rotation
    Number / LEO Date column, the Shipping Bill sheet does not."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        header = next(ws.iter_rows(values_only=True), ()) or ()
    finally:
        wb.close()
    cols = {str(c).strip().upper() for c in header if c is not None}
    if "ROTATION NUMBER" in cols or "LEO DATE" in cols:
        return "LEO"
    return "SHIPPING_BILL"


def detect_parser(path: str) -> tuple[Callable[[str], ParsedMessage], str]:
    """Resolve ``path`` to its ``(parser, module)`` by filename + (for xlsx) header.

    Raises :class:`UnknownCustomsFormat` for anything that is not a recognised
    customs file (so junk in the data dir is reported, never silently mis-imported)."""
    name = os.path.basename(path).upper()
    if name.startswith("CHPOI03"):
        return parse_chpoi03, "IGM"
    if name.startswith("CHPOI10"):
        return parse_chpoi10, "OOC"
    if name.startswith("CHPOI13"):
        return parse_chpoi13, "SMTP"
    if name.endswith(".TXT"):
        return parse_rms_txt, "RMS"
    if name.endswith(".XLSX"):
        kind = _xlsx_kind(path)
        return (parse_leo_xlsx, "LEO") if kind == "LEO" else (parse_shipping_bill_xlsx, "SHIPPING_BILL")
    raise UnknownCustomsFormat(f"unrecognised customs file: {path}")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class CustomsService:
    """Import orchestration + reads for the customs document layer."""

    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[CustomsRepository] = None) -> None:
        self._repo = repository or CustomsRepository(dsn)

    @staticmethod
    def _ms(t0: float) -> float:
        return round((perf_counter() - t0) * 1000, 1)

    # ------------------------------------------------------------------ import
    async def import_file(self, path: str) -> dict:
        """Parse + persist one customs file. Returns the repository import-result dict
        augmented with ``source_file``. On a real, non-duplicate success emits exactly
        one customs event (from actual processing). A parse failure is recorded as a
        FAILED result without raising, so a batch import never aborts on one bad file."""
        t0 = perf_counter()
        source_file = os.path.basename(path)
        try:
            parser, module = detect_parser(path)
            parsed = parser(path)
        except (CustomsParseError, UnknownCustomsFormat) as exc:
            log.warning("customs.import.parse_failed", source_file=source_file, error=str(exc))
            return {"source_file": source_file, "module": None,
                    "import_status": "FAILED", "record_count": 0, "imported_count": 0,
                    "error_count": 1, "duplicate": False, "error_detail": str(exc)}

        result = await self._repo.persist(
            parsed, source_file=source_file, source_sha256=_sha256(path),
            file_size=os.path.getsize(path))
        result["source_file"] = source_file

        if result["import_status"] == "SUCCESS":
            await self._emit_import_event(module, parsed, result)

        log.info("customs.import", module=module, source_file=source_file,
                 status=result["import_status"], record_count=result["record_count"],
                 imported_count=result["imported_count"], latency_ms=self._ms(t0))
        return result

    async def _emit_import_event(self, module: str, parsed: ParsedMessage,
                                 result: dict) -> None:
        """Emit one summary customs event for a freshly imported message."""
        event = _MODULE_EVENT.get(module)
        if not event:
            return
        payload = {"message_id": result.get("message_id"),
                   "record_count": result.get("record_count"),
                   "imported_count": result.get("imported_count")}
        try:
            await self._repo.record_event(
                event, module=module, reference=parsed.message.get("primary_ref"),
                payload=payload)
        except Exception as exc:  # noqa: BLE001 — an event write must never fail an import
            log.warning("customs.event.record_failed", event=event, error=str(exc))

    async def import_directory(self, root: str) -> dict:
        """Import EVERY recognised customs file under ``root`` (the official folder
        layout: IGM/OOC/SMTP/RMS/LEO/Shipping Bill). Returns a per-file result list
        plus totals. Files are processed deterministically (sorted) and independently
        — one failure never blocks the rest."""
        results: list[dict] = []
        for path in self._discover(root):
            results.append(await self.import_file(path))
        totals = {
            "files": len(results),
            "succeeded": sum(1 for r in results if r["import_status"] == "SUCCESS"),
            "duplicate": sum(1 for r in results if r["import_status"] == "SKIPPED_DUPLICATE"),
            "failed": sum(1 for r in results if r["import_status"] == "FAILED"),
            "records": sum(r["record_count"] for r in results),
            "imported": sum(r["imported_count"] for r in results),
        }
        return {"root": root, "totals": totals, "results": results}

    @staticmethod
    def _discover(root: str) -> list[str]:
        """All importable files under ``root`` (recursive), sorted, skipping dotfiles
        and non-customs artefacts (e.g. .DS_Store)."""
        found: list[str] = []
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.startswith(".") or fn.startswith("~$"):
                    continue
                if fn.upper().endswith((".XML", ".TXT", ".XLSX")):
                    found.append(os.path.join(dirpath, fn))
        return sorted(found)

    async def import_configured(self) -> dict:
        """Import the configured customer data directory ($CUSTOMS_DATA_DIR). Raises
        FileNotFoundError if it is not present (the router maps that to 404/409)."""
        root = os.environ.get(
            "CUSTOMS_DATA_DIR", os.path.expanduser("~/Downloads/Digital Twin/data/5- Customs"))
        if not os.path.isdir(root):
            raise FileNotFoundError(root)
        return await self.import_directory(root)

    # --------------------------------------------------- cargo binding (workflow)
    async def reconcile_cargo(self) -> dict:
        """Apply the customs → cargo workflow: drive jnpa.cargo.customs_status from the
        imported customs documents (Out-Of-Charge -> CLEARED; RMS scan selection ->
        UNDER_INSPECTION) for containers that exist in cargo. Emits one customs event
        per changed container and raises a scan-hold notification on the EXISTING cargo
        notification feed. Idempotent: a second run changes nothing (no events)."""
        t0 = perf_counter()
        changes = await self._repo.reconcile_cargo_status()
        for cn in changes["cleared"]:
            await self._safe_event(EVENT_CARGO_CLEARED, container_no=cn,
                                   payload={"customs_status": "CLEARED"})
        for cn in changes["under_inspection"]:
            await self._safe_event(EVENT_CARGO_SCAN_HOLD, container_no=cn,
                                   payload={"customs_status": "UNDER_INSPECTION"})
            try:
                await self._repo.create_cargo_notification(
                    cn, notification_type="CUSTOMS_SCAN_REQUIRED", severity="HIGH",
                    message=f"Container {cn} selected by RMS for customs scanning.")
            except Exception as exc:  # noqa: BLE001 — notification must not fail reconcile
                log.warning("customs.notify_failed", container_no=cn, error=str(exc))
        log.info("customs.reconcile", cleared=len(changes["cleared"]),
                 under_inspection=len(changes["under_inspection"]), latency_ms=self._ms(t0))
        return {"cleared": len(changes["cleared"]),
                "under_inspection": len(changes["under_inspection"]),
                "cleared_containers": changes["cleared"],
                "under_inspection_containers": changes["under_inspection"]}

    async def _safe_event(self, event: str, *, container_no: str, payload: dict) -> None:
        try:
            await self._repo.record_event(event, module="CARGO_BINDING",
                                          container_no=container_no, reference=container_no,
                                          payload=payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("customs.event.record_failed", event=event, error=str(exc))

    @staticmethod
    def _derive_workflow(view: Mapping[str, Any]) -> dict:
        """Derive the customs workflow stage of a container from its document facts.

        Import track (IGM -> RMS -> OOC -> Release) and transhipment track (SMTP -> bond)
        are both container-keyed; the export track (Shipping Bill -> LEO) is SB-keyed and
        so is not part of a per-container view."""
        st = view.get("status") or {}
        import_stage = None
        if st.get("ooc_cleared"):
            import_stage = "OUT_OF_CHARGE"        # customs-cleared for release
        elif st.get("rms_selected"):
            import_stage = "SCAN_SELECTED"        # risk-selected for scanning
        elif st.get("declared_igm"):
            import_stage = "MANIFESTED"           # declared on an IGM
        return {
            "import_stage": import_stage,
            "transhipment": "BONDED" if st.get("smtp_bonded") else None,
            "cleared_for_release": bool(st.get("ooc_cleared")),
        }

    # -------------------------------------------------------------------- reads
    async def list_events(self, **filters: Any) -> list[dict]:
        return await self._repo.list_events(**filters)

    async def summary(self) -> dict:
        return await self._repo.summary()

    async def list_messages(self, *, filters, limit, offset):
        return await self._repo.list_messages(filters=filters, limit=limit, offset=offset)

    async def count_messages(self, *, filters):
        return await self._repo.count_messages(filters=filters)

    async def get_message(self, message_id: int, *, with_errors: bool = False) -> Optional[dict]:
        msg = await self._repo.get_message(message_id)
        if msg is None:
            return None
        if with_errors:
            msg["errors"] = await self._repo.list_message_errors(message_id, limit=500, offset=0)
        return msg

    async def list_igm(self, *, filters, limit, offset):
        return await self._repo.list_igm(filters=filters, limit=limit, offset=offset)

    async def count_igm(self, *, filters):
        return await self._repo.count_igm(filters=filters)

    async def list_igm_containers(self, *, filters, limit, offset):
        return await self._repo.list_igm_containers(filters=filters, limit=limit, offset=offset)

    async def count_igm_containers(self, *, filters):
        return await self._repo.count_igm_containers(filters=filters)

    async def list_ooc(self, *, filters, limit, offset):
        return await self._repo.list_ooc(filters=filters, limit=limit, offset=offset)

    async def count_ooc(self, *, filters):
        return await self._repo.count_ooc(filters=filters)

    async def list_smtp(self, *, filters, limit, offset):
        return await self._repo.list_smtp(filters=filters, limit=limit, offset=offset)

    async def count_smtp(self, *, filters):
        return await self._repo.count_smtp(filters=filters)

    async def list_rms(self, *, filters, limit, offset):
        return await self._repo.list_rms(filters=filters, limit=limit, offset=offset)

    async def count_rms(self, *, filters):
        return await self._repo.count_rms(filters=filters)

    async def list_leo(self, *, filters, limit, offset):
        return await self._repo.list_leo(filters=filters, limit=limit, offset=offset)

    async def count_leo(self, *, filters):
        return await self._repo.count_leo(filters=filters)

    async def list_shipping_bills(self, *, filters, limit, offset):
        return await self._repo.list_shipping_bills(filters=filters, limit=limit, offset=offset)

    async def count_shipping_bills(self, *, filters):
        return await self._repo.count_shipping_bills(filters=filters)

    async def container_customs(self, container_no: str) -> dict:
        view = await self._repo.container_customs(container_no)
        view["workflow"] = self._derive_workflow(view)
        return view
