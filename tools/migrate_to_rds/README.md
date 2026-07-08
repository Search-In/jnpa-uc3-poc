# migrate_to_rds

A **standalone, one-time** migration utility that copies the local Docker
PostgreSQL (TimescaleDB) database into **AWS RDS PostgreSQL** — including the
TimescaleDB **hypertables**, which `pg_dump`/`pg_restore` cannot move into a
plain-PostgreSQL target.

It touches **no application code**. It is a self-contained tool driven entirely
by environment variables.

---

## Why not `pg_dump` / `pg_restore`?

A TimescaleDB hypertable's rows physically live in per-chunk child tables under
`_timescaledb_internal`. A `pg_restore` into a database **without** the
TimescaleDB extension (RDS PostgreSQL 18 has no such extension) cannot recreate
that partitioning, so the chunks are never restored and the parent table comes
back **empty**.

This tool reads from the **parent** hypertable with an ordinary `SELECT`
(TimescaleDB transparently unions the chunks) and writes into an ordinary
PostgreSQL table on RDS with a plain, batched `INSERT`. No `pg_restore`, no
TimescaleDB required on the target.

| Requirement | How it's met |
|---|---|
| Do **not** use `pg_restore` | Pure `psycopg2` reads/writes |
| Do **not** require TimescaleDB on RDS | Hypertables become plain tables |
| Read local, write RDS directly | Two live connections, streamed |
| Detect all `jnpa` tables | `information_schema` introspection |
| Skip Timescale metadata/chunks | Only the `jnpa` schema is read; views & chunk tables excluded |
| Preserve FK relationships | FK-graph **topological sort** + `session_replication_role=replica` during load, restored after |
| Schema preflight | Compares tables/columns/types **before any write**; aborts on mismatch |
| Create missing indexes automatically | After load, recreates every source index (incl. hypertable time indexes) — no manual SQL |
| Normal tables → UPSERT on PK | `INSERT … ON CONFLICT (pk) DO UPDATE` |
| Hypertables → append | Plain `INSERT`, made idempotent by a `ts` water-mark |
| Batches of 5k–10k | `BATCH_SIZE` (default 5000), commit per batch |
| Preserve timestamps & IDs | Values copied verbatim; `jsonb` round-tripped losslessly |
| Resume after interruption | Water-mark (hypertables) + state file (skip completed) |
| Preserve sequences | `setval` on every serial/identity sequence to `max(id)` after each table |
| Verify row counts **after each table** | Full `COUNT(*)` compare inline; stop on first mismatch |
| No table skipped silently | Skipped tables appear as `SKIPPED` in the report + a WARN |
| Final SUCCESS / FAILED verdict | Printed only after comparing source vs target counts |
| Log progress, retry, per-table counts | Structured logs, `tqdm` bars, exponential-backoff retry |

---

## What it does

1. **Introspects** the source `jnpa` schema — every base table, its columns, its
   primary key, and (from the TimescaleDB catalog) which tables are hypertables
   and on which time column. Nothing is hard-coded, so the tool stays correct as
   the schema evolves.
2. **Normal tables** (have a primary key) are copied with
   `INSERT … ON CONFLICT (pk) DO UPDATE` — a re-run is **idempotent**.
3. **Hypertables** (no primary key: `anpr_reads`, `rfid_reads`,
   `truck_telemetry`, `traffic_snapshots`) are copied in `ts`-ascending,
   batch-committed order. Before copying, the tool reads the target's
   `max(ts)` **water-mark**: rows at the boundary are deleted and everything
   `>= max(ts)` is re-copied. Because the target always holds a contiguous
   prefix by `ts`, this makes the append **idempotent** whether you are
   re-running or resuming after a crash — with no primary key needed.
4. **Streams** each table through a server-side cursor (constant memory, safe for
   large hypertables) and **commits every batch**.
5. **Resets sequences**: serial/identity ID values are copied verbatim (so IDs
   are preserved), then each backing sequence is advanced with `setval(...)` to
   the table's `max(id)` on the target — otherwise the first post-migration
   insert on RDS would restart at 1 and collide with a migrated ID.
6. **Verifies each table immediately** after copying it: a full `COUNT(*)` on
   both sides. On the **first mismatch it stops**, prints the offending table
   with source/target counts, and ends with `MIGRATION FAILED` (exit 1).
   Otherwise the run ends with `MIGRATION SUCCESS` (exit 0). Tables excluded via
   `SKIP_TABLES`/`ONLY_TABLES` are shown as `SKIPPED` — never dropped silently.

### The end-of-run report

```
MIGRATION REPORT
Table              Source  Target  Status
-----------------------------------------
drivers            6       6       OK
vehicle_master     3       3       OK
alerts             776     776     OK
anpr_reads         2498    2498    OK
traffic_snapshots  26      26      OK
-----------------------------------------
tables: 5 processed, 5 OK, 0 failing, 0 skipped
=====================
  MIGRATION SUCCESS
=====================
```

Exit codes: `0` = SUCCESS, `1` = FAILED/mismatch, `2` = misconfiguration,
`130` = interrupted (re-run with `--resume`). Both `migrate` and `verify`/
`verify.py` share this report and these codes, so either can gate a cut-over.

---

## Install

```bash
cd tools/migrate_to_rds
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Configure (environment variables)

Either provide a full libpq DSN, or the discrete parts.

```bash
# --- SOURCE: local Docker Postgres (host port 5433 -> container 5432) ---
export SOURCE_HOST=localhost
export SOURCE_PORT=5433
export SOURCE_DB=postgres
export SOURCE_USER=postgres
export SOURCE_PASSWORD=****            # your local POSTGRES_PASSWORD
# ...or:  export SOURCE_DSN="host=localhost port=5433 dbname=postgres user=postgres password=****"

# --- TARGET: AWS RDS PostgreSQL 18 ---
export TARGET_HOST=my-instance.abc123.ap-south-1.rds.amazonaws.com
export TARGET_PORT=5432
export TARGET_DB=jnpa3
export TARGET_USER=postgres
export TARGET_PASSWORD=****
export TARGET_SSLMODE=require          # RDS enforces TLS
# ...or:  export TARGET_DSN="postgresql://postgres:****@...rds.amazonaws.com:5432/jnpa3?sslmode=require"
```

Optional tuning:

| Variable | Default | Purpose |
|---|---|---|
| `MIGRATE_SCHEMA` | `jnpa` | Schema to migrate |
| `BATCH_SIZE` | `5000` | Rows per read/insert/commit batch (try 10000) |
| `SKIP_TABLES` | – | Comma-separated bare table names to exclude |
| `ONLY_TABLES` | – | Only migrate these tables (comma-separated) |
| `HYPERTABLES` | – | Override hypertable list if the TS catalog is unreadable |
| `STATE_FILE` | `./migration_state.json` | Resume/progress state |
| `MAX_RETRIES` | `5` | Retries per batch on transient errors |
| `RETRY_BASE_DELAY` | `1.5` | Backoff base seconds (exponential) |
| `STATEMENT_TIMEOUT_MS` | `0` | Per-session statement timeout on target (0 = off) |
| `CONNECT_TIMEOUT` | `10` | Connection timeout (s) so an unreachable RDS fails fast |
| `DISABLE_FK_DURING_LOAD` | `1` | `SET session_replication_role=replica` during load; restored after |
| `CREATE_INDEXES` | `1` | Auto-create missing indexes on the target after the data load |
| `STRICT_TYPES` | `1` | Abort preflight on any source/target column type mismatch |

A `.env` file in this directory is auto-loaded if `python-dotenv` is installed.

---

## Usage

```bash
# 1. Preview — read + report only, no writes to the target
python migrate.py migrate --dry-run

# 2. Migrate everything
python migrate.py migrate

# 3. Resume after an interruption (skips completed tables; hypertables
#    continue from the target water-mark)
python migrate.py migrate --resume

# 4. Verify — compare source vs target row counts (exit 1 on any mismatch)
python migrate.py verify          # or:  python verify.py

# 5. Generate index DDL from the LIVE source (authoritative)
python migrate.py emit-indexes --out create_missing_indexes.generated.sql
```

### Recommended minimal-downtime run book (zero manual SQL)

1. **Prepare RDS**: ensure the `jnpa` schema/tables exist (they already do per the
   task). Indexes do **not** need pre-creating — the tool builds them after load.
2. **Dry run**: `python migrate.py migrate --dry-run`. This also runs the schema
   preflight, so any missing table/column/type is reported before you commit.
3. **Bulk migrate** (restartable): `python migrate.py migrate`. This one command
   preflights the schema, loads every table in FK-safe order (FK checks disabled
   for the session and restored afterwards), preserves sequences, **creates all
   missing indexes**, and prints the final verification report.
4. **Short freeze**: stop local writers, then run
   `python migrate.py migrate --resume` to sweep the final rows (idempotent).
5. **Verify**: `python verify.py` — must report `MIGRATION SUCCESS`.
6. **Cut over** the application `POSTGRES_DSN` to RDS.

### Indexes — created automatically

The migrate step recreates **every** source index on the target after the data
load (`CREATE_INDEXES=1`, the default), including the TimescaleDB-auto-created
time indexes on the hypertables that a plain-PostgreSQL target would otherwise
lack. **No manual SQL is required.**

The checked-in `create_missing_indexes.sql` and `emit-indexes` mode remain
available if you ever want to inspect or apply the DDL by hand, but they are
optional:

```bash
python migrate.py emit-indexes --out create_missing_indexes.generated.sql  # optional
```

---

## Idempotency & resume — how it actually works

* **Normal tables** use `ON CONFLICT (pk) DO UPDATE`, so re-running is always
  safe and converges to the source.
* **Hypertables** are reconciled against `max(ts)` on the target on **every**
  run (not just `--resume`), so re-running never duplicates rows.
* The **state file** (`migration_state.json`) records per-table status. With
  `--resume`, tables already marked `complete` skip the *copy* — but they are
  **still count-verified**; if a "complete" table no longer matches (target
  changed out-of-band), it is automatically re-migrated once, then re-verified.
  Without `--resume`, every table is re-copied (idempotently) — use this for a
  full reconciliation.
* **Ctrl-C** is caught: the current batch finishes and commits, then the tool
  stops cleanly. Re-run with `--resume` to continue.

---

## Rollback

This migration is **non-destructive to the source** — it only reads from local
Postgres. "Rollback" therefore means undoing changes on the **RDS target**.

**Fastest (recommended): drop and recreate the schema on RDS.**
```sql
-- On RDS (jnpa3). Destroys ALL migrated data in the schema.
DROP SCHEMA jnpa CASCADE;
CREATE SCHEMA jnpa;
-- then re-apply your schema DDL (init.sql + migrations, minus TimescaleDB calls)
```

**Per-table (keep the schema, clear the data):**
```sql
-- On RDS. TRUNCATE is transactional and resets the tables to empty.
TRUNCATE jnpa.anpr_reads, jnpa.rfid_reads, jnpa.truck_telemetry,
         jnpa.traffic_snapshots RESTART IDENTITY;
-- add any normal tables you want to reset as well
```

**Reset the migration state** (so the next run starts clean):
```bash
rm -f tools/migrate_to_rds/migration_state.json
```

**Application rollback**: the app is unaffected until you repoint
`POSTGRES_DSN`. To roll back the cut-over, simply point `POSTGRES_DSN` back at
the local Docker Postgres — the source was never modified.

Because the migration is idempotent, you can also "roll forward" instead of
back: fix the issue and re-run `python migrate.py migrate` (or `--resume`).

---

## Files

| File | Purpose |
|---|---|
| `migrate.py` | The utility (modes: `migrate`, `verify`, `emit-indexes`) |
| `config.py` | Environment-variable configuration |
| `verify.py` | Standalone row-count verification wrapper |
| `create_missing_indexes.sql` | Hand-checked index DDL for the hypertables |
| `requirements.txt` | Python dependencies |
| `migration_state.json` | Auto-created resume/progress state (git-ignore it) |

---

## Notes & limitations

* Only the configured schema (`jnpa`) is read, so TimescaleDB internal chunk
  tables and continuous-aggregate materializations are never touched.
* `emit-indexes` reproduces every **non-primary-key** index (PKs come with your
  schema DDL). It skips chunk indexes by filtering to ordinary tables
  (`relkind = 'r'`).
* Foreign keys are honoured by ordering plain tables before hypertables; if you
  have FK cycles, load with `SKIP_TABLES` in passes or disable triggers on the
  target for the load window.
* The tool assumes the target column layout matches the source (same column
  names). It selects/inserts an explicit, introspected column list, so extra
  columns on the target with defaults are fine.
