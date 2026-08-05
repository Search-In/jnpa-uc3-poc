"""JNPA Port-Data API sync — the live-API counterpart of the file-dump
import scripts.

One poller, built ONCE in this repo, feeds all three PoCs: records pulled
from the API are routed into the SAME upload services the manual dump-import
path uses, so their sha256 import ledgers make dump + API delivery of the
same bytes idempotent by construction (a re-delivered file returns
SKIPPED_DUPLICATE, never a second import).

Layering:
  store.py      — raw-bytes store ({dir}/{group}/{sha}__{filename}); keeps
                  every downloaded file for re-parse, PoC-2 serving and
                  submission evidence
  repository.py — DAO over the 0117 tables (api_sync_state / api_ingest_run /
                  api_record / api_report_snapshot / api_defect_log) +
                  ensure_api_ingest_schema() boot DDL + the cross-ledger
                  known-sha probe + the per-group advisory lock
  routing.py    — the 13-group consumption table: group -> upload service
  service.py    — JnpaSyncService (sync_all / sync_group / replay_unrouted)
                  and jnpa_sync_loop (the lifespan scheduler task)
"""
from __future__ import annotations

from .service import JnpaSyncService, jnpa_sync_loop
from .repository import SyncRepository, ensure_api_ingest_schema
from .store import ApiFileStore

__all__ = [
    "JnpaSyncService",
    "jnpa_sync_loop",
    "SyncRepository",
    "ensure_api_ingest_schema",
    "ApiFileStore",
]
