"""Pydantic views over the JNPA Simulated Port-Data API v2.0 responses.

Validation is deliberately tolerant (``extra="allow"``, every non-identity
field Optional): the interface spec is v2.0 of a PoC service whose own
materials carry 45 catalogued inconsistencies (see docs/JNPA_API_DEFECTS.md),
and JNPA's 31-Jul notice states the API "has known defects". Schema drift must
degrade to a logged DefectObservation, never fail a sync run.

Two envelope realities are modelled separately:
  RecordsEnvelope  indexed groups — the 9 documented fields, of which the
                   pagination trio (matched/hasMore/nextCursor) is reliable.
  ReportEnvelope   report groups (berthing-reports / daily-reports) — the
                   captured reality is a 5-field envelope (defect D9): no
                   order, no matched, no hasMore, no nextCursor. Items are
                   raw dicts because the item schema is entirely
                   undocumented (defect D10).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

# The authoritative 13 slugs, in the order the API's own unknown_group 404
# returns them. Catalogue drift from this set is a reportable observation.
EXPECTED_GROUP_SLUGS = (
    "nlp-marine",
    "bathymetry",
    "port-craft-pilot",
    "shipping-lines",
    "customs",
    "edi-messages",
    "berthing-reports",
    "gate-documents",
    "rail-fois",
    "rail-form11-icd",
    "transport",
    "daily-reports",
    "cfs-ecy",
)

REPORT_GROUPS = frozenset({"berthing-reports", "daily-reports"})
STATIC_GROUPS = frozenset({"bathymetry"})  # listed in the catalogue, served empty
INDEXED_GROUPS = tuple(g for g in EXPECTED_GROUP_SLUGS
                       if g not in REPORT_GROUPS and g not in STATIC_GROUPS)


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="allow")


class TokenClient(_Tolerant):
    id: Optional[str] = None
    organisation: Optional[str] = None


class TokenResponse(_Tolerant):
    accessToken: Optional[str] = None
    tokenType: Optional[str] = None          # "Bearer"
    expiresIn: Optional[int] = None          # 3600 — fixed one hour
    expiresAt: Optional[str] = None          # RFC3339 +05:30
    scopes: Optional[List[str]] = None       # groups:read, files:read
    client: Optional[TokenClient] = None


@dataclass
class TokenInfo:
    """The cached bearer + the monotonic clock needed for proactive refresh."""

    access_token: str
    expires_in: float                        # seconds (default 3600)
    acquired_monotonic: float                # time.monotonic() at issue
    expires_at: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    client_id: Optional[str] = None
    organisation: Optional[str] = None

    def expiring(self, margin_s: float) -> bool:
        """True when within ``margin_s`` of expiry (proactive-refresh gate)."""
        return time.monotonic() >= self.acquired_monotonic + self.expires_in - margin_s


class GroupCoverage(_Tolerant):
    # RFC3339 bounds of the published data window (indexed groups only).
    from_: Optional[str] = None
    to: Optional[str] = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __init__(self, **data: Any) -> None:  # `from` is a Python keyword
        if "from" in data:
            data["from_"] = data.pop("from")
        super().__init__(**data)


class GroupInfo(_Tolerant):
    group: str
    name: Optional[str] = None
    description: Optional[str] = None
    delivery: Optional[str] = None           # indexed | report | static
    records: Optional[int] = None
    coverage: Optional[GroupCoverage] = None
    messageTypes: Optional[List[str]] = None
    note: Optional[str] = None
    links: Optional[Dict[str, Any]] = None


class GroupsEnvelope(_Tolerant):
    groups: List[GroupInfo] = []
    asOf: Optional[str] = None


class FileMeta(_Tolerant):
    fileRef: str
    mediaType: Optional[str] = None
    sizeBytes: Optional[int] = None
    checksumSha256: Optional[str] = None     # == unquoted ETag of the file
    url: Optional[str] = None                # relative /v2/files/{ref} path


class IndexedRecord(_Tolerant):
    recordId: str
    group: Optional[str] = None
    messageType: Optional[str] = None
    messageName: Optional[str] = None
    publishedAt: Optional[str] = None        # RFC3339 +05:30 — watermark key
    containerCount: Optional[int] = None
    vesselCall: Optional[str] = None         # VCN-shaped, but see defect D36:
                                             # fill-forward artefact — validate
                                             # before using as a join key
    summary: Optional[str] = None
    file: Optional[FileMeta] = None


class RecordsEnvelope(_Tolerant):
    asOf: Optional[str] = None
    group: Optional[str] = None
    delivery: Optional[str] = None
    order: Optional[str] = None
    count: Optional[int] = None
    matched: Optional[int] = None
    hasMore: Optional[bool] = None
    nextCursor: Optional[str] = None         # opaque; pass back VERBATIM only
    items: List[IndexedRecord] = []


class ReportEnvelope(_Tolerant):
    asOf: Optional[str] = None
    group: Optional[str] = None
    delivery: Optional[str] = None
    count: Optional[int] = None
    items: List[Dict[str, Any]] = []         # item schema undocumented (D10)
    # Documented-but-absent on report groups (D9) — kept Optional so a fixed
    # server upgrades gracefully:
    order: Optional[str] = None
    matched: Optional[int] = None
    hasMore: Optional[bool] = None
    nextCursor: Optional[str] = None


class ApiErrorBody(_Tolerant):
    error: Optional[str] = None              # bad_parameter | bad_cursor | ...
    message: Optional[str] = None
    reason: Optional[str] = None             # only seen on token expiry
    availableGroups: Optional[List[str]] = None


@dataclass
class FileFetch:
    """Outcome of GET /v2/files/{fileRef}. ``status`` is 200 or 304."""

    file_ref: str
    status: int
    content: Optional[bytes] = None          # None on 304
    filename: Optional[str] = None           # from Content-Disposition
    media_type: Optional[str] = None
    etag: Optional[str] = None               # unquoted
    sha256: Optional[str] = None             # locally computed over content
    size_bytes: int = 0

    @property
    def not_modified(self) -> bool:
        return self.status == 304


@dataclass
class DefectObservation:
    """One runtime observation of API behaviour deviating from (or confirming
    a known defect in) the published interface documents. Drained by the sync
    layer into core.api_defect_log — observations are DELIVERABLES: JNPA's
    31-Jul notice requires observed defects to be reported."""

    code: str                                # e.g. D22_PLUS_ENCODING, RUNTIME_*
    endpoint: str
    detail: str
    severity: str = "INFO"
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not self.observed_at:
            self.observed_at = datetime.now(timezone.utc).isoformat()


@dataclass
class RequestStats:
    """Per-drain counters for the ingest-run audit row."""

    request_count: int = 0
    retry_count: int = 0
    http_429_count: int = 0
    bytes_downloaded: int = 0
    rate_limit_remaining_last: Optional[int] = None
    rate_limit_remaining_min: Optional[int] = None
    status_counts: Dict[int, int] = field(default_factory=dict)

    def record(self, status: int, remaining: Optional[int],
               body_bytes: int = 0) -> None:
        self.request_count += 1
        self.status_counts[status] = self.status_counts.get(status, 0) + 1
        self.bytes_downloaded += body_bytes
        if status == 429:
            self.http_429_count += 1
        if remaining is not None:
            self.rate_limit_remaining_last = remaining
            if (self.rate_limit_remaining_min is None
                    or remaining < self.rate_limit_remaining_min):
                self.rate_limit_remaining_min = remaining

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "http_429_count": self.http_429_count,
            "bytes_downloaded": self.bytes_downloaded,
            "rate_limit_remaining_last": self.rate_limit_remaining_last,
            "rate_limit_remaining_min": self.rate_limit_remaining_min,
            "status_counts": {str(k): v for k, v in self.status_counts.items()},
        }


__all__ = [
    "EXPECTED_GROUP_SLUGS",
    "REPORT_GROUPS",
    "STATIC_GROUPS",
    "INDEXED_GROUPS",
    "TokenResponse",
    "TokenClient",
    "TokenInfo",
    "GroupCoverage",
    "GroupInfo",
    "GroupsEnvelope",
    "FileMeta",
    "IndexedRecord",
    "RecordsEnvelope",
    "ReportEnvelope",
    "ApiErrorBody",
    "FileFetch",
    "DefectObservation",
    "RequestStats",
]
