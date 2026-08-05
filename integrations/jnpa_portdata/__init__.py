"""JNPA Simulated Port-Data API integration — the live counterpart of the
sample-data-pack corpus (JNPA API Reference v2.0, 31-Jul-2026).

Layering mirrors :mod:`integrations.ulip` exactly:
  client.py     — the ONLY layer that speaks HTTP to dt.jnpa.in
                  (client-key -> 1-h bearer, single-flight refresh, bounded
                  retries, client-side rate budget, checksum verification,
                  typed errors, credentials never logged)
  schemas.py    — tolerant pydantic views over the v2 envelopes (the spec has
                  45 catalogued defects; drift degrades to a logged
                  DefectObservation, never a crash)
  exceptions.py — typed failure vocabulary for the sync layer's degrade path

The client key (JNPA_PORTDATA_CLIENT_KEY) is BACKEND-ONLY: read from the
process environment, sent only to the token endpoint, never exposed to the
frontend (no VITE_ variable, no browser call) and never committed — the key
is a credential per NOTICE_API_ACCESS.md §6.1.

Consumed by :mod:`services.jnpa_sync` — the group poller that routes API
records into the same upload services the file-dump import path uses (the
sha256 import ledger makes dump + API delivery of the same bytes idempotent).
"""
from __future__ import annotations

from .client import DEFAULT_API_URL, JnpaPortDataClient
from .exceptions import (
    JnpaAuthError,
    JnpaChecksumMismatch,
    JnpaError,
    JnpaHTTPError,
    JnpaInvalidResponse,
    JnpaNotConfigured,
    JnpaRateLimited,
    JnpaTimeout,
    JnpaUnavailable,
)
from .schemas import (
    EXPECTED_GROUP_SLUGS,
    INDEXED_GROUPS,
    REPORT_GROUPS,
    STATIC_GROUPS,
    ApiErrorBody,
    DefectObservation,
    FileFetch,
    FileMeta,
    GroupInfo,
    GroupsEnvelope,
    IndexedRecord,
    RecordsEnvelope,
    ReportEnvelope,
    RequestStats,
    TokenInfo,
    TokenResponse,
)

__all__ = [
    "JnpaPortDataClient",
    "DEFAULT_API_URL",
    "JnpaError",
    "JnpaNotConfigured",
    "JnpaTimeout",
    "JnpaUnavailable",
    "JnpaAuthError",
    "JnpaRateLimited",
    "JnpaHTTPError",
    "JnpaInvalidResponse",
    "JnpaChecksumMismatch",
    "EXPECTED_GROUP_SLUGS",
    "INDEXED_GROUPS",
    "REPORT_GROUPS",
    "STATIC_GROUPS",
    "ApiErrorBody",
    "DefectObservation",
    "FileFetch",
    "FileMeta",
    "GroupInfo",
    "GroupsEnvelope",
    "IndexedRecord",
    "RecordsEnvelope",
    "ReportEnvelope",
    "RequestStats",
    "TokenInfo",
    "TokenResponse",
]
