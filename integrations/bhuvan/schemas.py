"""Pydantic views over the Bhuvan WMS GetCapabilities document.

The raw OGC XML is parsed ONCE here into small typed models; nothing outside
this package ever sees ElementTree nodes. Both WMS 1.1.1 (no XML namespace)
and WMS 1.3.0 (``http://www.opengis.net/wms`` namespace) documents are
accepted — Bhuvan publishes 1.1.1 but the parser must not break if NRSC
upgrades the server.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List, Optional

from pydantic import BaseModel, Field

from .exceptions import BhuvanInvalidResponse

WMS_130_NS = "{http://www.opengis.net/wms}"


class WmsLayer(BaseModel):
    """One named (requestable) layer advertised by the WMS server."""

    name: str
    title: str = ""
    queryable: bool = False

    def as_api_dict(self) -> dict:
        """The exact shape the /api/bhuvan/layers surface promises."""
        return {"name": self.name, "title": self.title or self.name, "type": "WMS"}


class WmsCapabilities(BaseModel):
    """The subset of a GetCapabilities answer the gateway cares about."""

    version: str = ""
    service_title: str = ""
    layers: List[WmsLayer] = Field(default_factory=list)

    def find_layer(self, name: str) -> Optional[WmsLayer]:
        wanted = name.strip().lower()
        for layer in self.layers:
            if layer.name.strip().lower() == wanted:
                return layer
        return None


def _local(tag: str) -> str:
    """Element tag without its XML namespace (handles 1.1.1 and 1.3.0)."""
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str:
    for child in el:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def parse_capabilities(xml_text: str) -> WmsCapabilities:
    """Parse a WMS GetCapabilities XML body into :class:`WmsCapabilities`.

    Raises :class:`BhuvanInvalidResponse` on non-XML bodies, on an OGC
    ServiceExceptionReport (the server answered, but with an error document),
    and on XML that is not a WMS capabilities document.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise BhuvanInvalidResponse(
            f"Bhuvan WMS returned a non-XML body: {exc}") from exc

    root_tag = _local(root.tag)
    if root_tag == "ServiceExceptionReport":
        detail = " ".join(
            (child.text or "").strip() for child in root.iter() if child.text
        ).strip()
        raise BhuvanInvalidResponse(
            f"Bhuvan WMS answered a ServiceExceptionReport: {detail or 'no detail'}")
    if root_tag not in ("WMT_MS_Capabilities", "WMS_Capabilities"):
        raise BhuvanInvalidResponse(
            f"Bhuvan WMS returned unexpected XML root <{root_tag}>, "
            "expected a WMS capabilities document")

    version = (root.get("version") or "").strip()
    service_title = ""
    for child in root:
        if _local(child.tag) == "Service":
            service_title = _child_text(child, "Title")
            break

    # Named layers can nest arbitrarily deep (group layers); only elements with
    # a <Name> are requestable via GetMap, so only those are surfaced.
    layers: List[WmsLayer] = []
    seen: set[str] = set()
    for el in root.iter():
        if _local(el.tag) != "Layer":
            continue
        name = _child_text(el, "Name")
        if not name or name in seen:
            continue
        seen.add(name)
        layers.append(WmsLayer(
            name=name,
            title=_child_text(el, "Title"),
            queryable=(el.get("queryable") or "0").strip() in ("1", "true"),
        ))

    return WmsCapabilities(version=version, service_title=service_title, layers=layers)


__all__ = ["WmsLayer", "WmsCapabilities", "parse_capabilities"]
