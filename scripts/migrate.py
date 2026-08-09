#!/usr/bin/env python3
"""Apply the infra/postgres/v3 migrations in order, once each, idempotently.

Before this, the v3 SQL files were applied BY HAND — nothing in the Makefile
referenced them, there was no ledger of what had run, and a fresh machine had no
way to know which of 0100..0116 its database was at. That is the "deployment is
not reproducible" half of the audit finding.

    make migrate            # apply everything pending
    make migrate-status     # show applied / pending, apply nothing
    make migrate-dry-run    # print the plan, apply nothing

Design:

  * **Ledger.** ``core.schema_migrations`` records (version, name, checksum,
    applied_at, duration_ms). A version already in the ledger is skipped, so
    re-running is a no-op — the "idempotent execution" requirement.

  * **Checksum drift is fatal.** If a file changed after it was applied, the run
    ABORTS rather than silently ignoring the edit. Migrations are immutable once
    applied; fix forward with a new file. Override with --allow-drift only when
    you know the edit is cosmetic (a comment).

  * **Ordered by numeric prefix.** 0100, 0101, ... 0116. Files without a numeric
    prefix are ignored.

  * **Transactional by default, autocommit when required.** Each file runs inside
    BEGIN/COMMIT so a failure leaves nothing half-applied. Files that CANNOT run
    in a transaction (``CREATE INDEX CONCURRENTLY`` — PostgreSQL forbids it in a
    transaction block) are detected and run in autocommit instead. 0116 is the
    first such file.

  * **Opt-in for the destructive and one-off files.** 0900 (DROP SCHEMA jnpa) and
    0100 (the legacy copy shell script) are NEVER run automatically; 0900 needs
    --include-drop and a typed confirmation.

  * **Baseline for the existing database.** The live RDS already has 0101..0115
    applied BY HAND, with no ledger to prove it. Running them again would be
    mostly harmless (they are CREATE ... IF NOT EXISTS) but 0201..0203 are
    BACKFILLS — re-running those would duplicate rows. So an existing database is
    adopted once, without executing anything::

        make migrate-baseline VERSION=0203   # record 0101..0203 as already applied

    Verify with --status first, and only baseline a version you have confirmed is
    really present in the schema.

  * **A second directory, on request only.** ``--dir`` + ``--only`` point the same
    ledger/checksum/transaction machinery at ``infra/postgres/migrations`` (the
    hand-applied 0001..0053 set) for the few files a deployment genuinely needs,
    without re-running the backfills that share that directory::

        python scripts/migrate.py --dir infra/postgres/migrations --only 0036,0037

    Defaults are unchanged: a bare ``make migrate`` still sees only v3.

  * **Superuser, briefly.** Migrations are the one thing that legitimately needs
    DDL rights (docs/RDS_SECURITY.md §3 gives the app role none). Pass the
    superuser DSN via --dsn for the run; the deployed services keep using
    jnpa_app.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "infra" / "postgres" / "v3"

#: The OTHER migration directory. Its files (0001..0053) predate this runner and
#: have always been applied by hand — most of their objects are now also created
#: at gateway boot by the ``ensure_*_schema`` functions (JNPA_RUNTIME_DDL). It is
#: NOT the default: applying all of it unattended would re-run backfills such as
#: 0022. Reach it deliberately with ``--dir`` + ``--only``, which is what the UC-1
#: cold start does for the two files it genuinely needs:
#:
#:     python scripts/migrate.py --dir infra/postgres/migrations --only 0036,0037
LEGACY_MIGRATIONS_DIR = REPO_ROOT / "infra" / "postgres" / "migrations"

#: Files that must never be applied by an unattended `make migrate`.
#: 0100 is a shell script (the legacy cross-database copy); 0900 DROPs the legacy
#: schema and is gated behind --include-drop plus a typed confirmation.
DESTRUCTIVE = {"0900"}
NON_SQL = {"0100"}

#: Statements PostgreSQL refuses to run inside a transaction block.
_AUTOCOMMIT_MARKERS = ("CREATE INDEX CONCURRENTLY", "DROP INDEX CONCURRENTLY",
                       "REINDEX CONCURRENTLY", "ALTER TYPE", "VACUUM")

LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS core;
CREATE TABLE IF NOT EXISTS core.schema_migrations (
    version     text        PRIMARY KEY,
    name        text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer,
    applied_by  text        NOT NULL DEFAULT current_user
);
COMMENT ON TABLE core.schema_migrations IS
  'Applied infra/postgres/v3 migrations. Written by scripts/migrate.py (make migrate).';
"""


class Migration(NamedTuple):
    version: str
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def needs_autocommit(self) -> bool:
        upper = self.sql.upper()
        return any(m in upper for m in _AUTOCOMMIT_MARKERS)

    @property
    def is_destructive(self) -> bool:
        return self.version in DESTRUCTIVE


# --------------------------------------------------------------------------- io
def _checksum(text: str) -> str:
    """Hash the SQL only — comment-only edits still change it, deliberately.

    A migration is a historical fact; if the bytes differ from what ran, we want
    to know. --allow-drift exists for the cosmetic case.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def discover(directory: Path | None = None) -> list[Migration]:
    """Every numbered .sql migration in ``directory``, ordered by version."""
    directory = directory or MIGRATIONS_DIR
    out: list[Migration] = []
    if not directory.is_dir():
        raise SystemExit(f"migrations directory not found: {directory}")
    for path in sorted(directory.glob("*.sql")):
        m = re.match(r"^(\d{4})[_-](.+)\.sql$", path.name)
        if not m:
            continue  # hotfix_*.sql and friends are applied by hand, by design
        version, name = m.group(1), m.group(2)
        if version in NON_SQL:
            continue
        sql = path.read_text(encoding="utf-8")
        out.append(Migration(version, name, path, sql, _checksum(sql)))
    return out


def select(migrations: list[Migration], only: str | None) -> list[Migration]:
    """Narrow the discovered set to an explicit comma-separated version list.

    Two jobs, both about applying a directory this runner does not own:

      * **Pick exactly what was asked for.** ``--only 0036,0037`` against
        ``infra/postgres/migrations`` applies those two files and nothing else,
        so the backfills in that directory stay untouched.
      * **Refuse an ambiguous version.** ``infra/postgres/migrations`` contains
        BOTH ``0038_marine_vessel_call.sql`` and ``0038_perf_pdf_upload.sql``.
        The ledger is keyed on version, so recording one would mark the other as
        applied. Selecting a duplicated version is an error, not a coin toss.

    An unknown version is an error too: silently applying nothing is how a
    "migrated" database ends up missing a table.
    """
    if not only:
        return migrations
    wanted = [v.strip() for v in only.split(",") if v.strip()]
    picked: list[Migration] = []
    for version in wanted:
        matches = [m for m in migrations if m.version == version]
        if not matches:
            raise SystemExit(
                f"--only {version}: no migration with that version in the selected "
                f"directory. Available: {', '.join(m.version for m in migrations)}"
            )
        if len(matches) > 1:
            names = ", ".join(f"{m.version}_{m.name}.sql" for m in matches)
            raise SystemExit(
                f"--only {version}: AMBIGUOUS — {len(matches)} files share this version "
                f"({names}). The ledger is keyed on version, so applying one would "
                f"record the other as applied too. Rename one of them first."
            )
        picked.extend(matches)
    return sorted(picked, key=lambda m: m.version)


def resolve_dsn(explicit: str | None) -> str:
    """libpq DSN for the migration run.

    Order: --dsn, then $MIGRATE_DSN, then $RFID_POSTGRES_DSN (the repo's
    canonical libpq DSN), then $POSTGRES_DSN with the SQLAlchemy/asyncpg driver
    prefix stripped.
    """
    if explicit:
        return explicit
    for var in ("MIGRATE_DSN", "RFID_POSTGRES_DSN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    val = os.environ.get("POSTGRES_DSN", "").strip()
    if val:
        # postgresql+asyncpg://... -> postgresql://..., ssl= -> sslmode=
        val = re.sub(r"^postgresql\+\w+://", "postgresql://", val)
        val = val.replace("?ssl=require", "?sslmode=require")
        return val
    raise SystemExit(
        "no DSN. Pass --dsn, or set MIGRATE_DSN / RFID_POSTGRES_DSN / POSTGRES_DSN "
        "(see .env.local.example and docs/RDS_SECURITY.md)."
    )


def _redact(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:****@", dsn)


# ----------------------------------------------------------------------- runner
def with_keepalives(dsn: str) -> str:
    """Add libpq TCP keepalives unless the DSN already sets them.

    A ``CREATE INDEX CONCURRENTLY`` on a 9 GB table sends NOTHING over the socket
    for 10-20 minutes while the server works. Any NAT/firewall between the
    operator and RDS silently drops the idle connection, and the migration dies
    with "SSL SYSCALL error: EOF detected" — after having half-built an index,
    which then has to be found and dropped by hand. Observed on the first real
    run of 0116.

    30s idle / 10s interval / 6 probes keeps the connection demonstrably alive
    well inside a typical 350s NAT timeout.
    """
    if "keepalives" in dsn:
        return dsn
    params = "keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=6"
    return dsn + ("&" if "?" in dsn else "?") + params


def connect(dsn: str):
    try:
        import psycopg  # psycopg 3
    except ImportError:
        raise SystemExit(
            "psycopg is required: pip install 'psycopg[binary]' "
            "(or run inside the project venv: make venv)"
        )
    return psycopg.connect(with_keepalives(dsn), autocommit=True)


def ensure_ledger(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(LEDGER_DDL)


def applied_map(conn) -> dict[str, str]:
    """version -> checksum for everything already applied."""
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM core.schema_migrations")
        return {r[0]: r[1] for r in cur.fetchall()}


def check_drift(migrations: Iterable[Migration], applied: dict[str, str],
                allow_drift: bool) -> None:
    drifted = [
        m for m in migrations
        if m.version in applied and applied[m.version] != m.checksum
    ]
    if not drifted:
        return
    lines = [f"  {m.version}_{m.name}.sql  ledger={applied[m.version]}  file={m.checksum}"
             for m in drifted]
    msg = ("migration files changed AFTER they were applied:\n" + "\n".join(lines))
    if allow_drift:
        print(f"WARNING: {msg}\n  (--allow-drift: continuing)", file=sys.stderr)
        return
    raise SystemExit(
        f"ABORT: {msg}\n\n"
        "Migrations are immutable once applied. Fix forward with a NEW numbered\n"
        "file. If the change is cosmetic (a comment), re-run with --allow-drift."
    )


def split_statements(sql: str) -> list[str]:
    """Split a migration into individual statements for the autocommit path.

    Needed because psycopg sends a multi-statement string as ONE simple-query
    message, which PostgreSQL executes as an implicit transaction block — and
    `CREATE INDEX CONCURRENTLY` is rejected there ("cannot run inside a
    transaction block") even when the connection is in autocommit. Sending each
    statement separately is what actually makes it concurrent-safe.

    Handles the constructs these migrations actually use: line comments,
    block comments, single-quoted literals and dollar-quoted bodies (DO $$ ... $$).
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_line_comment = in_block_comment = in_single = False
    dollar_tag: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                # '' is an escaped quote, not a terminator.
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        # -- default state --
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m_tag = re.match(r"\$[A-Za-z_]\w*\$|\$\$", sql[i:])
            if m_tag:
                dollar_tag = m_tag.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if _is_executable(stmt):
                out.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if _is_executable(tail):
        out.append(tail)
    return out


def _is_executable(stmt: str) -> bool:
    """False for whitespace/comment-only fragments (the trailing chunk after the
    last semicolon in a well-commented file is usually one of these)."""
    stripped = re.sub(r"--[^\n]*", "", stmt)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    return bool(stripped.strip())


def drop_invalid_indexes(conn) -> list[str]:
    """Drop INVALID indexes left behind by an interrupted CONCURRENTLY build.

    This is not housekeeping — it is required for idempotence. A killed
    ``CREATE INDEX CONCURRENTLY`` leaves an index that EXISTS but is marked
    invalid; the planner ignores it, yet ``CREATE INDEX CONCURRENTLY IF NOT
    EXISTS`` sees the NAME and skips. Without this sweep a re-run silently
    "succeeds" while leaving the index permanently dead — exactly what happened
    on the first run of 0116 (idx_truck_telemetry_device_ts).

    Only ever touches invalid indexes, so a healthy database is untouched.
    """
    dropped: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT i.indisvalid AND n.nspname NOT LIKE 'pg_%'"
        )
        stale = cur.fetchall()
    for schema, name in stale:
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{schema}"."{name}"')
        dropped.append(f"{schema}.{name}")
    return dropped


def apply_one(conn, m: Migration) -> int:
    """Apply one migration + record it. Returns elapsed milliseconds."""
    t0 = time.perf_counter()
    if m.needs_autocommit:
        # Clear any half-built index from a previous interrupted run first, or
        # IF NOT EXISTS will skip the rebuild and leave it dead forever.
        for name in drop_invalid_indexes(conn):
            print(f"    dropped invalid index from an earlier run: {name}")
        # One statement per round trip — see split_statements().
        for stmt in split_statements(m.sql):
            with conn.cursor() as cur:
                cur.execute(stmt)
    else:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(m.sql)
    ms = int((time.perf_counter() - t0) * 1000)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.schema_migrations (version, name, checksum, duration_ms) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum, "
            "  applied_at = now(), duration_ms = EXCLUDED.duration_ms",
            (m.version, m.name, m.checksum, ms),
        )
    return ms


def baseline(conn, migrations: list[Migration], applied: dict[str, str],
             up_to: str) -> int:
    """Record 0101..``up_to`` as applied WITHOUT executing them.

    For adopting a database that was migrated by hand — the live RDS is at 0115
    with an empty ledger. Executing those files again would be mostly harmless
    (CREATE ... IF NOT EXISTS) but 0201..0203 are BACKFILLS and would duplicate
    rows, so the safe adoption path is to record, not re-run.

    Destructive migrations are never baselined: claiming 0900 ran when it did not
    would hide a real pending step.
    """
    targets = [m for m in migrations
               if m.version <= up_to and m.version not in applied and not m.is_destructive]
    if not targets:
        print(f"==> nothing to baseline at or below {up_to} (ledger already covers it)")
        return 0

    print(f"\n==> baseline will RECORD (not run) {len(targets)} migration(s) up to {up_to}:")
    for m in targets:
        print(f"      {m.version}_{m.name}.sql")
    print("\n!! Only do this if you have CONFIRMED these are already applied to this")
    print("   database (check --status and the actual schema first). Recording a")
    print("   migration that did not run means it will never run.")
    if input('   Type "baseline" to proceed: ').strip() != "baseline":
        print("   aborted.")
        return 1

    with conn.cursor() as cur:
        for m in targets:
            cur.execute(
                "INSERT INTO core.schema_migrations "
                "(version, name, checksum, duration_ms, applied_by) "
                "VALUES (%s, %s, %s, NULL, %s) ON CONFLICT (version) DO NOTHING",
                (m.version, m.name, m.checksum, f"{os.environ.get('USER', 'unknown')} (baseline)"),
            )
    print(f"\n==> recorded {len(targets)} migration(s) as applied")
    print("    `make migrate` will now apply only what comes after them.")
    return 0


# ------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", help="libpq DSN (needs DDL rights; see docs/RDS_SECURITY.md §3)")
    ap.add_argument("--status", action="store_true", help="show applied/pending and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, apply nothing")
    ap.add_argument("--target", help="stop after this version (e.g. 0115)")
    ap.add_argument("--include-drop", action="store_true",
                    help="also run 0900 (DROP SCHEMA jnpa) — asks for confirmation")
    ap.add_argument("--allow-drift", action="store_true",
                    help="continue when an applied file's checksum changed")
    ap.add_argument("--baseline", metavar="VERSION",
                    help="record every migration up to VERSION as applied WITHOUT "
                         "running it (adopts a database migrated by hand)")
    ap.add_argument("--dir", dest="directory", default=None,
                    help=f"migration directory (default {MIGRATIONS_DIR.relative_to(REPO_ROOT)}). "
                         f"Use with --only to reach {LEGACY_MIGRATIONS_DIR.relative_to(REPO_ROOT)}, "
                         "whose backfills must not run unattended.")
    ap.add_argument("--only", default=None, metavar="VERSIONS",
                    help="apply ONLY these comma-separated versions, e.g. 0036,0037. "
                         "Everything else in the directory is ignored.")
    args = ap.parse_args(argv)

    directory = Path(args.directory) if args.directory else MIGRATIONS_DIR
    if not directory.is_absolute():
        directory = (REPO_ROOT / directory).resolve()

    migrations = select(discover(directory), args.only)
    if not migrations:
        print("no migrations found", file=sys.stderr)
        return 1

    dsn = resolve_dsn(args.dsn)
    print(f"==> database: {_redact(dsn)}")
    print(f"==> migrations: {directory}"
          + (f"  (only {args.only})" if args.only else ""))

    conn = connect(dsn)
    try:
        ensure_ledger(conn)
        applied = applied_map(conn)

        if args.baseline:
            return baseline(conn, migrations, applied, args.baseline)

        check_drift(migrations, applied, args.allow_drift)

        pending = [m for m in migrations if m.version not in applied]
        if args.target:
            pending = [m for m in pending if m.version <= args.target]
        if not args.include_drop:
            skipped_destructive = [m for m in pending if m.is_destructive]
            pending = [m for m in pending if not m.is_destructive]
        else:
            skipped_destructive = []

        if args.status:
            print(f"\n{'ver':6} {'status':9} {'applied_at':22} name")
            with conn.cursor() as cur:
                cur.execute("SELECT version, applied_at, duration_ms "
                            "FROM core.schema_migrations")
                meta = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            for m in migrations:
                if m.version in applied:
                    at, ms = meta.get(m.version, (None, None))
                    stamp = at.strftime("%Y-%m-%d %H:%M:%S") if at else "-"
                    print(f"{m.version:6} {'APPLIED':9} {stamp:22} {m.name} ({ms}ms)")
                else:
                    tag = "DESTRUCTIVE" if m.is_destructive else "PENDING"
                    print(f"{m.version:6} {tag:9} {'-':22} {m.name}")
            return 0

        if not pending:
            print("==> database is up to date; nothing to apply")
            if skipped_destructive:
                print(f"    ({len(skipped_destructive)} destructive migration(s) held back; "
                      "use --include-drop)")
            return 0

        print(f"\n==> {len(pending)} migration(s) pending:")
        for m in pending:
            mode = "autocommit" if m.needs_autocommit else "transactional"
            print(f"      {m.version}_{m.name}.sql  [{mode}]")

        if args.dry_run:
            print("\n(--dry-run: nothing applied)")
            return 0

        if args.include_drop and any(m.is_destructive for m in pending):
            print("\n!! 0900 DROPS SCHEMA jnpa CASCADE. This is not reversible.")
            if input('   Type "drop legacy schema" to proceed: ').strip() != "drop legacy schema":
                print("   aborted.")
                return 1

        print()
        for m in pending:
            note = "  (CONCURRENTLY — this can take 10-20 min on truck_telemetry)" \
                if m.needs_autocommit else ""
            print(f"--> applying {m.version}_{m.name}{note}", flush=True)
            try:
                ms = apply_one(conn, m)
            except Exception as exc:  # noqa: BLE001 — report which file, then stop
                print(f"\nFAILED at {m.version}_{m.name}.sql:\n  {exc}", file=sys.stderr)
                print("\nNothing after this migration was applied. Fix the file and "
                      "re-run `make migrate`.", file=sys.stderr)
                return 1
            print(f"    ok ({ms} ms)")

        print(f"\n==> applied {len(pending)} migration(s)")
        if skipped_destructive:
            print(f"    ({len(skipped_destructive)} destructive migration(s) held back)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
