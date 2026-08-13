"""SecureVision service layer — everything the gateway decides about vendor data.

  cameras.py    explicit SecureVision <-> JNPA camera mapping (never guesses)
  normalize.py  vendor payloads -> the typed shapes our screens read
  analyses.py   in-process cache of uploads this gateway performed
  repository.py durable Video Analytics history (core.video_analysis)
  history.py    the history service: durable store in front of that cache
  tickets.py    short-lived stream tickets for the MJPEG <img> tag

The HTTP client lives in :mod:`integrations.securevision`; the routes live in
gateway/routers/securevision.py. Nothing here talks HTTP, and nothing here is
imported by an existing JNPA module — the integration is strictly additive.
"""
from __future__ import annotations

from . import analyses, cameras, normalize, tickets
from .history import VideoAnalysisHistory
from .repository import VideoAnalysisRepository

__all__ = ["analyses", "cameras", "normalize", "tickets",
           "VideoAnalysisHistory", "VideoAnalysisRepository"]
