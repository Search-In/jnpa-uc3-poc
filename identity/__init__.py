"""identity — face-recognition driver verification (Appendix C #2, PDP augmentation).

PoC posture (DPDP Act): biometrics are SYNTHETIC and CONSENTED only — no real
driver biometrics are processed. This package demonstrates the verification
*pipeline* (embed -> match -> PROVISIONAL on miss) on a deterministic synthetic
gallery so the threshold/decision logic is provable without handling personal
data. See docs/ASSUMPTIONS.md "Identity / face-recognition (C2)".
"""

__version__ = "0.1.0"
