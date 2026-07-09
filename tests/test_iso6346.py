"""Unit tests for the shared ISO 6346 container-number validator.

Ported check-digit algorithm must match the ISO 6346 standard and the UC-2 twin
so a container minted in one twin validates unchanged in the other (the
"follow-the-box" cross-twin key).
"""
from __future__ import annotations

import pytest

from jnpa_shared.iso6346 import (
    compute_check_digit,
    is_valid_container_no,
    parse_container_no,
    with_check_digit,
)


# Canonical published ISO 6346 examples (owner+U+serial -> full valid number).
# These check digits are the textbook results of the standard's algorithm.
@pytest.mark.parametrize(
    "prefix10,expected_full",
    [
        ("CSQU305438", "CSQU3054383"),  # the standard's own worked example
        ("MSCU123456", "MSCU1234566"),
        ("MAEU765432", "MAEU7654320"),
        ("HLXU123456", "HLXU1234561"),
    ],
)
def test_known_check_digits(prefix10: str, expected_full: str) -> None:
    assert with_check_digit(prefix10) == expected_full
    assert is_valid_container_no(expected_full)
    assert compute_check_digit(prefix10) == int(expected_full[10])


def test_wrong_check_digit_is_invalid() -> None:
    # MSCU1234566 is valid; every other trailing digit must be rejected.
    valid = "MSCU1234566"
    assert is_valid_container_no(valid)
    for d in "0123456789":
        cand = valid[:10] + d
        if d == valid[10]:
            continue
        assert not is_valid_container_no(cand), cand


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "MSCU123456",       # too short (no check digit)
        "MSCU12345678",     # too long
        "MSC1234566U",      # digits/letters misplaced
        "1234U123456",      # owner not letters
        "MSCX1234566",      # category not in U/J/Z
        "mscu1234566",      # lowercase
        "MSCU12A4566",      # non-digit in serial
    ],
)
def test_structurally_invalid_rejected(bad: str) -> None:
    assert not is_valid_container_no(bad)


def test_category_j_and_z_allowed() -> None:
    for cat in ("U", "J", "Z"):
        full = with_check_digit(f"ABC{cat}000000")
        assert is_valid_container_no(full)


def test_parse_container_no() -> None:
    parts = parse_container_no("MSCU1234566")
    assert parts == {
        "owner_code": "MSC",
        "category": "U",
        "serial": "123456",
        "check_digit": 6,
    }
    assert parse_container_no("nonsense") is None


def test_compute_check_digit_length_guard() -> None:
    with pytest.raises(ValueError):
        compute_check_digit("TOOSHORT")


def test_check_digit_range() -> None:
    # A computed remainder of 10 maps to 0; result is always a single digit 0-9.
    for i in range(0, 1000, 7):
        full = with_check_digit(f"ABC U{i:06d}".replace(" ", ""))
        assert full[10] in "0123456789"
        assert is_valid_container_no(full)
