"""Plate-string post-processor: regex normalisation + state-code whitelist +
character-confusion fixer.

The OCR head (``ocr.py``) returns a raw upper-cased string of [A-Z0-9]. Real-world
ANPR confuses visually similar glyphs (``0``/``O``, ``1``/``I``, ``8``/``B`` …).
We exploit the rigid structure of an Indian registration plate to repair those
swaps deterministically: positions the format says *must* be digits get the
letter->digit fixes applied, and positions that *must* be letters get the
inverse, before the candidate is validated against the canonical plate regexes.

Two plate families are recognised:

    Classic:  ^([A-Z]{2})[ -]?([0-9]{1,2})[ -]?([A-Z]{1,3})[ -]?([0-9]{4})$
              SS  DD   L[L][L]  NNNN     e.g.  MH04AB1234, GJ01AAA1234
    BH series ^([0-9]{2})BH([0-9]{4})([A-Z]{1,2})$
              YY  BH   NNNN   L[L]       e.g.  22BH1234AA

``postprocess(raw)`` returns a :class:`PlateResult` carrying the cleaned plate,
whether it validated, and the per-character fixes that were applied (useful for
the eval suite and for explaining a correction in the UI / logs).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# --- Canonical plate grammars (spec) ---------------------------------------
# The spec gives the format with optional separators; we match against the
# separator-stripped, upper-cased string so the groups are positionally clean.
_CLASSIC_RE = re.compile(r"^([A-Z]{2})([0-9]{1,2})([A-Z]{1,3})([0-9]{4})$")
_BH_RE = re.compile(r"^([0-9]{2})BH([0-9]{4})([A-Z]{1,2})$")

# As written in the prompt (kept for reference / external callers that want the
# separator-tolerant form). We normalise separators out before matching.
CLASSIC_PATTERN = r"^([A-Z]{2})[ -]?([0-9]{1,2})[ -]?([A-Z]{1,3})[ -]?([0-9]{4})$"
BH_PATTERN = r"^([0-9]{2})BH([0-9]{4})([A-Z]{1,2})$"

# Confusion-fix table: applied ONLY on positions the regex says must be digits
# (letter-shaped glyph -> the digit it was mistaken for).
LETTER_TO_DIGIT: Dict[str, str] = {"O": "0", "I": "1", "S": "5", "B": "8", "Z": "2"}
# Inverse, applied ONLY on positions that must be letters.
DIGIT_TO_LETTER: Dict[str, str] = {v: k for k, v in LETTER_TO_DIGIT.items()}

_RESOURCES = Path(__file__).resolve().parents[2] / "resources"
_STATE_CODES_FILE = _RESOURCES / "state_codes.txt"


def _load_state_codes() -> frozenset[str]:
    """Two-letter RTO state/UT codes (whitelist). Falls back to a built-in set
    if the resource file is missing so the service still runs."""
    try:
        codes = {
            line.strip().upper()
            for line in _STATE_CODES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        if codes:
            return frozenset(codes)
    except OSError:
        pass
    return _BUILTIN_STATE_CODES


# Current Indian state + UT RTO prefixes (incl. the ones that drifted, e.g. TS,
# LD, DD/DN merged). Used both as a fallback and as the source for writing the
# resource file via ``write_state_codes_resource``.
_BUILTIN_STATE_CODES = frozenset(
    {
        "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "DN", "GA", "GJ",
        "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN",
        "MP", "MZ", "NL", "OD", "OR", "PB", "PY", "RJ", "SK", "TN", "TR",
        "TS", "UK", "UA", "UP", "WB",
    }
)

STATE_CODES = _load_state_codes()


@dataclass
class PlateResult:
    raw: str
    plate: str                       # cleaned candidate (may equal raw)
    valid: bool                      # matched a canonical grammar + state code
    series: Optional[str] = None     # "classic" | "bh" | None
    state: Optional[str] = None      # SS prefix when classic
    fixes: List[str] = field(default_factory=list)  # e.g. ["pos5 O->0"]


def _strip(raw: str) -> str:
    """Upper-case and drop everything that is not A-Z/0-9."""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def _apply_template(s: str, template: str) -> tuple[str, List[str]]:
    """Fix glyphs against a positional template.

    ``template`` is a string the same length as ``s`` where each char is
    ``D`` (must be a digit) or ``L`` (must be a letter). Returns the repaired
    string and the list of human-readable fixes applied.
    """
    out: List[str] = []
    fixes: List[str] = []
    for i, ch in enumerate(s):
        kind = template[i]
        if kind == "D" and ch in LETTER_TO_DIGIT:
            fixed = LETTER_TO_DIGIT[ch]
            fixes.append(f"pos{i} {ch}->{fixed}")
            out.append(fixed)
        elif kind == "L" and ch in DIGIT_TO_LETTER:
            fixed = DIGIT_TO_LETTER[ch]
            fixes.append(f"pos{i} {ch}->{fixed}")
            out.append(fixed)
        else:
            out.append(ch)
    return "".join(out), fixes


def _classic_template(n: int) -> Optional[str]:
    """Positional D/L template for a classic plate of total length ``n``.

    Layout: 2 letters + (1-2 digits) + (1-3 letters) + 4 digits. We resolve the
    ambiguous middle widths by length: total length determines the split that
    keeps the trailing 4 digits and the 2-letter state prefix fixed.
    """
    # Fixed ends: LL .... DDDD. Middle = digits(1-2) + letters(1-3) = n-6 chars.
    middle = n - 6
    if middle < 2 or middle > 5:
        return None
    # Prefer 2 series-digits when room allows (common: SS DD LL NNNN = 10).
    digits = 2 if middle >= 3 else 1
    letters = middle - digits
    if not (1 <= letters <= 3):
        return None
    return "LL" + "D" * digits + "L" * letters + "DDDD"


def _bh_template() -> str:
    # YY BH NNNN LL  -> always 8-10 chars; BH letters are literal.
    return "DDLLDDDD"  # first 8 are fixed; trailing 1-2 letters handled below


def postprocess(raw: str) -> PlateResult:
    """Clean, repair, and validate a raw OCR plate string."""
    s = _strip(raw)
    if not s:
        return PlateResult(raw=raw, plate="", valid=False)

    # --- BH series first (its 'BH' literal is distinctive) -----------------
    # Length 9 (YYBHNNNNL) or 10 (YYBHNNNNLL). Template: DD BH DDDD L[L].
    if 9 <= len(s) <= 10 and s[2:4] in {"BH", "8H", "B4", "84"}:
        tmpl = "DDLLDDDD" + "L" * (len(s) - 8)
        # Force the BH literal positions to letters in the template, but the two
        # chars themselves must become 'B','H' — handle explicitly.
        fixed, fixes = _apply_template(s, tmpl)
        cand = fixed[:2] + "BH" + fixed[4:]
        if fixed[2:4] != "BH":
            fixes.append(f"pos2-3 {fixed[2:4]}->BH")
        m = _BH_RE.match(cand)
        if m:
            return PlateResult(raw=raw, plate=cand, valid=True, series="bh", fixes=fixes)

    # --- Classic series ----------------------------------------------------
    tmpl = _classic_template(len(s))
    if tmpl is not None:
        fixed, fixes = _apply_template(s, tmpl)
        m = _CLASSIC_RE.match(fixed)
        if m:
            state = m.group(1)
            valid_state = state in STATE_CODES
            return PlateResult(
                raw=raw,
                plate=fixed,
                valid=valid_state,
                series="classic",
                state=state,
                fixes=fixes,
            )

    # No grammar matched — return the stripped string, not validated.
    return PlateResult(raw=raw, plate=s, valid=False)


def write_state_codes_resource(path: Optional[Path] = None) -> Path:
    """Materialise the state-code whitelist resource file (one-time helper)."""
    dest = Path(path) if path else _STATE_CODES_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    header = "# Indian RTO state / UT codes (whitelist). One per line.\n"
    dest.write_text(header + "\n".join(sorted(_BUILTIN_STATE_CODES)) + "\n", encoding="utf-8")
    return dest


__all__ = [
    "PlateResult",
    "postprocess",
    "STATE_CODES",
    "LETTER_TO_DIGIT",
    "DIGIT_TO_LETTER",
    "CLASSIC_PATTERN",
    "BH_PATTERN",
    "write_state_codes_resource",
]
