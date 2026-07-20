"""Shipping-line service — the single import/read entry point.

Thin over :class:`services.shipping_lines.repository.ShippingLinesRepository`: it
owns file-format detection (list-type from folder, terminal from filename, shape
from content), observability (one structured log line per import), shipping-line
event emission (only from ACTUAL processing), and the file/directory import
orchestration. Mirrors :mod:`services.customs.service`: stateless apart from the DSN.
"""
from __future__ import annotations

import hashlib
import os
from time import perf_counter
from typing import Any, Optional

from jnpa_shared.logging import get_logger

from .parsers import (
    ParsedList,
    ShippingLineParseError,
    looks_record_labelled,
    parse_edo,
    parse_flat,
    parse_record_labelled,
    read_rows,
)
from .repository import ShippingLinesRepository

log = get_logger("services.shipping_lines.service")

# Shipping-line lifecycle event names — emitted ONLY on a real, successful import.
EVENT_IAL_IMPORTED = "shipping_line.ial_imported"
EVENT_EAL_IMPORTED = "shipping_line.eal_imported"
EVENT_EDO_IMPORTED = "shipping_line.edo_imported"
_LIST_EVENT = {"IAL": EVENT_IAL_IMPORTED, "EAL": EVENT_EAL_IMPORTED, "EDO": EVENT_EDO_IMPORTED}

_TERMINALS = ("APMT", "BMCT", "GTI", "NSFT", "NSICT", "NSIGT")
# Default customer folder (verified on disk). Case matches the actual directory so
# the importer works on a case-sensitive filesystem too — unlike import_customs.py.
DEFAULT_DATA_DIR = os.environ.get(
    "SHIPPING_LINES_DATA_DIR",
    os.path.expanduser("~/Downloads/Digital Twin/Data/4-Shipping Lines"))


class UnknownShippingLineFormat(Exception):
    """Raised when a file cannot be matched to a known shipping-line format."""


def detect_format(path: str) -> tuple[str, str, str]:
    """Resolve ``path`` to ``(list_type, terminal, physical_format)``.

    list_type comes from the parent folder (EAL_FORMAT / IAL FORMAT / EDO), the
    terminal from a filename token, the physical format from the extension (EDO's
    .xlsx carries CODECO XML so it is flagged CODECO_XML)."""
    up = path.upper()
    folder = os.path.basename(os.path.dirname(path)).upper()
    ext = os.path.splitext(path)[1].lower()

    if "EDO" in folder or os.path.basename(up).startswith("EDO"):
        list_type = "EDO"
    elif "EAL" in folder or "/EAL" in up or os.path.basename(up).startswith("EAL"):
        list_type = "EAL"
    elif "IAL" in folder or os.path.basename(up).startswith("IAL"):
        list_type = "IAL"
    else:
        raise UnknownShippingLineFormat(f"cannot determine list type: {path}")

    squished = os.path.basename(up).replace(" ", "").replace("_", "")
    terminal = next((t for t in _TERMINALS if t in squished), "OTHER")

    if list_type == "EDO":
        fmt = "CODECO_XML"
    elif ext == ".csv":
        fmt = "CSV"
    elif ext == ".xls":
        fmt = "XLS"
    elif ext == ".xlsx":
        fmt = "XLSX"
    else:
        raise UnknownShippingLineFormat(f"unsupported extension: {path}")
    return list_type, terminal, fmt


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse(path: str, list_type: str, terminal: str) -> ParsedList:
    if list_type == "EDO":
        return parse_edo(path, terminal=terminal)
    rows = read_rows(path)
    if looks_record_labelled(rows):
        return parse_record_labelled(path, list_type=list_type, terminal=terminal)
    return parse_flat(path, list_type=list_type, terminal=terminal)


class ShippingLinesService:
    """Import orchestration + reads for the shipping-line document layer."""

    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[ShippingLinesRepository] = None) -> None:
        self._repo = repository or ShippingLinesRepository(dsn)

    @staticmethod
    def _ms(t0: float) -> float:
        return round((perf_counter() - t0) * 1000, 1)

    # ------------------------------------------------------------------ import
    async def import_file(self, path: str) -> dict:
        """Parse + persist one shipping-line file. A parse failure is recorded as a
        FAILED result without raising, so a batch import never aborts on one bad file."""
        t0 = perf_counter()
        source_file = os.path.basename(path)
        try:
            list_type, terminal, fmt = detect_format(path)
            parsed = _parse(path, list_type, terminal)
        except (ShippingLineParseError, UnknownShippingLineFormat) as exc:
            log.warning("shipping_lines.import.parse_failed", source_file=source_file, error=str(exc))
            return {"source_file": source_file, "list_type": None, "terminal": None,
                    "import_status": "FAILED", "record_count": 0, "imported_count": 0,
                    "error_count": 1, "duplicate": False, "error_detail": str(exc)}

        result = await self._repo.persist(
            parsed, source_file=source_file, source_sha256=_sha256(path),
            physical_format=fmt, file_size=os.path.getsize(path))
        result["source_file"] = source_file

        if result["import_status"] == "SUCCESS":
            await self._emit_import_event(list_type, result)

        log.info("shipping_lines.import", list_type=list_type, terminal=terminal,
                 source_file=source_file, status=result["import_status"],
                 record_count=result["record_count"], imported_count=result["imported_count"],
                 latency_ms=self._ms(t0))
        return result

    async def _emit_import_event(self, list_type: str, result: dict) -> None:
        event = _LIST_EVENT.get(list_type)
        if not event:
            return
        payload = {"file_id": result.get("file_id"), "terminal": result.get("terminal"),
                   "record_count": result.get("record_count"),
                   "imported_count": result.get("imported_count")}
        try:
            await self._repo.record_event(event, module=list_type,
                                          reference=result.get("source_file"), payload=payload)
        except Exception as exc:  # noqa: BLE001 — an event write must never fail an import
            log.warning("shipping_lines.event.record_failed", event=event, error=str(exc))

    async def import_directory(self, root: str) -> dict:
        """Import EVERY recognised shipping-line file under ``root`` (recursive, sorted,
        independent). Returns a per-file result list plus totals."""
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
        found: list[str] = []
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.startswith(".") or fn.startswith("~$"):
                    continue
                if fn.lower().endswith((".csv", ".xls", ".xlsx")):
                    found.append(os.path.join(dirpath, fn))
        return sorted(found)

    async def import_configured(self) -> dict:
        """Import the configured customer data directory ($SHIPPING_LINES_DATA_DIR)."""
        root = DEFAULT_DATA_DIR
        if not os.path.isdir(root):
            raise FileNotFoundError(root)
        return await self.import_directory(root)

    # -------------------------------------------------------------------- reads
    async def summary(self) -> dict:
        return await self._repo.summary()

    async def list_containers(self, *, filters, limit, offset):
        return await self._repo.list_containers(filters=filters, limit=limit, offset=offset)

    async def count_containers(self, *, filters):
        return await self._repo.count_containers(filters=filters)

    async def container_view(self, container_no: str) -> dict:
        return await self._repo.container_view(container_no)

    async def list_by_bl(self, bl: str, *, limit, offset):
        return await self._repo.list_by_bl(bl, limit=limit, offset=offset)

    async def count_by_bl(self, bl: str) -> int:
        return await self._repo.count_by_bl(bl)

    async def get_line(self, line_code: str):
        return await self._repo.get_line(line_code)

    async def list_lines(self, *, limit, offset):
        return await self._repo.list_lines(limit=limit, offset=offset)

    async def count_lines(self) -> int:
        return await self._repo.count_lines()

    async def list_delivery_orders(self, *, filters, limit, offset):
        return await self._repo.list_delivery_orders(filters=filters, limit=limit, offset=offset)

    async def count_delivery_orders(self, *, filters):
        return await self._repo.count_delivery_orders(filters=filters)

    async def list_files(self, *, filters, limit, offset):
        return await self._repo.list_files(filters=filters, limit=limit, offset=offset)

    async def count_files(self, *, filters):
        return await self._repo.count_files(filters=filters)

    async def get_file(self, file_id: int, *, with_errors: bool = False):
        row = await self._repo.get_file(file_id)
        if row is None:
            return None
        if with_errors:
            row["errors"] = await self._repo.list_file_errors(file_id, limit=500, offset=0)
        return row

    async def list_events(self, **filters: Any) -> list[dict]:
        return await self._repo.list_events(**filters)
