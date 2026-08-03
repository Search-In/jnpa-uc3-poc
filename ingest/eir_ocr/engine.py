"""Multi-variant Tesseract OCR with SHA-256 LRU cache and early-exit.

Efficiency vs a naive single image_to_string:
  1. Preprocess variants (autocontrast + binarize) × PSM 6 then 4
  2. Deduplicate lines across passes
  3. After each pass, run the field extractor; stop when high-value fields hit
  4. Cache full results by sha256(image bytes)
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import preprocess
from .config import EirOcrConfig
from .extract import FieldValue, extract_eir_bundle, high_value_hits
from .normalize import is_valid_container


@dataclass
class OcrResult:
    raw_text: str
    fields: Dict[str, FieldValue]
    confidence: float
    source: str  # OCR | CACHE
    timings_ms: Dict[str, float] = field(default_factory=dict)
    variants_run: int = 0
    early_exit: bool = False
    sha256: str = ""
    tesseract_version: str = ""
    engine_ready: bool = True
    error: Optional[str] = None
    extras: Dict[str, FieldValue] = field(default_factory=dict)


class _LRU:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(0, capacity)
        self._data: OrderedDict[str, OcrResult] = OrderedDict()

    def get(self, key: str) -> Optional[OcrResult]:
        if self.capacity <= 0 or key not in self._data:
            return None
        self._data.move_to_end(key)
        hit = self._data[key]
        # Return a shallow copy tagged as CACHE.
        return OcrResult(
            raw_text=hit.raw_text,
            fields=dict(hit.fields),
            confidence=hit.confidence,
            source="CACHE",
            timings_ms={"cache_ms": 0.0},
            variants_run=hit.variants_run,
            early_exit=hit.early_exit,
            sha256=hit.sha256,
            tesseract_version=hit.tesseract_version,
            engine_ready=hit.engine_ready,
            extras=dict(hit.extras),
        )

    def put(self, key: str, value: OcrResult) -> None:
        if self.capacity <= 0:
            return
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def tesseract_status() -> Tuple[bool, str]:
    """Return (ready, version_or_error)."""
    try:
        import pytesseract
    except ImportError as e:
        return False, f"pytesseract_missing:{e}"
    try:
        ver = str(pytesseract.get_tesseract_version())
        return True, ver
    except Exception as e:  # noqa: BLE001
        return False, f"tesseract_binary_missing:{e}"


class OcrEngine:
    """Stateful OCR engine (cache + config)."""

    def __init__(self, cfg: Optional[EirOcrConfig] = None) -> None:
        self.cfg = cfg or EirOcrConfig.from_env()
        self._cache = _LRU(self.cfg.cache_size)
        ready, ver = tesseract_status()
        self.ready = ready
        self.version = ver

    def infer_bytes(
        self,
        raw: bytes,
        *,
        doc_type: str = "EIR",
        use_cache: bool = True,
    ) -> OcrResult:
        digest = sha256_hex(raw)
        if use_cache:
            cached = self._cache.get(digest)
            if cached is not None:
                return cached

        t0 = time.perf_counter()
        if not self.ready:
            # Soft degrade: empty OCR, still run extractor on "" so API shape is stable.
            empty = OcrResult(
                raw_text="",
                fields={},
                confidence=0.0,
                source="OCR",
                timings_ms={"total_ms": (time.perf_counter() - t0) * 1000},
                sha256=digest,
                tesseract_version=self.version,
                engine_ready=False,
                error=self.version,
            )
            return empty

        import pytesseract

        t_prep = time.perf_counter()
        variants = preprocess.variants_from_bytes(
            raw, binarize_threshold=self.cfg.binarize_threshold
        )
        prep_ms = (time.perf_counter() - t_prep) * 1000

        seen: set[str] = set()
        lines: List[str] = []
        variants_run = 0
        early = False
        fields: Dict[str, FieldValue] = {}
        extras: Dict[str, FieldValue] = {}
        t_ocr = time.perf_counter()

        # Order: autocontrast×psm6, autocontrast×psm4, binarize×psm6, binarize×psm4
        schedule: List[Tuple[str, Any, int]] = []
        for name, img in variants:
            for psm in (6, 4):
                schedule.append((name, img, psm))

        for name, img, psm in schedule:
            try:
                text = pytesseract.image_to_string(img, config=f"--psm {psm}") or ""
            except Exception:  # noqa: BLE001
                text = ""
            variants_run += 1
            for line in text.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
            joined = "\n".join(lines)
            bundle = extract_eir_bundle(joined, doc_type=doc_type)
            fields = bundle.fields
            extras = bundle.extras
            # Run all preprocess×PSM passes before considering early-exit so
            # late-appearing ISO/seal lines on Gateway slips are not skipped.
            if (
                variants_run >= len(schedule)
                and fields.get("ContainerNo")
                and is_valid_container(fields["ContainerNo"].value)
                and high_value_hits(
                    fields,
                    names=self.cfg.early_exit_fields,
                    min_hits=self.cfg.early_exit_min_hits,
                )
            ):
                early = True
                break

        ocr_ms = (time.perf_counter() - t_ocr) * 1000
        joined = "\n".join(lines)
        if not fields:
            bundle = extract_eir_bundle(joined, doc_type=doc_type)
            fields = bundle.fields
            extras = bundle.extras

        conf = _overall_confidence(fields, joined)
        result = OcrResult(
            raw_text=joined,
            fields=fields,
            confidence=conf,
            source="OCR",
            timings_ms={
                "prep_ms": round(prep_ms, 2),
                "ocr_ms": round(ocr_ms, 2),
                "total_ms": round((time.perf_counter() - t0) * 1000, 2),
            },
            variants_run=variants_run,
            early_exit=early,
            sha256=digest,
            tesseract_version=self.version,
            engine_ready=True,
            extras=extras,
        )
        if use_cache:
            self._cache.put(digest, result)
        return result

    def infer_path(self, path: str, *, doc_type: str = "EIR", use_cache: bool = True) -> OcrResult:
        with open(path, "rb") as f:
            return self.infer_bytes(f.read(), doc_type=doc_type, use_cache=use_cache)


def _overall_confidence(fields: Dict[str, FieldValue], raw_text: str) -> float:
    if not raw_text.strip():
        return 0.0
    if not fields:
        return 0.4  # text present but no structured fields
    vals = [f.conf for f in fields.values() if f.value]
    if not vals:
        return 0.4
    return round(sum(vals) / len(vals), 3)


# Module-level default engine factory (lazy).
_ENGINE: Optional[OcrEngine] = None


def get_engine(cfg: Optional[EirOcrConfig] = None) -> OcrEngine:
    global _ENGINE
    if _ENGINE is None or (cfg is not None and cfg is not _ENGINE.cfg):
        _ENGINE = OcrEngine(cfg)
    return _ENGINE
