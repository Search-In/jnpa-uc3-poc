"""Email ingestion (UC3 Email Processing page).

    config.py       — mailbox settings from the environment ONLY (no secret in code)
    imap_client.py  — read-only IMAP reader (stdlib), strict "subject starts with" filter
    classifier.py   — attachment -> existing UC3 master table, via the marine registry
    repository.py   — DAO over the 0139 ledger (core.email_message / _attachment / _error)
    service.py      — orchestration: preview + import through the EXISTING upload services

Nothing here parses a file or writes a master table itself; the shipped marine and
gate-document upload services do that, unchanged. gateway/mailer.py remains the
separate OUTBOUND (SMTP) seam.
"""
from .service import EmailProcessingService

__all__ = ["EmailProcessingService"]
