"""Deterministic plates that link to the Vahan simulator's dataset.

The simulator (``ingest/vahan_sim``) builds its 25k-plate corpus by calling
``_plate_for_index(i)`` for ``i = 0, 1, 2, …``. We mirror that *exact* algorithm
here so the plate this fleet assigns to device index ``i`` is the very plate the
Vahan simulator generated for the same index — i.e. every truck's plate resolves
to a real RC via ``GET /vahan/rc/{plate}``. Keeping a tiny copy (rather than
importing ``vahan_sim``) lets the trucking-app container ship without a build
dependency on the Vahan service while staying byte-for-byte compatible.

If the two ever drift, the linkage check in the tests will catch it.
"""
from __future__ import annotations

import hashlib

# These three constants MUST match ingest/vahan_sim/seed.py exactly.
_SEED = "jnpa-uc3-vahan-sim-v1"
_SERIES = [
    ("MH04", "Maharashtra", "MH04"),
    ("MH43", "Maharashtra", "MH43"),
    ("MH06", "Maharashtra", "MH06"),
    ("GJ01", "Gujarat", "GJ01"),
    ("KA01", "Karnataka", "KA01"),
    ("TN22", "Tamil Nadu", "TN22"),
    ("KL07", "Kerala", "KL07"),
]
_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"  # I/O/Q dropped (digit confusion)


def _h(*parts: object) -> int:
    """Stable 64-bit-ish int hash from SEED + parts (mirrors vahan_sim._h)."""
    raw = _SEED + "|" + "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big")


def plate_for_index(i: int) -> str:
    """The plate the Vahan simulator generated for dataset index ``i``.

    ~2% BH-series; the rest classic SS DD LL NNNN across the 7 RTO series.
    """
    if _h("isbh", i) % 100 < 2:
        yy = 21 + (_h("bhyy", i) % 5)
        num = _h("bhnum", i) % 10_000
        l1 = _LETTERS[_h("bhl1", i) % len(_LETTERS)]
        l2 = _LETTERS[_h("bhl2", i) % len(_LETTERS)]
        return f"{yy:02d}BH{num:04d}{l1}{l2}"

    prefix, _state, _rto = _SERIES[i % len(_SERIES)]
    l1 = _LETTERS[_h("l1", i) % len(_LETTERS)]
    l2 = _LETTERS[_h("l2", i) % len(_LETTERS)]
    num = (_h("num", i) % 9999) + 1
    return f"{prefix}{l1}{l2}{num:04d}"
