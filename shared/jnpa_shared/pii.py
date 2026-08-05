"""PII masking primitives (DPDP) — pure functions, no framework imports.

The UC-III corpus carries real personal data: ~31.8k driving licence numbers and
dates of birth in ``core.driver``, and ~2.2k transporter emails / mobiles /
addresses in ``core.transporter``. Before this module those values were serialised
verbatim by /api/drivers/master and /api/transporters.

Design rules:

  * **Fail closed.** The caller must prove entitlement to see cleartext. Anything
    that cannot prove it — including an unauthenticated demo build where
    ``AUTH_ENABLED=false`` and there is no principal at all — gets masked output.
  * **Shape preserving.** Masking never changes a payload's keys, types or
    nesting, so every existing client keeps working: a masked licence is still a
    string in the same field. No API is removed or renamed.
  * **Length hiding.** The mask is a FIXED six-star run, not a length-preserving
    one, so the ciphertext does not leak how long the original was::

        MH23 20170012229  ->  MH23******229

  * **Idempotent.** Masking an already-masked value is a no-op, so a value that
    passes through two serialisation layers is not double-starred.

Kept framework-free (no FastAPI/pydantic) so it is unit-testable on its own and
usable from both ``gateway/`` and ``services/``. Role resolution lives in
``gateway/pii.py``, which owns the request-scoped part.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Iterable, Mapping, MutableMapping

# The fixed-width mask run. Six stars regardless of input length (see module doc).
STARS = "******"

# A value that already contains a mask run is treated as masked (idempotence).
_MASKED_RE = re.compile(r"\*{3,}")

# --------------------------------------------------------------------------- #
# Field registry
# --------------------------------------------------------------------------- #
# Field names (case-insensitive, matched exactly against the serialised key) that
# carry PII, mapped to the masker that handles them. Aliases matter: the same
# datum surfaces under several names across routers — e.g. the driver licence is
# ``licence_number`` in core.driver, ``licence_no``/``licence_no_norm`` in the
# driver-master DTO, ``license_no`` in core.driver_identity, and ``driver_licence``
# on EIR / gate documents / job assignments.
LICENCE_FIELDS: frozenset[str] = frozenset({
    "licence_number", "licence_no", "licence_no_norm", "licence",
    "license_no", "license_number", "driver_licence", "driver_license",
    "dl_no", "dl_number",
})
DOB_FIELDS: frozenset[str] = frozenset({"dob", "date_of_birth", "birth_date"})
MOBILE_FIELDS: frozenset[str] = frozenset({
    "mobile", "mobile_number", "phone", "phone_number", "contact_number",
})
EMAIL_FIELDS: frozenset[str] = frozenset({"email", "contact_email", "notify_email", "email_id"})
ADDRESS_FIELDS: frozenset[str] = frozenset({"address", "residential_address", "postal_address"})
AADHAAR_FIELDS: frozenset[str] = frozenset({"aadhaar", "aadhaar_no", "aadhaar_number"})

#: Every PII field name this module knows about.
PII_FIELDS: frozenset[str] = (
    LICENCE_FIELDS | DOB_FIELDS | MOBILE_FIELDS | EMAIL_FIELDS
    | ADDRESS_FIELDS | AADHAAR_FIELDS
)

# ``aadhaar_masked`` is stored pre-masked at rest; leave it alone rather than
# masking a mask. Same for the boolean/enum companions that merely say whether a
# datum exists.
_PASSTHROUGH: frozenset[str] = frozenset({"aadhaar_masked"})


def masking_enabled() -> bool:
    """Global kill switch, default ON.

    Set ``PII_MASKING_ENABLED=false`` only for a controlled back-office
    deployment. It is deliberately opt-OUT: a missing/typo'd value keeps masking.
    """
    raw = os.environ.get("PII_MASKING_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def unmask_roles() -> frozenset[str]:
    """Roles entitled to cleartext PII (default: DTCCC_ADMIN + CUSTOMS).

    Override with ``PII_UNMASK_ROLES`` as a comma-separated list. An empty value
    means *nobody* sees cleartext, which is a valid hardening posture.
    """
    raw = os.environ.get("PII_UNMASK_ROLES")
    if raw is None:
        return frozenset({"DTCCC_ADMIN", "CUSTOMS"})
    return frozenset(p.strip().upper() for p in raw.split(",") if p.strip())


# --------------------------------------------------------------------------- #
# Value maskers
# --------------------------------------------------------------------------- #
def _already_masked(s: str) -> bool:
    return bool(_MASKED_RE.search(s))


def mask_licence(value: Any, *, keep_head: int = 4, keep_tail: int = 3) -> Any:
    """``MH23 20170012229`` -> ``MH23******229``.

    Keeps the leading RTO/state code (operationally useful — it is not personally
    identifying on its own) and a short tail so an operator can still
    eyeball-match a licence against a physical document, without the full number
    ever leaving the server. Values too short to split are masked entirely.
    """
    if value is None:
        return None
    s = str(value)
    if not s or _already_masked(s):
        return value
    if len(s) <= keep_head + keep_tail:
        return STARS
    return f"{s[:keep_head]}{STARS}{s[-keep_tail:]}"


def mask_dob(value: Any) -> Any:
    """Reduce a date of birth to its year: ``1994-09-10`` -> ``1994-**-**``.

    The birth YEAR is what the operational screens actually use (age band /
    licence-eligibility checks); the exact day is the identifying part.
    """
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return f"{value.year}-**-**"
    s = str(value)
    if not s or _already_masked(s):
        return value
    m = re.match(r"^(\d{4})[-/]", s)
    return f"{m.group(1)}-**-**" if m else STARS


def mask_mobile(value: Any, *, keep_tail: int = 3) -> Any:
    """``9876543210`` -> ``******210`` — enough to confirm a callback number."""
    if value is None:
        return None
    s = str(value)
    if not s or _already_masked(s):
        return value
    digits = re.sub(r"\D", "", s)
    if len(digits) <= keep_tail:
        return STARS
    return f"{STARS}{digits[-keep_tail:]}"


def mask_email(value: Any) -> Any:
    """``ravi.kumar@acme.co.in`` -> ``r******@acme.co.in``.

    The domain is retained because it identifies the *company*, not the person,
    and the transporter screens genuinely use it.
    """
    if value is None:
        return None
    s = str(value)
    if not s or _already_masked(s):
        return value
    if "@" not in s:
        return STARS
    local, _, domain = s.partition("@")
    head = local[:1] if local else ""
    return f"{head}{STARS}@{domain}" if domain else STARS


def mask_address(value: Any) -> Any:
    """Collapse a street address to its last comma-separated component.

    ``Plot 14, MIDC Taloja, Panvel 410208`` -> ``******, Panvel 410208``. The
    city/PIN tail is the part the logistics screens use for routing; the street
    line is the identifying part.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or _already_masked(s):
        return value
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) <= 1:
        return STARS
    return f"{STARS}, {parts[-1]}"


def mask_aadhaar(value: Any, *, keep_tail: int = 4) -> Any:
    """``123412341234`` -> ``******1234`` (UIDAI's own display convention)."""
    if value is None:
        return None
    s = str(value)
    if not s or _already_masked(s):
        return value
    digits = re.sub(r"\D", "", s)
    if len(digits) <= keep_tail:
        return STARS
    return f"{STARS}{digits[-keep_tail:]}"


#: field-name -> masker. Consulted by :func:`mask_payload`.
def _masker_for(field: str):
    f = field.lower()
    if f in _PASSTHROUGH:
        return None
    if f in LICENCE_FIELDS:
        return mask_licence
    if f in DOB_FIELDS:
        return mask_dob
    if f in MOBILE_FIELDS:
        return mask_mobile
    if f in EMAIL_FIELDS:
        return mask_email
    if f in ADDRESS_FIELDS:
        return mask_address
    if f in AADHAAR_FIELDS:
        return mask_aadhaar
    return None


# --------------------------------------------------------------------------- #
# Structure walker
# --------------------------------------------------------------------------- #
def mask_payload(obj: Any, *, extra_fields: Iterable[str] = ()) -> Any:
    """Recursively mask every known PII field in ``obj``.

    Walks dicts and lists/tuples of any depth, so a nested profile envelope
    (``{"driver": {...}, "licence": {...}}``) is covered without the caller
    naming each branch. Returns NEW containers — the input is never mutated, so a
    cached row shared between an entitled and a non-entitled caller cannot be
    corrupted by whichever one is served first.

    Non-container scalars pass through unchanged; unknown keys pass through
    unchanged. This makes the function safe to apply to any response body.
    """
    extra = frozenset(f.lower() for f in extra_fields)

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            out: MutableMapping[str, Any] = {}
            for k, v in node.items():
                key = str(k)
                masker = _masker_for(key)
                if masker is None and key.lower() in extra:
                    masker = mask_licence  # generic head/tail redaction
                if masker is not None and not isinstance(v, (Mapping, list, tuple)):
                    out[key] = masker(v)
                else:
                    out[key] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, tuple):
            return tuple(walk(v) for v in node)
        return node

    return walk(obj)


__all__ = [
    "STARS",
    "PII_FIELDS",
    "LICENCE_FIELDS",
    "DOB_FIELDS",
    "MOBILE_FIELDS",
    "EMAIL_FIELDS",
    "ADDRESS_FIELDS",
    "AADHAAR_FIELDS",
    "masking_enabled",
    "unmask_roles",
    "mask_licence",
    "mask_dob",
    "mask_mobile",
    "mask_email",
    "mask_address",
    "mask_aadhaar",
    "mask_payload",
]
