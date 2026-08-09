"""One upload size limit for every ingest module (UC2-036).

The cap was declared as a 10 MB literal in five routers independently. Two
problems followed. The corpus's largest file — ``11-Transport Data/PDP
Details.xlsx`` at 24 MB — is refused with a bare 413, which is a demo failure on
real supplied data rather than on anything a bidder did. And a limit duplicated
five times is a limit that drifts: raising it in one place fixes one module and
silently leaves the others.

The default is sized to the corpus, not to a round number: it must admit the
largest file JNPA actually shared, with headroom. Override with ``MAX_UPLOAD_MB``
where a deployment needs a different ceiling — in front of this sits whatever the
reverse proxy allows, so raising it here alone is not sufficient if Caddy/nginx
also caps the body.
"""
from __future__ import annotations

import os


def _mb_from_env(default_mb: int = 32) -> int:
    raw = (os.environ.get("MAX_UPLOAD_MB") or "").strip()
    if not raw:
        return default_mb
    try:
        mb = int(float(raw))
    except ValueError:
        return default_mb
    # A zero or negative ceiling would refuse every upload, which is never what
    # someone setting this variable meant.
    return mb if mb > 0 else default_mb


#: Largest accepted request body for a file upload, in bytes.
MAX_UPLOAD_MB = _mb_from_env()
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

__all__ = ["MAX_UPLOAD_BYTES", "MAX_UPLOAD_MB"]
