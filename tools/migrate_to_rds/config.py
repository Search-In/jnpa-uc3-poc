"""
Configuration for the local-TimescaleDB -> AWS-RDS-PostgreSQL migration utility.

Everything is driven by environment variables so the utility stays a standalone
tool and never has to import or touch application code.  A ``.env`` file placed
next to this module is loaded automatically if ``python-dotenv`` is installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

# Optional: load a local .env file so operators can keep secrets out of the shell
# history.  This is a no-op if python-dotenv is not installed.
try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:  # pragma: no cover
    pass


def _split(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_dsn(prefix: str, default_dsn: str) -> str:
    """Return a libpq DSN.

    ``<PREFIX>_DSN`` wins if set.  Otherwise a DSN is assembled from the discrete
    ``<PREFIX>_HOST/PORT/DB/USER/PASSWORD`` variables, falling back to ``default``.
    """
    dsn = os.getenv(f"{prefix}_DSN")
    if dsn:
        return dsn
    host = os.getenv(f"{prefix}_HOST")
    if not host:
        return default_dsn
    parts = [
        f"host={host}",
        f"port={os.getenv(f'{prefix}_PORT', '5432')}",
        f"dbname={os.getenv(f'{prefix}_DB', 'postgres')}",
        f"user={os.getenv(f'{prefix}_USER', 'postgres')}",
    ]
    password = os.getenv(f"{prefix}_PASSWORD")
    if password:
        parts.append(f"password={password}")
    sslmode = os.getenv(f"{prefix}_SSLMODE")
    if sslmode:
        parts.append(f"sslmode={sslmode}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# TimescaleDB internal / continuous-aggregate schemas.  We only ever read from
# the target schema (``jnpa``) so chunk tables never appear, but these are kept
# here for documentation and defence in depth.
# ---------------------------------------------------------------------------
TIMESCALE_INTERNAL_SCHEMAS = (
    "_timescaledb_catalog",
    "_timescaledb_internal",
    "_timescaledb_config",
    "_timescaledb_cache",
    "timescaledb_information",
    "timescaledb_experimental",
)


@dataclass
class Config:
    # Connections ---------------------------------------------------------
    # Source defaults to the docker-published port 5433 -> container 5432.
    source_dsn: str = field(
        default_factory=lambda: _build_dsn(
            "SOURCE",
            "host=localhost port=5433 dbname=postgres user=postgres "
            f"password={os.getenv('POSTGRES_PASSWORD', 'jnpa_pw')}",
        )
    )
    # Target has no sensible default (must be provided) but we keep the same
    # assembly logic for convenience.  RDS forces TLS, so default sslmode=require.
    target_dsn: str = field(
        default_factory=lambda: _build_dsn(
            "TARGET",
            "host="
            + os.getenv("TARGET_HOST", "")
            + " port=5432 dbname=jnpa3 user=postgres sslmode=require",
        )
    )

    schema: str = field(default_factory=lambda: os.getenv("MIGRATE_SCHEMA", "jnpa"))

    # Batching ------------------------------------------------------------
    batch_size: int = field(
        default_factory=lambda: int(os.getenv("BATCH_SIZE", "5000"))
    )

    # Tables the operator wants to exclude entirely (comma separated, bare names).
    skip_tables: List[str] = field(
        default_factory=lambda: _split(os.getenv("SKIP_TABLES", ""))
    )

    # Explicit override of which tables are hypertables.  Normally auto-detected
    # from the TimescaleDB catalog; this is an escape hatch if the catalog is
    # unavailable (comma separated bare names).
    hypertables_override: List[str] = field(
        default_factory=lambda: _split(os.getenv("HYPERTABLES", ""))
    )

    # Only migrate these tables if set (comma separated bare names) - handy for
    # re-running a single table.
    only_tables: List[str] = field(
        default_factory=lambda: _split(os.getenv("ONLY_TABLES", ""))
    )

    # Resume / state ------------------------------------------------------
    state_file: str = field(
        default_factory=lambda: os.getenv(
            "STATE_FILE",
            os.path.join(os.path.dirname(__file__), "migration_state.json"),
        )
    )

    # Retry ---------------------------------------------------------------
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("MAX_RETRIES", "5"))
    )
    retry_base_delay: float = field(
        default_factory=lambda: float(os.getenv("RETRY_BASE_DELAY", "1.5"))
    )

    # Statement timeout applied on the *target* per session (ms). 0 = disabled.
    statement_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("STATEMENT_TIMEOUT_MS", "0"))
    )

    # Connection timeout (seconds) so an unreachable RDS host fails fast.
    connect_timeout: int = field(
        default_factory=lambda: int(os.getenv("CONNECT_TIMEOUT", "10"))
    )

    # Disable FK/trigger enforcement on the target for the load session
    # (SET session_replication_role = replica) so table order can't break FKs.
    # We ALSO topologically sort by the FK graph as a fallback. 1=on (default).
    disable_fk_during_load: bool = field(
        default_factory=lambda: os.getenv("DISABLE_FK_DURING_LOAD", "1") != "0"
    )

    # Automatically create missing indexes on the target after the data load.
    # 1=on (default) so no manual SQL is ever required.
    create_indexes_after_load: bool = field(
        default_factory=lambda: os.getenv("CREATE_INDEXES", "1") != "0"
    )

    # Abort before writing if a source column's type differs from the target's.
    # 1=on (default). Set 0 to downgrade type differences to warnings.
    strict_types: bool = field(
        default_factory=lambda: os.getenv("STRICT_TYPES", "1") != "0"
    )

    def target_configured(self) -> bool:
        """True if the target connection has a non-empty host."""
        for tok in self.target_dsn.split():
            if tok.startswith("host=") and tok[len("host="):].strip():
                return True
        # a URI-style DSN (postgres://...) counts as configured
        return "://" in self.target_dsn

    def redacted_target(self) -> str:
        """target_dsn with any password token stripped, for logging."""
        return " ".join(
            tok for tok in self.target_dsn.split() if not tok.startswith("password=")
        )

    def redacted_source(self) -> str:
        return " ".join(
            tok for tok in self.source_dsn.split() if not tok.startswith("password=")
        )


def load_config() -> Config:
    return Config()
