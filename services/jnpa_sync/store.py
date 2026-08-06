"""Raw-bytes store for API-downloaded files.

Layout: ``{root}/{group}/{sha256}__{filename}`` — content-addressed first so
the same bytes under two names collapse to one file, original filename kept
because several corpus parsers derive terminal/layout/variant from it. The
store is the source `GET /api/jnpa/files/{sha}` serves (PoC-2's reference
ingest reads it over HTTP) and part of the submission evidence trail.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

_SAFE = re.compile(r"[^A-Za-z0-9._ ()\[\]-]")
_MAX_NAME = 140


def sanitize_filename(filename: Optional[str]) -> str:
    """A filesystem-safe rendition of an upstream filename. Never empty."""
    name = Path(filename or "").name  # strip any path components
    name = _SAFE.sub("_", name).strip(" .")
    return name[:_MAX_NAME] or "unnamed.bin"


class ApiFileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, group: str, sha256: str, filename: Optional[str]) -> Path:
        return self.root / group / f"{sha256}__{sanitize_filename(filename)}"

    def save(self, group: str, sha256: str, filename: Optional[str],
             content: bytes) -> str:
        """Persist bytes; returns the stored path (str, for the DB row).
        Content-addressed: an existing file with the same sha is left alone."""
        target = self.path_for(group, sha256, filename)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(content)
            tmp.replace(target)
        return str(target)

    def find_by_sha(self, sha256: str) -> Optional[Path]:
        """Locate a stored file by content hash, any group."""
        if not self.root.is_dir():
            return None
        for group_dir in self.root.iterdir():
            if not group_dir.is_dir():
                continue
            for candidate in group_dir.glob(f"{sha256}__*"):
                return candidate
        return None

    @staticmethod
    def original_filename(stored: Path) -> str:
        """The filename component of a stored path (after the sha prefix)."""
        _, _, name = stored.name.partition("__")
        return name or stored.name

    def open_bytes(self, sha256: str) -> Optional[Tuple[bytes, str]]:
        """(content, original_filename) for a stored sha, or None."""
        path = self.find_by_sha(sha256)
        if path is None or not path.is_file():
            return None
        return path.read_bytes(), self.original_filename(path)


__all__ = ["ApiFileStore", "sanitize_filename"]
