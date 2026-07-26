"""Normalization helpers shared by extractors and the batch verifier.

Ports the confusion-folding / alnum-norm tricks from docs/ocr_gate_docs.py and
adds Indian plate + ISO-6346 container validators used as extraction filters.
"""
from __future__ import annotations

import re

# Same fold as UC-3 plate grammar / docs OCR verifier.
CONFUSION = str.maketrans(
    {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"}
)

# Common thermal-OCR glyph repairs applied before extraction.
OCR_CHAR_REPAIR = str.maketrans({
    "@": "0",
    "€": "E",
    "£": "E",
})

# Indian state / RTO prefixes seen on JNPA slips (extend as needed).
_STATE_CODES = {
    "MH", "GJ", "KA", "TN", "KL", "AP", "TS", "DL", "UP", "RJ", "MP", "HR",
    "PB", "WB", "OR", "OD", "BR", "JH", "CG", "GA", "HP", "UK", "AS", "BH",
}

# Classic: MH43BX1488 / MH04AB1234 — require 3–4 digit serial (rejects JO5P7).
PLATE_RE = re.compile(
    r"\b("
    r"(?:[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4})"
    r"|(?:BH\d{2}[A-Z]{1,2}\d{4})"
    r")\b",
    re.IGNORECASE,
)

# ISO-6346 container: 4 letters + 7 digits.
CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})\b", re.IGNORECASE)
# Loose container (spaces / @ / O mixed in) — cleaned by recover_container.
CONTAINER_LOOSE_RE = re.compile(
    r"\b([A-Z]{3,4}[\s0-9O@]{6,12})\b",
    re.IGNORECASE,
)

EIR_NO_RE = re.compile(r"\b(\d{6,10})\b")
BAT_RE = re.compile(r"\b([A-Z]\d{3})\b", re.IGNORECASE)
ISO_CODE_RE = re.compile(r"\b(\d{4})\b")


def repair_ocr_glyphs(s: str) -> str:
    """Cheap character-level repairs common on thermal EIR photos."""
    if not s:
        return ""
    return s.translate(OCR_CHAR_REPAIR)


def norm_alnum(s: str) -> str:
    """Uppercase and strip everything but A-Z0-9."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def norm_spaced(s: str) -> str:
    """Uppercase, collapse runs of non-alphanumerics to single spaces."""
    return re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).strip()


def fold_confusion(s: str) -> str:
    return norm_alnum(s).translate(CONFUSION)


def is_valid_plate(value: str) -> bool:
    v = norm_alnum(value)
    if not v or not PLATE_RE.fullmatch(v):
        return False
    if v.startswith("BH"):
        return len(v) >= 10
    return v[:2] in _STATE_CODES


def is_valid_container(value: str) -> bool:
    v = norm_alnum(value)
    # After glyph repair, also accept O→0 folded digit run.
    if CONTAINER_RE.fullmatch(v):
        return True
    folded = v.translate(str.maketrans({"O": "0"}))
    return bool(CONTAINER_RE.fullmatch(folded))


def recover_container(text: str) -> str | None:
    """Find a container number even when OCR inserts spaces/@/O/S confusions."""
    repaired = repair_ocr_glyphs(text or "")
    m = CONTAINER_RE.search(repaired)
    if m and is_valid_container(m.group(1)):
        return norm_alnum(m.group(1)).upper()

    # MRKUS@14206 / MSMUL908508 → fold S/L/O/@ on the serial into digits.
    digit_fold = str.maketrans({
        "@": "0", "O": "0", "S": "5", "I": "1", "L": "1", "B": "8", "Z": "2",
    })
    for m in re.finditer(r"\b([A-Z]{4})([A-Z0-9@]{6,10})\b", repaired, re.IGNORECASE):
        letters = m.group(1).upper()
        tail = m.group(2).upper().translate(digit_fold)
        digits = re.sub(r"[^0-9]", "", tail)
        if len(digits) >= 7:
            cand = letters + digits[:7]
            if is_valid_container(cand):
                return cand

    for m in CONTAINER_LOOSE_RE.finditer(repaired):
        cand = norm_alnum(m.group(1)).translate(str.maketrans({"O": "0"}))
        m2 = re.match(r"^([A-Z]{4})(\d{7})$", cand)
        if m2:
            return cand

    # Join split forms like "MSMU 1908508"
    m = re.search(
        r"\b([A-Z]{4})\s*[O0@]?[\sO0@]*(\d{3})\s*(\d{4})\b",
        repaired,
        re.IGNORECASE,
    )
    if m:
        letters = re.sub(r"[^A-Z]", "", m.group(1).upper())
        digits = re.sub(r"[^0-9]", "", (m.group(2) + m.group(3)).translate(OCR_CHAR_REPAIR))
        if len(letters) == 4 and len(digits) == 7:
            return letters + digits
    return None


def recover_plate(text: str) -> str | None:
    """Return the best Indian plate found in text (strict state-code check)."""
    repaired = repair_ocr_glyphs(text or "")
    for m in PLATE_RE.finditer(repaired):
        cand = norm_alnum(m.group(1)).upper()
        if is_valid_plate(cand):
            return cand
    return recover_noisy_plate(repaired)


def recover_noisy_plate(text: str) -> str | None:
    """Recover plates from garbled OCR like ``WIH4GAF 43 75`` → ``MH46AF4375``.

    Uses a structured Indian-plate parse (state + RTO + series + serial) with
    common thermal confusions, rather than an unbounded character beam.
    """
    repaired = repair_ocr_glyphs(text or "")
    # Prefer digit-preserving spacing: "WIH4GAF 43 75" → keep digit groups.
    spaced = re.sub(r"[^A-Z0-9]+", " ", repaired.upper()).strip()
    # Drop leading label tokens when the whole truck line is passed in.
    parts = [p for p in spaced.split() if p not in {"TRUCK", "TRK", "LIC", "NO", "VEHICLE", "VEH"}]
    spaced = " ".join(parts)
    compact = norm_alnum(spaced)
    if not compact or len(compact) < 8:
        return None
    if is_valid_plate(compact):
        return compact

    candidates: list[str] = []

    # Join trailing digit groups into a 4-digit serial when OCR splits them.
    # e.g. "WIH4GAF 43 75" → head WIH4GAF + serial 4375
    if len(parts) >= 2 and all(p.isdigit() for p in parts[-2:]) and len("".join(parts[-2:])) == 4:
        serial = "".join(parts[-2:])
        head = norm_alnum("".join(parts[:-2]))
        built = _assemble_plate(head, serial)
        if built:
            candidates.append(built)

    # Compact: last 4 chars digits → serial
    m = re.match(r"^([A-Z0-9]*?[A-Z])(\d{4})$", compact)
    if not m:
        m = re.match(r"^([A-Z0-9]+)(\d{4})$", compact)
    if m:
        built = _assemble_plate(m.group(1), m.group(2))
        if built:
            candidates.append(built)

    # Prefix-only repairs on the full compact string
    for a, b in (("VI", "MH"), ("WI", "MH"), ("VN", "MH"), ("WH", "MH"), ("MI", "MH"), ("NK", "MH")):
        if compact.startswith(a):
            cand = b + compact[2:]
            # Drop one spurious letter after MH (WIH→MH H…)
            if len(cand) >= 11 and cand[2].isalpha() and cand[3].isdigit():
                alt = cand[:2] + cand[3:]
                if is_valid_plate(alt):
                    candidates.append(alt)
            # G as '6' inside RTO: MH4GAF4375 → MH46AF4375
            g6 = re.sub(r"^(MH\d)G", r"\g<1>6", cand)
            if g6 != cand and is_valid_plate(g6):
                candidates.append(g6)
            if is_valid_plate(cand):
                candidates.append(cand)

    for cand in candidates:
        if is_valid_plate(cand):
            return cand
    return None


def _assemble_plate(head: str, serial: str) -> str | None:
    """Build MH##XX#### from a noisy head + 4-digit serial."""
    if not re.fullmatch(r"\d{4}", serial):
        return None
    h = norm_alnum(head)
    # Normalize state to MH when OCR shows WI/VI/WH…
    if h.startswith(("VI", "WI", "VN", "WH", "MI", "NK", "NH")):
        h = "MH" + h[2:]
    elif h.startswith("W") and len(h) > 1 and h[1] in "IHN":
        h = "MH" + h[2:]  # drop spurious I/H after W→M
    if not h.startswith("MH"):
        # Last resort: force MH if head looks like a truck OCR blob
        if re.match(r"^[A-Z]{2,4}\d", h):
            h = "MH" + re.sub(r"^[A-Z]+", "", h, count=1)
            if not h.startswith("MH"):
                h = "MH" + h
        else:
            return None

    rest = h[2:]
    # Drop a spurious letter stuck before the RTO digits (MH H4GAF → MH4GAF)
    if rest and rest[0].isalpha() and len(rest) > 1 and rest[1].isdigit():
        rest = rest[1:]

    # Interpret G as 6 when it sits in the RTO digit slot: 4G → 46
    rest = re.sub(r"^(\d)G", r"\g<1>6", rest)
    rest = rest.translate(str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"}))

    m = re.match(r"^(\d{1,2})([A-Z]{1,3})(\d*)$", rest)
    if not m:
        return None
    rto, series, extra = m.group(1), m.group(2), m.group(3)
    if len(rto) == 1:
        rto = rto + "0"  # unlikely; prefer failing
        return None
    # If extra digits leaked into head, prefer the explicit serial arg
    cand = f"MH{rto}{series}{serial}"
    return cand if is_valid_plate(cand) else None


def looks_like_iso_code(value: str) -> bool:
    return bool(ISO_CODE_RE.fullmatch(norm_alnum(value)))
