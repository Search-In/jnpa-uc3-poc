"""Pure file -> typed-dict decoders for the shipping-line documents (no I/O to a DB).

Mirrors :mod:`services.customs.parsers`: deterministic, unit-testable functions that
turn the heterogeneous per-terminal IAL/EAL/EDO files into a uniform
:class:`ParsedList` envelope. The repository is the only SQL speaker.
"""
from __future__ import annotations

from .common import ParsedList, ShippingLineParseError
from .edo_codeco import parse_edo
from .flat_tabular import looks_record_labelled, parse_flat, read_rows
from .record_labeled import parse_record_labelled

__all__ = [
    "ParsedList",
    "ShippingLineParseError",
    "parse_edo",
    "parse_flat",
    "parse_record_labelled",
    "looks_record_labelled",
    "read_rows",
]
