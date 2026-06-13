"""Programmatic image degradation for the evaluator.

Reproduces the three "port operating conditions" the bid commits to handling:

    * dust + haze   — a low-contrast scattering layer + slight blur
    * fog           — a denser white veil + stronger blur
    * night/low-light — gamma-darkening + sensor noise

All transforms are deterministic given a seed so the eval slices are
reproducible (no wall-clock RNG). Each returns a new BGR ``uint8`` image.
"""
from __future__ import annotations

import cv2
import numpy as np

Image = np.ndarray


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def gaussian_blur(img: Image, ksize: int = 5, sigma: float = 1.5) -> Image:
    k = ksize if ksize % 2 == 1 else ksize + 1
    return cv2.GaussianBlur(img, (k, k), sigma)


def haze_layer(img: Image, intensity: float = 0.45, seed: int = 0) -> Image:
    """Atmospheric-scattering haze: blend toward a bright grey veil with a
    smooth random transmission map (thicker at the top, like real haze)."""
    h, w = img.shape[:2]
    rng = _rng(seed)
    # Smooth transmission map: low-freq noise + vertical gradient.
    base = rng.uniform(0.0, 1.0, size=(max(2, h // 16), max(2, w // 16))).astype(np.float32)
    trans = cv2.resize(base, (w, h), interpolation=cv2.INTER_CUBIC)
    grad = np.linspace(1.0, 0.4, h, dtype=np.float32)[:, None]
    veil = np.clip(intensity * (0.5 * trans + 0.5 * grad), 0.0, 1.0)[..., None]
    airlight = np.full_like(img, 220, dtype=np.float32)
    out = img.astype(np.float32) * (1.0 - veil) + airlight * veil
    return np.clip(out, 0, 255).astype(np.uint8)


def dust(img: Image, intensity: float = 0.35, seed: int = 0) -> Image:
    """Dust: a warm low-contrast veil + sparse particulate speckle + mild blur."""
    rng = _rng(seed)
    h, w = img.shape[:2]
    # Warm (B<R) dusty airlight.
    airlight = np.zeros_like(img, dtype=np.float32)
    airlight[..., 0] = 150  # B
    airlight[..., 1] = 175  # G
    airlight[..., 2] = 200  # R
    veil = float(np.clip(intensity, 0, 1))
    out = img.astype(np.float32) * (1.0 - veil) + airlight * veil
    # Sparse bright specks (suspended particles catching light).
    speck = rng.random((h, w)) > 0.992
    out[speck] = np.clip(out[speck] + 60, 0, 255)
    out = np.clip(out, 0, 255).astype(np.uint8)
    return gaussian_blur(out, ksize=3, sigma=0.8)


def low_light(img: Image, gamma: float = 2.6, noise_sigma: float = 10.0, seed: int = 0) -> Image:
    """Night / low-light: gamma-darken, then add read-noise."""
    rng = _rng(seed)
    norm = img.astype(np.float32) / 255.0
    darkened = np.power(norm, gamma) * 255.0
    noise = rng.normal(0.0, noise_sigma, size=img.shape)
    out = darkened + noise
    return np.clip(out, 0, 255).astype(np.uint8)


def dust_haze(img: Image, seed: int = 0) -> Image:
    """Combined dust+haze degradation used by eval slice (b)."""
    out = haze_layer(img, intensity=0.40, seed=seed)
    out = dust(out, intensity=0.30, seed=seed + 1)
    return gaussian_blur(out, ksize=5, sigma=1.2)


def night_low_light(img: Image, seed: int = 0) -> Image:
    """Combined night low-light degradation used by eval slice (c)."""
    out = low_light(img, gamma=2.4, noise_sigma=12.0, seed=seed)
    return gaussian_blur(out, ksize=3, sigma=0.7)


# Registry the evaluator iterates over.
DEGRADATIONS = {
    "clean": lambda img, seed=0: img.copy(),
    "dust_haze": dust_haze,
    "night": night_low_light,
}


__all__ = [
    "gaussian_blur",
    "haze_layer",
    "dust",
    "low_light",
    "dust_haze",
    "night_low_light",
    "DEGRADATIONS",
]
