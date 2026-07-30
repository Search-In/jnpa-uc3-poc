"""XML parsing + DocumentType detection — the second stage of the framework.

Turns a raw XML string into an ElementTree root (tolerant of a leading declaration
and of bare ``&`` in customer free-text), and identifies the PCS message type from
the ``<DocumentType>`` tag, falling back to the root element name.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

from .pcs_common import MarineParseError, clean

# Root element → canonical message type, for files that omit <DocumentType>.
_ROOT_TO_TYPE = {
    "VoyageRegistration": "CALINF",
    "BerthManagement": "BERMAN",
    "VesselProfile": "VESPRO",
    "VesselArrival": "VESARR",
    "VesselMovement": "VESDEP",
}

# Bare ampersands that are not part of an entity — customer free-text (owner names).
_BARE_AMP_RE = re.compile(r"&(?!#?\w+;)")


def safe_fromstring(xml: str) -> ET.Element:
    """Parse an XML string into a root Element. Retries once with bare ``&``
    sanitised, since customer free-text occasionally contains an unescaped ampersand."""
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        return ET.fromstring(_BARE_AMP_RE.sub("&amp;", xml))


def document_type(root: ET.Element) -> Optional[str]:
    """Resolve the PCS message type: the ``<DocumentType>`` tag if present, else the
    root element name mapped through the known-roots table. UPPER-cased."""
    dt = clean(root.findtext(".//DocumentType"))
    if dt:
        return dt.upper()
    return _ROOT_TO_TYPE.get(root.tag)


def require(root: ET.Element, container_tag: str, message: str) -> ET.Element:
    """Return the first ``container_tag`` under root, or raise MarineParseError —
    used by each message parser to assert its expected body block is present."""
    el = root.find(f".//{container_tag}")
    if el is None:
        raise MarineParseError(f"{message}: <{container_tag}> block not found")
    return el
