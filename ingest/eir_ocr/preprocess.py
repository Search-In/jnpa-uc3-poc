"""Image preprocessing variants for gate-slip OCR.

Ported from docs/ocr_gate_docs.py::ocr_photo — grayscale → EXIF transpose →
2× upscale → autocontrast (+ optional binarise / sharpen). Deterministic.
"""
from __future__ import annotations

import io
from typing import Any, List, Tuple


def load_pil():
    """Import PIL lazily so unit tests of extractors don't need Pillow."""
    from PIL import Image, ImageOps

    return Image, ImageOps


def variants_from_bytes(
    raw: bytes,
    *,
    binarize_threshold: int = 140,
) -> List[Tuple[str, Any]]:
    """Return named PIL images ready for Tesseract.

    Order matters for early-exit: autocontrast first (usually best recall),
    then sharpened (helps DP World truck lines), then binarised.
    """
    Image, ImageOps = load_pil()
    from PIL import ImageFilter

    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("L")
    big = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    auto = ImageOps.autocontrast(big)
    sharp = auto.filter(ImageFilter.SHARPEN)
    thresh = auto.point(lambda p: 255 if p > binarize_threshold else 0)
    return [
        ("autocontrast", auto),
        ("sharpen", sharp),
        ("binarize", thresh),
    ]


def variants_from_path(
    path: str,
    *,
    binarize_threshold: int = 140,
) -> List[Tuple[str, Any]]:
    with open(path, "rb") as f:
        return variants_from_bytes(f.read(), binarize_threshold=binarize_threshold)
