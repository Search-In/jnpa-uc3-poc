#!/usr/bin/env python3
"""
Standalone verification: compare source vs target row counts for every table in
the schema.  Exits non-zero if any table mismatches so it can gate a cut-over in
CI/scripts.

This is a thin wrapper around ``migrate.py verify`` and reads the same
environment variables (see config.py / README.md).

    python verify.py
"""

from __future__ import annotations

import sys

from config import load_config
from migrate import run_verify


if __name__ == "__main__":
    sys.exit(run_verify(load_config()))
