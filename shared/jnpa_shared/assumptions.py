"""Loader for the cross-UC assumptions single-source (``shared/assumptions.json``).

Both digital twins (UC-2 and UC-3) read the SAME JSON so port throughput, gate
capacity, vehicle counts, road capacity and vessel assumptions have one
authoritative home instead of being scattered across service code. Prose
rationale lives in ``docs/ASSUMPTIONS.md``; the numeric KPI targets remain
authoritative in ``jnpa_shared.kpi.KPI_TARGETS`` and are mirrored (read-only)
into the JSON for convenience.

Resolution order for the file:
  1. ``JNPA_ASSUMPTIONS_PATH`` env var (explicit override, e.g. a deploy mount)
  2. ``shared/assumptions.json`` relative to this package (editable install)
  3. an upward walk from CWD looking for ``shared/assumptions.json`` (repo root)
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("JNPA_ASSUMPTIONS_PATH")
    if env:
        paths.append(Path(env))
    # shared/jnpa_shared/assumptions.py -> shared/assumptions.json
    paths.append(Path(__file__).resolve().parent.parent / "assumptions.json")
    # Upward walk from CWD (covers running from a service subdir).
    here = Path.cwd()
    for parent in [here, *here.parents]:
        paths.append(parent / "shared" / "assumptions.json")
    return paths


def assumptions_path() -> Optional[Path]:
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


@lru_cache(maxsize=1)
def load_assumptions() -> Dict[str, Any]:
    """Return the parsed assumptions document. Raises FileNotFoundError if the
    single-source JSON cannot be located (a deployment misconfiguration)."""
    path = assumptions_path()
    if path is None:
        raise FileNotFoundError(
            "shared/assumptions.json not found; set JNPA_ASSUMPTIONS_PATH or run "
            "from the repo root."
        )
    return json.loads(path.read_text())


def get(section: str, key: Optional[str] = None, default: Any = None) -> Any:
    """Convenience accessor: ``get('gates', 'throughput_target_vph')``."""
    doc = load_assumptions()
    sect = doc.get(section, {})
    if key is None:
        return sect
    return sect.get(key, default) if isinstance(sect, dict) else default


__all__ = ["assumptions_path", "load_assumptions", "get"]
