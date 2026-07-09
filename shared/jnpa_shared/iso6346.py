"""ISO 6346 container-number utilities (shared by UC-2 and UC-3).

Implements the real check-digit algorithm so simulator-generated and
mapper-parsed container numbers are verifiably valid, not merely regex-shaped.
Ported from the UC-2 twin's ``packages/schemas/src/entities/iso6346.ts`` so both
digital twins compute identical check digits and a container number minted in
UC-2 validates unchanged in UC-3 (the "follow-the-box" cross-twin key).

Format: ``OOO U NNNNNN C``
  * ``OOO``     owner code (3 letters)
  * ``U``       equipment category identifier (U, J or Z)
  * ``NNNNNN``  6-digit serial
  * ``C``       check digit (0-9; a computed value of 10 maps to 0 by convention)
"""
from __future__ import annotations

import re
from typing import Optional, TypedDict

CONTAINER_NO_RE = re.compile(r"^[A-Z]{3}[UJZ]\d{6}\d$")


def _build_letter_values() -> dict[str, int]:
    """ISO 6346 letter weights: values start at 10, skipping every multiple of
    11 (11, 22, 33, ...) — a documented quirk of the standard. A=10, B=12, ..."""
    mapping: dict[str, int] = {}
    value = 10
    for i in range(26):
        if value % 11 == 0:
            value += 1  # skip multiples of 11
        mapping[chr(65 + i)] = value
        value += 1
    return mapping


LETTER_VALUES = _build_letter_values()


def compute_check_digit(prefix10: str) -> int:
    """Compute the ISO 6346 check digit for the first 10 characters
    (4 letters + 6 digits). Returns 0-9."""
    if len(prefix10) != 10:
        raise ValueError(
            f"ISO6346: expected 10 chars before check digit, got {len(prefix10)}"
        )
    total = 0
    for i, ch in enumerate(prefix10):
        if ch.isalpha():
            base = LETTER_VALUES[ch.upper()]
        elif ch.isdigit():
            base = int(ch)
        else:
            raise ValueError(f"ISO6346: invalid character {ch!r} in prefix")
        total += base * (2 ** i)
    remainder = total % 11
    return 0 if remainder == 10 else remainder


def is_valid_container_no(value: str) -> bool:
    """True if ``value`` is a structurally- and check-digit-valid ISO 6346 number."""
    if not value or not CONTAINER_NO_RE.match(value):
        return False
    try:
        expected = compute_check_digit(value[:10])
    except ValueError:
        return False
    return int(value[10]) == expected


def with_check_digit(prefix10: str) -> str:
    """Given a 10-char prefix (owner+category+serial), return the full 11-char
    container number with a valid appended check digit."""
    return prefix10 + str(compute_check_digit(prefix10))


class ContainerParts(TypedDict):
    owner_code: str
    category: str
    serial: str
    check_digit: int


def parse_container_no(value: str) -> Optional[ContainerParts]:
    """Parse the structural parts of a container number (does not validate the
    check digit — use :func:`is_valid_container_no` for that)."""
    if not value or not CONTAINER_NO_RE.match(value):
        return None
    return ContainerParts(
        owner_code=value[:3],
        category=value[3],
        serial=value[4:10],
        check_digit=int(value[10]),
    )


__all__ = [
    "CONTAINER_NO_RE",
    "LETTER_VALUES",
    "compute_check_digit",
    "is_valid_container_no",
    "with_check_digit",
    "parse_container_no",
    "ContainerParts",
]
