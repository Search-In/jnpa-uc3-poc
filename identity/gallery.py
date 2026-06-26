"""Deterministic synthetic enrolled-driver gallery.

DPDP posture: every entry here is a SYNTHETIC, CONSENTED record — there are no
real drivers and no real biometrics. The "enrolment embedding" is the
deterministic synthetic vector from :mod:`identity.embeddings`, standing in for
the template a production face-recognition enrolment would store under explicit,
consent-gated processing. Names and licence numbers are generated from a fixed
seed so the gallery is identical across runs, hosts, and CI.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .embeddings import synth_embedding

SEED = "jnpa-uc3-identity-gallery-v1"
DEFAULT_GALLERY_SIZE = 50

# Deterministic name parts (no real persons; obviously synthetic).
_FIRST = [
    "Aarav", "Rohan", "Imran", "Vikram", "Sanjay", "Prakash", "Deepak",
    "Manoj", "Suresh", "Ramesh", "Anil", "Kiran", "Faruk", "Joseph", "Gurpreet",
]
_LAST = [
    "Sharma", "Patil", "Khan", "Reddy", "Nair", "Gowda", "Singh", "Das",
    "Mehta", "Pillai", "Yadav", "Shaikh", "Verma", "Naidu", "Pawar",
]


@dataclass(frozen=True)
class EnrolledDriver:
    """One synthetic, consented enrolment record."""

    driver_id: str
    name: str
    license_no: str
    embedding: List[float] = field(repr=False)
    synthetic: bool = True
    consented: bool = True
    # Optional reference-photo pointer (object-store URL). None until a consented
    # reference is enrolled; the raw embedding/template still never leaves.
    photo_url: Optional[str] = None

    def public(self) -> Dict[str, object]:
        """Serialisable view WITHOUT the raw embedding (templates never leave)."""
        return {
            "driver_id": self.driver_id,
            "name": self.name,
            "license_no": self.license_no,
            "synthetic": self.synthetic,
            "consented": self.consented,
            "photo_url": self.photo_url,
        }


def _pick(options: List[str], key: str) -> str:
    h = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big")
    return options[h % len(options)]


def _license_no(driver_id: str) -> str:
    """A deterministic, regex-plausible DL-style number for the synthetic driver."""
    h = hashlib.sha256(f"{SEED}|dl|{driver_id}".encode("utf-8")).hexdigest()
    state = ["MH", "GJ", "KA", "TN", "KL"][int(h[:2], 16) % 5]
    rto = int(h[2:4], 16) % 100
    year = 2008 + (int(h[4:6], 16) % 15)
    serial = int(h[6:13], 16) % 10_000_000
    return f"{state}{rto:02d}{year}{serial:07d}"


def generate_gallery(n: int = DEFAULT_GALLERY_SIZE) -> Dict[str, EnrolledDriver]:
    """Build the deterministic synthetic gallery of ``n`` enrolled drivers.

    Returns an ordered ``{driver_id: EnrolledDriver}`` map. Every record is
    SYNTHETIC + CONSENTED and carries its deterministic enrolment embedding.
    """
    gallery: Dict[str, EnrolledDriver] = {}
    for i in range(1, n + 1):
        driver_id = f"DRV-{i:04d}"
        name = f"{_pick(_FIRST, SEED + driver_id + 'f')} {_pick(_LAST, SEED + driver_id + 'l')}"
        gallery[driver_id] = EnrolledDriver(
            driver_id=driver_id,
            name=name,
            license_no=_license_no(driver_id),
            embedding=synth_embedding(driver_id),
        )
    return gallery


__all__ = [
    "SEED",
    "DEFAULT_GALLERY_SIZE",
    "EnrolledDriver",
    "generate_gallery",
]
