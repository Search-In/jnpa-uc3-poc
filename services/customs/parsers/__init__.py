"""Customs file parsers — pure ``file -> typed dict`` decoders.

Each parser is a PURE function of its input bytes/path: no DB, no network, no
clock, no RNG — so parsing the same official customer file always yields the same
structure, and every parser is unit-testable against the real samples under
``$CUSTOMS_DATA_DIR`` without a running server or database.

  chpoi03  IGM   (XML)   vessel -> cargo lines -> containers
  chpoi10  OOC   (XML)   bill-of-entry / out-of-charge -> containers -> items
  chpoi13  SMTP  (XML)   sub-manifest transhipment permit (flat lines)
  rms_txt  RMS   (.txt)  container scanning selection list
  leo_xlsx LEO   (.xlsx) let export order rows
  sb_xlsx  SB    (.xlsx) shipping bill rows

All parsers return a ``ParsedMessage`` (see ``common``): a message-level envelope
plus the nested payload the repository persists verbatim.
"""
from __future__ import annotations

from .chpoi03 import parse_chpoi03
from .chpoi10 import parse_chpoi10
from .chpoi13 import parse_chpoi13
from .common import CustomsParseError, ParsedMessage
from .leo_xlsx import parse_leo_xlsx
from .rms_txt import parse_rms_txt
from .sb_xlsx import parse_shipping_bill_xlsx

__all__ = [
    "ParsedMessage",
    "CustomsParseError",
    "parse_chpoi03",
    "parse_chpoi10",
    "parse_chpoi13",
    "parse_rms_txt",
    "parse_leo_xlsx",
    "parse_shipping_bill_xlsx",
]
