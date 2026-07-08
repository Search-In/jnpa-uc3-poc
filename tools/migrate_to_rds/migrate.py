#!/usr/bin/env python3
"""
One-time migration utility: local Docker PostgreSQL (TimescaleDB) -> AWS RDS
PostgreSQL.

Why this exists
---------------
``pg_dump``/``pg_restore`` cannot faithfully move TimescaleDB *hypertables* into a
plain PostgreSQL instance: the data physically lives in per-chunk child tables
under ``_timescaledb_internal`` and the restore expects the TimescaleDB extension
to recreate that partitioning.  RDS PostgreSQL 18 has no TimescaleDB extension,
so the chunks never get restored and the parent table comes back empty.

This tool sidesteps the problem completely:

* It reads from the *parent* hypertable with an ordinary ``SELECT`` (TimescaleDB
  transparently unions the chunks), so we get every row.
* It writes into an ordinary PostgreSQL table on RDS with plain ``INSERT``.
* Normal tables are UPSERTed on their primary key so a re-run is idempotent.
* Hypertables have no primary key, so they resume from a ``ts`` high-water mark
  read back from the target.

It never calls ``pg_restore`` and never needs TimescaleDB on the target.

Modes
-----
    migrate         Copy data (default). Combine with --dry-run / --resume.
    verify          Compare row counts between source and target.
    emit-indexes    Write a SQL file recreating all source indexes on the target.

Everything is configured through environment variables - see config.py / README.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras
from psycopg2 import sql

from config import Config, load_config

# tqdm is optional; fall back to a tiny no-op shim so the tool still runs.
try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    class tqdm:  # type: ignore
        def __init__(self, total=None, desc=None, unit=None, leave=True, **_):
            self.total = total
            self.n = 0
            self.desc = desc

        def update(self, n=1):
            self.n += n

        def set_postfix_str(self, *_a, **_k):
            pass

        def close(self):
            if self.total:
                print(f"    {self.desc}: {self.n}/{self.total}")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()


# Transient error types worth retrying (connection blips, RDS failover, etc.).
TRANSIENT_ERRORS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str, *, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {level:5s} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------
class TableInfo:
    def __init__(
        self,
        name: str,
        columns: List[str],
        pk: List[str],
        is_hypertable: bool,
        time_column: Optional[str],
        json_columns: List[str],
    ):
        self.name = name
        self.columns = columns
        self.pk = pk
        self.is_hypertable = is_hypertable
        self.time_column = time_column  # for hypertables, the partitioning column
        self.json_columns = json_columns

    @property
    def has_pk(self) -> bool:
        return bool(self.pk)


def fetch_base_tables(cur, schema: str) -> List[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    return [r[0] for r in cur.fetchall()]


def fetch_hypertables(cur, schema: str) -> Dict[str, str]:
    """Return {table_name: time_column} for hypertables in *schema*.

    Reads the TimescaleDB catalog directly.  Returns {} if TimescaleDB is not
    installed on the source (in which case the caller may fall back to the
    HYPERTABLES override).
    """
    # Is TimescaleDB present at all?
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    if cur.fetchone() is None:
        return {}

    # The dimension table holds the partitioning ("time") column of each
    # hypertable; the first dimension is the primary time dimension.
    cur.execute(
        """
        SELECT h.table_name, d.column_name
        FROM _timescaledb_catalog.hypertable h
        JOIN _timescaledb_catalog.dimension d ON d.hypertable_id = h.id
        WHERE h.schema_name = %s
        ORDER BY h.table_name, d.id
        """,
        (schema,),
    )
    result: Dict[str, str] = {}
    for table_name, column_name in cur.fetchall():
        # keep the first (primary) dimension only
        result.setdefault(table_name, column_name)
    return result


def fetch_columns(cur, schema: str, table: str) -> Tuple[List[str], List[str]]:
    """Return (ordered column names, json/jsonb column names)."""
    cur.execute(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    cols: List[str] = []
    json_cols: List[str] = []
    for name, data_type, udt in cur.fetchall():
        cols.append(name)
        if data_type in ("json", "jsonb") or udt in ("json", "jsonb"):
            json_cols.append(name)
    return cols, json_cols


def fetch_primary_key(cur, schema: str, table: str) -> List[str]:
    """Return the ordered primary-key column list ([] if none)."""
    cur.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)
        WHERE i.indisprimary
          AND n.nspname = %s
          AND c.relname = %s
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (schema, table),
    )
    return [r[0] for r in cur.fetchall()]


def fetch_column_types(cur, schema: str, table: str) -> Dict[str, str]:
    """Return {column_name: udt_name} for a table (empty dict if it doesn't
    exist). udt_name is the canonical underlying type (e.g. int4, text, jsonb)."""
    cur.execute(
        """
        SELECT column_name, udt_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return {name: udt for name, udt in cur.fetchall()}


def fetch_fk_edges(cur, schema: str) -> List[Tuple[str, str]]:
    """Return (child_table, parent_table) FK edges within *schema*.

    Read from pg_constraint so we get the true referenced table regardless of
    column naming. Self-references are returned too and filtered by the caller.
    """
    cur.execute(
        """
        SELECT child.relname AS child_table, parent.relname AS parent_table
        FROM pg_constraint con
        JOIN pg_class child   ON child.oid = con.conrelid
        JOIN pg_class parent  ON parent.oid = con.confrelid
        JOIN pg_namespace n    ON n.oid = con.connamespace
        WHERE con.contype = 'f' AND n.nspname = %s
        """,
        (schema,),
    )
    return [(c, p) for c, p in cur.fetchall()]


def toposort_tables(names: List[str], edges: List[Tuple[str, str]]) -> List[str]:
    """Order *names* so every FK parent precedes its child (Kahn's algorithm).

    Ties are broken alphabetically for reproducibility. Edges referencing tables
    outside *names* and self-references are ignored. If a cycle exists the
    remaining tables are appended alphabetically (FK checks are also disabled
    during load, so a cycle cannot corrupt the migration).
    """
    nodeset = set(names)
    parents: Dict[str, set] = defaultdict(set)   # child -> {parents}
    children: Dict[str, set] = defaultdict(set)  # parent -> {children}
    for child, parent in edges:
        if child == parent or child not in nodeset or parent not in nodeset:
            continue
        if parent not in parents[child]:
            parents[child].add(parent)
            children[parent].add(child)

    indeg = {n: len(parents[n]) for n in names}
    queue = sorted(n for n in names if indeg[n] == 0)
    order: List[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for c in sorted(children[n]):
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
        queue.sort()

    if len(order) < len(names):  # cycle: append the rest deterministically
        order.extend(sorted(n for n in names if n not in order))
    return order


def build_table_info(cur, cfg: Config, table: str, hypertables: Dict[str, str]) -> TableInfo:
    cols, json_cols = fetch_columns(cur, cfg.schema, table)
    pk = fetch_primary_key(cur, cfg.schema, table)
    is_hyper = table in hypertables or table in cfg.hypertables_override
    time_col = hypertables.get(table)
    if is_hyper and not time_col:
        # override path: guess a 'ts' column if present, else first timestamp col
        time_col = "ts" if "ts" in cols else None
    return TableInfo(table, cols, pk, is_hyper, time_col, json_cols)


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------
def build_select(cfg: Config, info: TableInfo, where: Optional[sql.Composed] = None) -> sql.Composed:
    """SELECT with json/jsonb columns cast to text for clean round-tripping."""
    select_items = []
    for col in info.columns:
        if col in info.json_columns:
            select_items.append(
                sql.SQL("{}::text").format(sql.Identifier(col))
            )
        else:
            select_items.append(sql.Identifier(col))
    query = sql.SQL("SELECT {cols} FROM {schema}.{table}").format(
        cols=sql.SQL(", ").join(select_items),
        schema=sql.Identifier(cfg.schema),
        table=sql.Identifier(info.name),
    )
    if where is not None:
        query = query + sql.SQL(" ") + where
    # deterministic ordering helps hypertable resume and makes runs reproducible
    if info.is_hypertable and info.time_column:
        query = query + sql.SQL(" ORDER BY {}").format(sql.Identifier(info.time_column))
    elif info.has_pk:
        query = query + sql.SQL(" ORDER BY {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in info.pk)
        )
    return query


def build_insert(cfg: Config, info: TableInfo) -> sql.Composed:
    """INSERT ... VALUES %s, with UPSERT on PK for normal tables."""
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in info.columns)
    base = sql.SQL("INSERT INTO {schema}.{table} ({cols}) VALUES %s").format(
        schema=sql.Identifier(cfg.schema),
        table=sql.Identifier(info.name),
        cols=col_idents,
    )
    if info.has_pk:
        non_pk = [c for c in info.columns if c not in info.pk]
        if non_pk:
            assignments = sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                for c in non_pk
            )
            conflict = sql.SQL(" ON CONFLICT ({pk}) DO UPDATE SET {sets}").format(
                pk=sql.SQL(", ").join(sql.Identifier(c) for c in info.pk),
                sets=assignments,
            )
        else:
            # PK covers every column -> nothing to update, just skip dup rows
            conflict = sql.SQL(" ON CONFLICT ({pk}) DO NOTHING").format(
                pk=sql.SQL(", ").join(sql.Identifier(c) for c in info.pk),
            )
        return base + conflict
    # No PK (hypertable): plain append. Resume is handled by the ts water-mark.
    return base


# ---------------------------------------------------------------------------
# Connection management + retry
# ---------------------------------------------------------------------------
@contextmanager
def connect(dsn: str, *, name: str, connect_timeout: int = 10):
    conn = psycopg2.connect(dsn, connect_timeout=connect_timeout)
    try:
        yield conn
    finally:
        conn.close()


def apply_session_settings(conn, cfg: Config) -> bool:
    """Apply target session settings. Returns True if FK enforcement was
    successfully disabled for the load session."""
    fk_disabled = False
    with conn.cursor() as cur:
        if cfg.statement_timeout_ms > 0:
            cur.execute("SET statement_timeout = %s", (cfg.statement_timeout_ms,))
        if cfg.disable_fk_during_load:
            # Bypass FK checks AND user triggers for THIS session only. Reverts
            # on disconnect; we also restore it explicitly after the load. The
            # RDS master user holds rds_superuser, which may set this.
            try:
                cur.execute("SET session_replication_role = replica")
                fk_disabled = True
            except psycopg2.Error:
                conn.rollback()
                log("could not SET session_replication_role=replica "
                    "(insufficient privilege); relying on topological FK order",
                    level="WARN")
    conn.commit()
    return fk_disabled


def restore_session_settings(conn) -> None:
    """Restore FK/trigger enforcement after the load (belt-and-suspenders;
    the session also resets on disconnect)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET session_replication_role = origin")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()


def with_retry(cfg: Config, fn, *, desc: str):
    """Run *fn* with exponential-backoff retry on transient errors."""
    attempt = 0
    while True:
        try:
            return fn()
        except TRANSIENT_ERRORS as exc:
            attempt += 1
            if attempt > cfg.max_retries:
                log(f"{desc}: giving up after {cfg.max_retries} retries: {exc}",
                    level="ERROR")
                raise
            delay = cfg.retry_base_delay * (2 ** (attempt - 1))
            log(f"{desc}: transient error ({exc.__class__.__name__}), "
                f"retry {attempt}/{cfg.max_retries} in {delay:.1f}s", level="WARN")
            time.sleep(delay)


# ---------------------------------------------------------------------------
# State (resume) handling
# ---------------------------------------------------------------------------
class State:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    self.data = json.load(fh)
            except Exception:
                self.data = {}

    def get(self, table: str) -> dict:
        return self.data.get(table, {})

    def mark(self, table: str, **kwargs) -> None:
        entry = self.data.setdefault(table, {})
        entry.update(kwargs)
        self._flush()

    def is_complete(self, table: str) -> bool:
        return self.data.get(table, {}).get("status") == "complete"

    def _flush(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2, default=str)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Core copy routines
# ---------------------------------------------------------------------------
def count_rows(conn, cfg: Config, info: TableInfo,
               where: Optional[sql.Composed] = None, params: Sequence = ()) -> int:
    q = sql.SQL("SELECT count(*) FROM {schema}.{table}").format(
        schema=sql.Identifier(cfg.schema), table=sql.Identifier(info.name)
    )
    if where is not None:
        q = q + sql.SQL(" ") + where
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchone()[0]


def hypertable_watermark(target, cfg: Config, info: TableInfo):
    """Return the max(time_column) already present on the target, or None."""
    if not info.time_column:
        return None
    with target.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT max({col}) FROM {schema}.{table}").format(
                col=sql.Identifier(info.time_column),
                schema=sql.Identifier(cfg.schema),
                table=sql.Identifier(info.name),
            )
        )
        return cur.fetchone()[0]


def verify_table(source, target, cfg: Config, info: TableInfo) -> Tuple[int, object]:
    """Return (source_count, target_count) doing a full COUNT(*) on both sides.

    target_count is an int, or an 'ERROR: ...' string if the target could not be
    counted (e.g. the table is missing on the target).
    """
    src_n = count_rows(source, cfg, info)
    try:
        tgt_n = count_rows(target, cfg, info)
    except psycopg2.Error as exc:
        target.rollback()
        tgt_n = f"ERROR: {(exc.pgerror or str(exc)).strip()}"
    return src_n, tgt_n


def reset_sequences(target, cfg: Config, info: TableInfo) -> List[str]:
    """Advance every sequence owned by *info*'s columns to max(column) on the
    target, so post-migration inserts don't collide with migrated IDs.

    Copying serial/identity values verbatim preserves the IDs but leaves the
    backing sequence at its old position; this realigns it. Handles bigserial,
    serial and GENERATED ... AS IDENTITY columns. Returns the columns reset.
    """
    reset: List[str] = []
    with target.cursor() as cur:
        cur.execute(
            """
            SELECT column_name,
                   pg_get_serial_sequence(format('%%I.%%I', %s, %s), column_name)
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (cfg.schema, info.name, cfg.schema, info.name),
        )
        owned = [(col, seq) for col, seq in cur.fetchall() if seq]
        for col, seq in owned:
            # setval to max(col); if the table is empty, park the sequence at 1
            # with is_called=false so the first insert still yields 1.
            cur.execute(
                sql.SQL(
                    "SELECT setval(%s, "
                    "COALESCE((SELECT max({col}) FROM {sch}.{tbl}), 1), "
                    "(SELECT max({col}) FROM {sch}.{tbl}) IS NOT NULL)"
                ).format(
                    col=sql.Identifier(col),
                    sch=sql.Identifier(cfg.schema),
                    tbl=sql.Identifier(info.name),
                ),
                (seq,),
            )
            reset.append(col)
    target.commit()
    return reset


def copy_table(
    source,
    target,
    cfg: Config,
    info: TableInfo,
    state: State,
    *,
    dry_run: bool,
    resume: bool,
) -> Tuple[int, int]:
    """Copy one table. Returns (rows_read, rows_written)."""
    where: Optional[sql.Composed] = None
    params: Sequence = ()

    # --- Hypertable water-mark reconciliation --------------------------------
    # Hypertables have no primary key, so a plain re-INSERT would duplicate rows.
    # We ALWAYS reconcile against the target's high-water mark (not just under
    # --resume): rows are copied in ts-ascending, batch-committed order, so the
    # target always holds a contiguous prefix [min .. max(ts)]. Re-copying from
    # max(ts) therefore makes the append idempotent whether this is a re-run or a
    # resume after interruption. On an empty target the water-mark is NULL and
    # everything is copied.
    if info.is_hypertable and not dry_run and info.time_column:
        watermark = hypertable_watermark(target, cfg, info)
        if watermark is not None:
            # Rows exactly at the boundary ts may have been only partially
            # committed, so delete them and re-copy everything >= watermark.
            log(f"  reconcile: {info.name} {info.time_column} >= {watermark}")
            with target.cursor() as cur:
                cur.execute(
                    sql.SQL("DELETE FROM {schema}.{table} WHERE {col} = %s").format(
                        schema=sql.Identifier(cfg.schema),
                        table=sql.Identifier(info.name),
                        col=sql.Identifier(info.time_column),
                    ),
                    (watermark,),
                )
            target.commit()
            where = sql.SQL("WHERE {col} >= %s").format(
                col=sql.Identifier(info.time_column)
            )
            params = (watermark,)

    total = count_rows(source, cfg, info, where, params)

    kind = "hypertable" if info.is_hypertable else "table"
    pk_desc = ",".join(info.pk) if info.has_pk else "(no PK -> append)"
    log(f"  {info.name} [{kind}] rows={total} pk={pk_desc}")

    if dry_run:
        return total, 0

    select_q = build_select(cfg, info, where)
    insert_q = build_insert(cfg, info)

    written = 0
    # Server-side (named) cursor streams the source without loading it all into
    # memory - essential for large hypertables.
    cursor_name = f"mig_{info.name}"
    with source.cursor(name=cursor_name) as scur:
        scur.itersize = cfg.batch_size
        scur.execute(select_q, params)

        with tqdm(total=total, desc=info.name, unit="row", leave=False) as bar:
            while True:
                rows = scur.fetchmany(cfg.batch_size)
                if not rows:
                    break

                def _write(rows=rows):
                    with target.cursor() as tcur:
                        psycopg2.extras.execute_values(
                            tcur, insert_q.as_string(target), rows,
                            page_size=cfg.batch_size,
                        )
                    target.commit()

                try:
                    with_retry(cfg, _write, desc=f"write {info.name}")
                except TRANSIENT_ERRORS:
                    target.rollback()
                    raise

                written += len(rows)
                bar.update(len(rows))
                bar.set_postfix_str(f"{written}/{total}")
                state.mark(info.name, status="in_progress", rows_written=written,
                           total=total)

    state.mark(info.name, status="complete", rows_written=written, total=total,
               finished_at=datetime.now(timezone.utc).isoformat())
    log(f"  {info.name}: wrote {written} rows", level="INFO")
    return total, written


# ---------------------------------------------------------------------------
# Table planning
# ---------------------------------------------------------------------------
def plan_tables(source, cfg: Config) -> Tuple[List[TableInfo], List[str]]:
    """Return (tables_to_migrate, explicitly_skipped_table_names).

    Every base table in the schema is accounted for: it is either migrated or
    listed in the skipped set - nothing is dropped silently.
    """
    with source.cursor() as cur:
        all_tables = fetch_base_tables(cur, cfg.schema)
        hypertables = fetch_hypertables(cur, cfg.schema)
        fk_edges = fetch_fk_edges(cur, cfg.schema)

    if not hypertables and cfg.hypertables_override:
        log("TimescaleDB catalog empty; using HYPERTABLES override", level="WARN")
        hypertables = {t: "ts" for t in cfg.hypertables_override}

    skip = set(cfg.skip_tables)
    only = set(cfg.only_tables)

    infos: List[TableInfo] = []
    skipped: List[str] = []
    with source.cursor() as cur:
        for table in all_tables:
            if table in skip or (only and table not in only):
                skipped.append(table)
                continue
            infos.append(build_table_info(cur, cfg, table, hypertables))

    # Order so every FK parent precedes its child (topological sort of the FK
    # graph). Hypertables are pushed last (they are large and unreferenced), but
    # ties within each group still respect the topological order. This is what
    # prevents e.g. `cameras` loading before `gates`.
    topo = toposort_tables([i.name for i in infos], fk_edges)
    rank = {name: idx for idx, name in enumerate(topo)}
    infos.sort(key=lambda i: (i.is_hypertable, rank[i.name]))
    return infos, skipped


# ---------------------------------------------------------------------------
# Schema preflight
# ---------------------------------------------------------------------------
def preflight_schema(source, target, cfg: Config,
                     infos: List[TableInfo]) -> List[str]:
    """Compare source vs target tables/columns/types BEFORE any write.

    Returns a list of human-readable problems; empty means the target is
    compatible. Detects: missing target table, missing target column, and
    (when strict_types) column type mismatches.
    """
    problems: List[str] = []
    with source.cursor() as scur, target.cursor() as tcur:
        for info in infos:
            src_types = fetch_column_types(scur, cfg.schema, info.name)
            tgt_types = fetch_column_types(tcur, cfg.schema, info.name)

            if not tgt_types:
                problems.append(f"{info.name}: table is MISSING on target")
                continue

            for col in info.columns:
                if col not in tgt_types:
                    problems.append(
                        f"{info.name}.{col}: column MISSING on target")
                    continue
                if cfg.strict_types and src_types.get(col) != tgt_types.get(col):
                    problems.append(
                        f"{info.name}.{col}: type mismatch "
                        f"(source={src_types.get(col)} target={tgt_types.get(col)})")
    return problems


# ---------------------------------------------------------------------------
# Index DDL (collect once, used for both file emit and auto-apply)
# ---------------------------------------------------------------------------
def collect_index_ddl(source, cfg: Config) -> List[Tuple[str, str, str]]:
    """Return [(table, index_name, CREATE INDEX IF NOT EXISTS ...)] for every
    non-primary-key index on the source schema, excluding TimescaleDB chunk
    indexes. This includes the hypertable time indexes TimescaleDB auto-creates
    (which a plain-Postgres target would otherwise be missing)."""
    stmts: List[Tuple[str, str, str]] = []
    with source.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname AS tablename, ic.relname AS indexname,
                   pg_get_indexdef(i.indexrelid) AS def
            FROM pg_index i
            JOIN pg_class c   ON c.oid = i.indrelid
            JOIN pg_class ic  ON ic.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND NOT i.indisprimary            -- PKs come with the schema
              AND c.relkind = 'r'               -- skip TimescaleDB chunk indexes
            ORDER BY c.relname, ic.relname
            """,
            (cfg.schema,),
        )
        for tablename, indexname, ddl in cur.fetchall():
            ddl = ddl.replace("CREATE UNIQUE INDEX ",
                              "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
            ddl = ddl.replace("CREATE INDEX ",
                              "CREATE INDEX IF NOT EXISTS ", 1)
            stmts.append((tablename, indexname, ddl))
    return stmts


def create_indexes(source, target, cfg: Config) -> Tuple[int, int]:
    """Create every missing index on the target. Returns (created_ok, failed).
    Each statement is IF NOT EXISTS, so this is safe to re-run."""
    stmts = collect_index_ddl(source, cfg)
    ok = fail = 0
    log(f"creating {len(stmts)} index(es) on target (post-load)")
    for tablename, indexname, ddl in stmts:
        try:
            with target.cursor() as cur:
                cur.execute(ddl)
            target.commit()
            ok += 1
        except psycopg2.Error as exc:
            target.rollback()
            fail += 1
            log(f"  index {indexname} on {tablename} FAILED: "
                f"{(exc.pgerror or str(exc)).strip()}", level="ERROR")
    log(f"indexes: {ok} created/verified, {fail} failed")
    return ok, fail


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def run_migrate(cfg: Config, *, dry_run: bool, resume: bool) -> int:
    log(f"source : {cfg.redacted_source()}")
    log(f"target : {cfg.redacted_target()}")
    log(f"schema : {cfg.schema}   batch_size={cfg.batch_size}   "
        f"mode={'DRY-RUN' if dry_run else 'MIGRATE'}   resume={resume}")

    state = State(cfg.state_file)

    with connect(cfg.source_dsn, name="source",
                 connect_timeout=cfg.connect_timeout) as source, \
            connect(cfg.target_dsn, name="target",
                    connect_timeout=cfg.connect_timeout) as target:

        infos, skipped = plan_tables(source, cfg)
        log(f"planned {len(infos)} tables "
            f"({sum(i.is_hypertable for i in infos)} hypertables), "
            f"{len(skipped)} explicitly skipped")
        log("load order: " + ", ".join(i.name for i in infos))
        if skipped:
            log(f"explicitly skipped (SKIP_TABLES/ONLY_TABLES): "
                f"{', '.join(skipped)}", level="WARN")

        # ------------------------------------------------------------------
        # SCHEMA PREFLIGHT (fix #2): compare tables/columns/types BEFORE any
        # write. Abort cleanly if the target is incompatible.
        # ------------------------------------------------------------------
        problems = preflight_schema(source, target, cfg, infos)
        if problems:
            log(f"SCHEMA PREFLIGHT FAILED: {len(problems)} problem(s) - "
                f"no data was written:", level="ERROR")
            for p in problems:
                log(f"  - {p}", level="ERROR")
            return 1
        log("schema preflight OK: all tables/columns/types compatible")

        # ------------------------------------------------------------------
        # DRY-RUN: read + report only, no writes.
        # ------------------------------------------------------------------
        if dry_run:
            report = []
            for info in infos:
                read, _ = copy_table(source, None, cfg, info, state,
                                     dry_run=True, resume=False)
                report.append((info.name, read, "-", "DRY-RUN", info.is_hypertable))
            for name in skipped:
                report.append((name, "-", "-", "SKIPPED", False))
            print_report(report, title="DRY-RUN PLAN", verdict=None)
            return 0

        # ------------------------------------------------------------------
        # MIGRATE: copy each table, then verify source vs target immediately.
        # Stop at the first mismatch/error - nothing is skipped silently.
        # ------------------------------------------------------------------
        fk_disabled = apply_session_settings(target, cfg)
        log(f"FK enforcement during load: "
            f"{'disabled (session_replication_role=replica)' if fk_disabled else 'ENABLED (topological order only)'}")

        report = []
        interrupted = {"flag": False}

        def _handle_sigint(signum, frame):
            log("interrupt received - finishing current batch then stopping",
                level="WARN")
            interrupted["flag"] = True

        signal.signal(signal.SIGINT, _handle_sigint)

        try:
            for info in infos:
                already = resume and state.is_complete(info.name)
                try:
                    if already:
                        log(f"  {info.name}: marked complete (resume) - verifying only")
                    else:
                        copy_table(source, target, cfg, info, state,
                                   dry_run=False, resume=resume)

                    # --- per-table verification ---
                    src_n, tgt_n = verify_table(source, target, cfg, info)
                    ok = isinstance(tgt_n, int) and tgt_n == src_n

                    # A resumed table that fails verification is re-copied once.
                    if already and not ok:
                        log(f"  {info.name}: resume state stale (src={src_n} "
                            f"tgt={tgt_n}) - re-migrating", level="WARN")
                        copy_table(source, target, cfg, info, state,
                                   dry_run=False, resume=resume)
                        src_n, tgt_n = verify_table(source, target, cfg, info)
                        ok = isinstance(tgt_n, int) and tgt_n == src_n

                    # Sequences reset on every path (fix #F5) - idempotent.
                    if ok:
                        seqs = reset_sequences(target, cfg, info)
                        if seqs:
                            log(f"  {info.name}: reset sequence(s) for "
                                f"{', '.join(seqs)}")
                except psycopg2.Error as exc:
                    # Non-transient DB error (fix #F4): report it, don't crash.
                    target.rollback()
                    msg = (exc.pgerror or str(exc)).strip().splitlines()[0]
                    report.append((info.name, "?", f"ERROR", "ERROR",
                                   info.is_hypertable))
                    for name in skipped:
                        report.append((name, "-", "-", "SKIPPED", False))
                    print_report(report, title="MIGRATION REPORT", verdict="FAILED")
                    log(f"STOP: {info.name} failed with a database error: {msg}",
                        level="ERROR")
                    return 1

                status = "OK" if ok else "MISMATCH"
                report.append((info.name, src_n, tgt_n, status, info.is_hypertable))
                log(f"  {info.name}: source={src_n} target={tgt_n} -> {status}",
                    level="INFO" if ok else "ERROR")

                # --- fail-fast on mismatch ---
                if not ok:
                    for name in skipped:
                        report.append((name, "-", "-", "SKIPPED", False))
                    print_report(report, title="MIGRATION REPORT", verdict="FAILED")
                    log(f"STOP: {info.name} differs (source={src_n}, "
                        f"target={tgt_n}). Migration aborted.", level="ERROR")
                    return 1

                if interrupted["flag"]:
                    for name in skipped:
                        report.append((name, "-", "-", "SKIPPED", False))
                    print_report(report, title="MIGRATION REPORT (INTERRUPTED)",
                                 verdict="INCOMPLETE")
                    log("stopped after current table due to interrupt; "
                        "re-run with --resume to continue", level="WARN")
                    return 130
        finally:
            if fk_disabled:
                restore_session_settings(target)
                log("restored FK enforcement (session_replication_role=origin)")

        # ------------------------------------------------------------------
        # CREATE MISSING INDEXES (fix #3) - automatically, after the load.
        # ------------------------------------------------------------------
        idx_ok = idx_fail = 0
        if cfg.create_indexes_after_load:
            idx_ok, idx_fail = create_indexes(source, target, cfg)

        # ------------------------------------------------------------------
        # FINAL VERIFICATION REPORT (fix #4): success iff every source count
        # equals the target count.
        # ------------------------------------------------------------------
        for name in skipped:
            report.append((name, "-", "-", "SKIPPED", False))
        all_match = all(r[3] == "OK" for r in report if r[3] != "SKIPPED")
        verdict = "SUCCESS" if (all_match and idx_fail == 0) else "FAILED"
        print_report(report, title="MIGRATION REPORT", verdict=verdict)
        if cfg.create_indexes_after_load:
            log(f"indexes created/verified on target: {idx_ok} "
                f"({idx_fail} failed)")
        if skipped:
            log(f"NOTE: {len(skipped)} table(s) were explicitly skipped and "
                f"NOT migrated: {', '.join(skipped)}", level="WARN")
        return 0 if verdict == "SUCCESS" else 1


def run_verify(cfg: Config) -> int:
    log("VERIFY: comparing source vs target row counts")
    log(f"source : {cfg.redacted_source()}")
    log(f"target : {cfg.redacted_target()}")

    with connect(cfg.source_dsn, name="source",
                 connect_timeout=cfg.connect_timeout) as source, \
            connect(cfg.target_dsn, name="target",
                    connect_timeout=cfg.connect_timeout) as target:
        infos, skipped = plan_tables(source, cfg)

        rows = []
        mismatches = 0
        for info in infos:
            src_n, tgt_n = verify_table(source, target, cfg, info)
            ok = isinstance(tgt_n, int) and tgt_n == src_n
            if not ok:
                mismatches += 1
            rows.append((info.name, src_n, tgt_n, "OK" if ok else "MISMATCH",
                         info.is_hypertable))
        for name in skipped:
            rows.append((name, "-", "-", "SKIPPED", False))

        verdict = "FAILED" if mismatches else "SUCCESS"
        print_report(rows, title="VERIFICATION REPORT", verdict=verdict)
        if mismatches:
            log(f"VERIFY FAILED: {mismatches} table(s) mismatch", level="ERROR")
            return 1
        log("VERIFY OK: all tables match", level="INFO")
    return 0


def run_emit_indexes(cfg: Config, out_path: str) -> int:
    """Dump CREATE INDEX statements for every source index (rewritten with
    IF NOT EXISTS) so they can also be applied by hand if desired. Note: the
    migrate step already creates these automatically (CREATE_INDEXES=1)."""
    log(f"emitting index DDL for schema {cfg.schema} -> {out_path}")
    with connect(cfg.source_dsn, name="source",
                 connect_timeout=cfg.connect_timeout) as source:
        stmts = collect_index_ddl(source, cfg)

    lines = [
        "-- Auto-generated index DDL for RDS (source: local TimescaleDB).",
        "-- Recreates every non-primary-key index on the target.",
        "-- Safe to run repeatedly (IF NOT EXISTS).",
        "",
    ]
    current = None
    for tablename, indexname, ddl in stmts:
        if tablename != current:
            lines.append(f"\n-- {cfg.schema}.{tablename}")
            current = tablename
        lines.append(ddl + ";")

    with open(out_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"wrote {out_path} ({len(stmts)} indexes)")
    return 0


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(rows, *, title: str, verdict: Optional[str]) -> None:
    """Print the Table / Source / Target / Status report.

    rows: iterable of (name, source_count, target_count, status, is_hypertable).
    verdict: final "SUCCESS"/"FAILED"/... banner, or None to omit it.
    """
    name_w = max([len("Table")] + [len(str(r[0])) for r in rows]) + 2
    src_w = max([len("Source")] + [len(str(r[1])) for r in rows]) + 2
    tgt_w = max([len("Target")] + [len(str(r[2])) for r in rows]) + 2

    header = (f"{'Table':<{name_w}}{'Source':<{src_w}}"
              f"{'Target':<{tgt_w}}{'Status'}")
    rule = "-" * len(header)

    print()
    print(title)
    print(header)
    print(rule)
    for name, src_n, tgt_n, status, _is_hyper in rows:
        print(f"{str(name):<{name_w}}{str(src_n):<{src_w}}"
              f"{str(tgt_n):<{tgt_w}}{status}")
    print(rule)

    counted = [r for r in rows if r[3] != "SKIPPED"]
    ok = sum(1 for r in counted if r[3] == "OK")
    bad = sum(1 for r in counted if r[3] not in ("OK", "DRY-RUN"))
    skipped = sum(1 for r in rows if r[3] == "SKIPPED")
    print(f"tables: {len(counted)} processed, {ok} OK, {bad} failing, "
          f"{skipped} skipped")
    if verdict:
        banner = f"  MIGRATION {verdict}  "
        edge = "=" * len(banner)
        print(edge)
        print(banner)
        print(edge)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate local TimescaleDB data into AWS RDS PostgreSQL "
                    "without pg_restore or TimescaleDB on the target.",
    )
    parser.add_argument(
        "mode", nargs="?", default="migrate",
        choices=["migrate", "verify", "emit-indexes"],
        help="migrate (default) | verify | emit-indexes",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="read + report only; no writes to target")
    parser.add_argument("--resume", action="store_true",
                        help="skip completed tables and resume hypertables from "
                             "the target water-mark")
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(__file__), "create_missing_indexes.generated.sql"),
        help="output path for emit-indexes")
    args = parser.parse_args(argv)

    cfg = load_config()

    # emit-indexes only needs the source; every other mode needs the target.
    if args.mode != "emit-indexes" and not cfg.target_configured():
        log("Target connection is not set. Configure the RDS connection via "
            "TARGET_DSN or TARGET_HOST/TARGET_DB/... (see README).",
            level="ERROR")
        return 2

    try:
        if args.mode == "migrate":
            return run_migrate(cfg, dry_run=args.dry_run, resume=args.resume)
        if args.mode == "verify":
            return run_verify(cfg)
        if args.mode == "emit-indexes":
            return run_emit_indexes(cfg, args.out)
    except KeyboardInterrupt:
        log("aborted by user", level="WARN")
        return 130
    except psycopg2.Error as exc:
        log(f"database error: {exc}", level="ERROR")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
