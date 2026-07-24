# Release Report — Backend Migration to `jnpa_schema_v3`

**Branch:** `migrate-schema-v3` (uncommitted, pending review)
**Date:** 2026-07-24
**Scope:** complete backend cutover from legacy schema `jnpa` (database `jnpa3`)
to the canonical v3 database `jnpa_schema_v3` (schemas `core` / `staging` / `mart`).

---

## 1. Target topology (Scenario B — confirmed by stakeholder)

| Database | Schema | Role after deployment |
|---|---|---|
| **`jnpa_schema_v3`** | `core` | ALL runtime reads/writes — 171+ tables |
| **`jnpa_schema_v3`** | `staging` | text-typed landing tables (arch ETL) |
| **`jnpa_schema_v3`** | `mart` | ALL KPI/analytics view reads — 20 views |
| `jnpa_schema_v3` | `jnpa` *(temporary)* | copied in for backfill only; dropped by `0900` after verification |
| `jnpa3` | `jnpa` | frozen legacy — **rollback reference only**, never addressed by runtime |

The application code is database-name agnostic: every statement is
schema-qualified (`core.*` / `mart.*` / `staging.*`). The database is selected
exclusively by the five DSN environment variables.

## 2. Migration summary

* **1,509** hard-coded `jnpa.*` SQL references eliminated across the backend.
* **106 legacy operational tables** ported to `core` under canonical naming
  (singular; collisions resolved: `drivers→driver_identity`,
  `vehicle_master→vehicle_rc`, `services→ulip_service`,
  `berthing_reports→berthing_record`).
* **19 legacy tables** mapped onto architecture tables (`core.igm` family,
  `core.advance_list_container`/`advance_list_dg`,
  `core.delivery_order`/`_line`, `core.transporter`, `core.driver`, `core.pdp`,
  `core.vehicle`, `core.ref_shipping_line`) with additive column extensions
  preserving every legacy DTO field.
* **13 legacy views** re-created in `mart` (`mart.v_gate_throughput`, …,
  `mart.v_customs_container_status`).
* **21 runtime-DDL sites** disabled behind `JNPA_RUNTIME_DDL` (default `0`) —
  the backend can no longer create or mutate schema objects at boot.
* **API contracts frozen:** all responses byte-compatible (verified by live
  endpoint sweep); Kafka topics/event-types, Redis usage, auth untouched.

## 3. Files changed

161 modified + `infra/postgres/v3/` (9 files) + `.gitignore`.

| Category | Count | Notes |
|---|---|---|
| Service repositories | 12 | customs, shipping_lines, transporters_drivers, driver_master, cargo, cfs_ecy, berthing ×2, performance ×2, fastag ×2 |
| Gateway routers | 41 | all `/api/*` routers |
| Gateway core/ext modules | 21 | main, audit, enforcement, enrollment, geofence, fleet, vehicle_intel, *_ext |
| Standalone services | 11 | parking, gate-data, empty-container, carbon, identity |
| Ingest / AI / scenarios | 24 | rfid, trucking_app, vahan, anomaly, congestion, scenarios |
| Shared library | 4 | vahan_db, fastag, schemas, tracing |
| Scripts | 10 | import_*, demo_reset, bootstrap_check, validate_audit_persistence |
| Tests | 14 | incl. 5 drift-guard suites rewritten to validate against the v3 runbook |
| v3 migration runbook | 9 | `infra/postgres/v3/0100…0900` |

## 4. Database objects added (all additive)

* 106 ported `core` tables + their indexes, sequences, CHECK constraints, FKs
* 2 trigger functions (`core.set_updated_at`,
  `core.geofence_events_default_event_type`) + row triggers
* 1 operational sequence (`core.challan_seq`) + per-table id sequences
* Additive columns on 14 architecture tables (legacy ids, `message_id` lineage,
  `iso_valid`, `row_sha256`, upload ledger links, timestamps) — **no existing
  column, PK, or seeded value modified**
* 13 `mart` views; upsert-arbiter unique indexes
  (`uq_driver_licence_norm`, `uq_alc_file_rowsha`, `uq_dol_legacy_dedup`, `uq_rms_scan_report_igm`)

## 5. Data migrated (local rehearsal, exact parity)

| Data set | Rows | Result |
|---|---|---|
| Transporters | 2,195 → `core.transporter` | 100 % + legacy ids preserved |
| Driver master | 31,498 enriched into 31,846 seeded `core.driver` | licence-matched |
| PDP ledger | 367,078 → `core.pdp` | 100 % |
| SL advance lists | 8,877 + 417 DG splits | 100 % |
| Delivery orders | 5 flat → 3 headers + 5 lines | 100 % |
| Operational tables (alerts, gate, geofence, cargo, …) | 104 tables | count parity verified |
| `truck_telemetry` | 40,895,948 -> `core.truck_telemetry` | 100 % exact parity |
| `rfid_read` | 25,032,300 -> `core.rfid_read` | 100 % exact parity |
| Data-quality log | 592 anomalies → `core.dq_issue` | per arch DQ pattern |

## 6. Tests

* **530 passed / 0 failed / 0 errors** (1 skipped) across the backend suite.
* Every rewritten INSERT/UPSERT EXPLAIN-validated against a live v3 database.
* Live endpoint sweep returned 200 + correct DTOs for Transporters, Drivers,
  PDP history, Vehicles, Cargo, Customs, Shipping Lines, CFS/ECY, KPI, Alerts,
  Blacklist.

## 7. Production cutover runbook (Scenario B)

```text
0. pg_dump snapshot of jnpa3 (safety).
1. infra/postgres/v3/0100_copy_legacy_schema.sh      # jnpa3.jnpa -> jnpa_schema_v3.jnpa
2. psql -d jnpa_schema_v3 -f 0101_core_operational_ext.sql
3. psql -d jnpa_schema_v3 -f 0102_arch_extensions.sql
4. psql -d jnpa_schema_v3 -f 0103_mart_views.sql
   psql -d jnpa_schema_v3 -f 0104_operational_fixups.sql
5. psql -d jnpa_schema_v3 -f 0201_backfill_ported.sql
   psql -d jnpa_schema_v3 -f 0202_backfill_arch.sql
   psql -d jnpa_schema_v3 -f 0203_backfill_timeseries.sql   # long-running, restartable
6. Flip the five DSNs in the production env:  /jnpa3 -> /jnpa_schema_v3
      POSTGRES_DSN, RFID_POSTGRES_DSN, TRUCK_POSTGRES_DSN,
      CONGESTION_POSTGRES_DSN, ANOMALY_POSTGRES_DSN
   (JNPA_RUNTIME_DDL stays unset.)
7. Deploy the migrate-schema-v3 build; run the smoke list (all modules 200).
8. After verification window: psql -d jnpa_schema_v3 -f 0900_drop_legacy_schema.sql
   (removes ONLY the copied jnpa schema in the target; jnpa3 is untouched.)
```

**Assumption (verify at step 1):** production `jnpa3` on RDS contains plain
tables only (RDS has no TimescaleDB), so `pg_dump | psql` copies cleanly. The
local rehearsal used TimescaleDB, whose internal triggers were explicitly
excluded from the ported DDL.

## 8. Rollback procedure

1. Revert the five DSNs to `/jnpa3` and redeploy the previous image — the
   legacy schema in `jnpa3` was never written to by the new build and is
   immediately consistent.
2. All v3 DDL is additive inside `jnpa_schema_v3`; nothing to unwind.
3. Writes made to `jnpa_schema_v3` between cutover and rollback would need
   reconciliation (export from `core.*` deltas by `created_at`) — keep the
   verification window short.

## 9. Known risks

| Risk | Severity | Mitigation |
|---|---|---|
| Writes landing in v3 during a rollback window are not auto-synced back to jnpa3 | Med | short verification window; delta export by `created_at` |
| Upload rows with unresolvable terminal codes now fail the file loudly (legacy stored free text) | Low | `ref_terminal_alias` seeding; failures logged to `core.dq_issue` |
| Customs response `id` values now derive from stable legacy ids via ext columns; NEW imports mint ids from sequences ≥ current max | Low | verified locally; sequence setvals in `0202`/`0104` |
| `time_bucket()` in 4 KPI views requires the same extension surface as legacy prod (they ran on jnpa3 already) | Low | views copied verbatim from legacy definitions |
| `.env.migration-test` (local only) contains credentials | — | gitignored; not part of the release |
| Dead legacy DDL strings remain in gated `ensure_*` functions | Cosmetic | cleanup PR after release |

## 10. Environment verification (pre-flip state)

* Five production DSNs currently point at `jnpa3` — **flipped at step 6, not before.**
* `JNPA_RUNTIME_DDL` unset in every env file and container → runtime DDL disabled.
* No other env var changes required.

---

## 11. Final verification (local rehearsal, 2026-07-24)

* RFID/telemetry parity: exact — 40,895,948 and 25,032,300 rows, 0 loader errors.
* DB health: 171 core + 2 staging tables, 20 mart views, 460 indexes,
  408 constraints, 121 sequences, 2 trigger functions.
* Integrity: 0 unvalidated FKs; 0 duplicate ids (transporter/driver/alc/pdp);
  0 orphans across driver->transporter, alc->sl_import_file,
  delivery_order_line->delivery_order, transporter_vehicle->transporter.
* API smoke: 16/16 endpoints HTTP 200 across Transporters, Drivers, Vehicles,
  Cargo, Customs, Shipping Lines, CFS/ECY, KPI, Alerts, Blacklist, Gate,
  Performance, Geo (zones + events), Notifications (recent + health).
* Note: loader min(ts) boundary bug found in rehearsal (first row excluded) —
  fixed in 0203 and data corrected; production runbook carries the fix.
