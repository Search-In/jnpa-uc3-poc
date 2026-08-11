"""SecureVision <-> JNPA camera mapping.

SecureVision does not publish a camera-registry API, and this application has
its own camera identifiers (gateway/routers/anpr.py ``KNOWN_CAMERAS``:
``CAM-COR-01``…``CAM-COR-06`` on the corridor, ``CAM-<TERMINAL>-ENT`` at the
gates). Two registries, no join key, no discovery endpoint.

The only honest way to bridge them is an EXPLICIT operator-maintained mapping.
Guessing — string-matching ``CAM-01`` onto ``CAM-COR-01`` because they look
similar — would attach a detection to the wrong physical camera, and every
downstream decision (which gate is degraded, which zone was entered) would
inherit that error silently.

So: when a mapping exists, we resolve it and say so. When it does not, we say
``mapped: false`` and the UI renders "Camera mapping unavailable" rather than a
plausible-looking gate name. There is deliberately no fallback that invents one.

Configuration (env, gateway-only):

    SECUREVISION_CAMERA_MAP   JSON object {"<securevision_code>": "<jnpa_code>"}
                              e.g. {"CAM-01": "CAM-NSICT-ENT", "CAM-02": "CAM-COR-01"}

An unparseable value is logged once and treated as "no mapping configured" —
a malformed operator edit must not take the whole integration down.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional

from gateway.logging import get_logger

log = get_logger("services.securevision.cameras")

ENV_VAR = "SECUREVISION_CAMERA_MAP"

_cache: Optional[Dict[str, str]] = None
_cache_raw: Optional[str] = None


def _load(raw: str) -> Dict[str, str]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        log.warning("securevision_camera_map_invalid", error=str(exc))
        return {}
    if not isinstance(parsed, dict):
        log.warning("securevision_camera_map_not_an_object",
                    kind=type(parsed).__name__)
        return {}
    out: Dict[str, str] = {}
    for sv_code, jnpa_code in parsed.items():
        if isinstance(sv_code, str) and isinstance(jnpa_code, str) \
                and sv_code.strip() and jnpa_code.strip():
            out[sv_code.strip().upper()] = jnpa_code.strip().upper()
    return out


def camera_map() -> Dict[str, str]:
    """The configured SecureVision -> JNPA camera mapping (possibly empty).

    Re-parsed only when the env value changes, so a test can set the variable
    and see it take effect without reaching into module internals.
    """
    global _cache, _cache_raw
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if raw != _cache_raw or _cache is None:
        _cache_raw = raw
        _cache = _load(raw) if raw else {}
    return _cache


def reset_cache() -> None:
    """Drop the parsed mapping (tests)."""
    global _cache, _cache_raw
    _cache, _cache_raw = None, None


def to_jnpa(securevision_code: Optional[str]) -> Optional[str]:
    """The JNPA camera this SecureVision code refers to, or None when the
    mapping is not configured. None means "unknown", never "probably this one"."""
    if not securevision_code:
        return None
    return camera_map().get(securevision_code.strip().upper())


def to_securevision(jnpa_code: Optional[str]) -> Optional[str]:
    """Reverse lookup, for building an upload's ``camera_code`` from a JNPA
    camera the operator picked. Ambiguous reverse mappings (two SecureVision
    codes pointing at one JNPA camera) resolve to the first configured entry —
    the mapping is expected to be 1:1 and is operator-maintained."""
    if not jnpa_code:
        return None
    target = jnpa_code.strip().upper()
    for sv_code, mapped in camera_map().items():
        if mapped == target:
            return sv_code
    return None


def describe(securevision_code: Optional[str]) -> Dict[str, object]:
    """Mapping verdict for one SecureVision camera code, as the UI consumes it.

    ``mapped=False`` is a first-class answer: the screen shows "Camera mapping
    unavailable" and still displays the vendor's own code, so an operator can
    fix the configuration without guessing what the vendor called the camera.
    """
    code = (securevision_code or "").strip() or None
    jnpa = to_jnpa(code)
    return {
        "securevision_code": code,
        "jnpa_camera_id": jnpa,
        "mapped": bool(jnpa),
        "map_configured": bool(camera_map()),
    }


__all__ = ["camera_map", "reset_cache", "to_jnpa", "to_securevision", "describe",
           "ENV_VAR"]
