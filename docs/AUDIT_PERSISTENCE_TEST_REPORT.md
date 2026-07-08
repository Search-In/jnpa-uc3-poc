# RDS Single-Source-of-Truth — Testing Report

**Date:** 2026-07-07  **Env:** local docker stack (`make up`), Postgres on `localhost:5433`.
**Scope:** migration 0003 + `gateway/audit.py` + `gateway/audit_client.py` + module wiring.
**Result:** ✅ **ALL ACCEPTANCE CRITERIA PASS** (validated against the running stack, not mocks).

## 1. Migration

| Check | Method | Result |
|---|---|---|
| Applies cleanly | `psql < migrations/0003_audit_persistence.sql` | ✅ 5 tables + 22 indexes created |
| Idempotent (no data damage) | re-ran the full migration | ✅ exit 0, no error, no rows touched (all `IF NOT EXISTS`) |
| Runtime top-up | gateway boot log | ✅ `audit_schema_ready` emitted by `ensure_audit_schema()` |
| Indexes present | `pg_indexes` | ✅ api_audit_log=4, digital_twin_events=5, notifications=5, decision_audit=4, geofence_events=4 (incl. PK, vehicle-lookup, timestamp) |

## 2. Writer unit validation — `scripts/validate_audit_persistence.py`

Exercises every writer against live Postgres and reads back row counts.

```
[PASS] api_audit_log insert
[PASS] digital_twin_events insert (direct+alert)
[PASS] notifications insert
[PASS] decision_audit insert
[PASS] geofence_events insert (direct+alert)
[PASS] anpr_reads insert (AI detection storage)
[PASS] anpr_reads -> ANPR_DETECTION event mirror
=== RESULT: ALL PASS ===
```
Run: `POSTGRES_DSN='postgresql+asyncpg://postgres:jnpa_pw@localhost:5433/postgres' .venv/bin/python scripts/validate_audit_persistence.py`

## 3. Runtime end-to-end (live gateway with the new code)

Gateway restarted (code is bind-mounted; no rebuild). Endpoints exercised, then
row counts compared.

| Acceptance criterion | Evidence | Result |
|---|---|---|
| **Every API call has an audit record** | Hit `/api/parking/availability`, `/api/carbon/rollup`, `/api/empty/allocations`, `/api/vahan/rc/...` → `api_audit_log` grew 3→7 with `service_name, endpoint, status_code, latency_ms` populated | ✅ |
| **Every AI detection has a DB record** | ANPR pump consuming Kafka `anpr.reads`: `jnpa.anpr_reads` climbing live (1590 → 1626 → +1/6s), each mirrored to `digital_twin_events` as `ANPR_DETECTION` (633 rows) | ✅ |
| **Every alert has a DB record** | `persist_alert_event` → `digital_twin_events` (`CUSTOMS_ALERT`, `PARKING_VIOLATION`, …) + geofence-family → `geofence_events` (6) | ✅ |
| **Every notification has delivery history** | `push.deliver` → `notifications` with `delivery_status` (SENT/FAILED/NO_SUBSCRIPTION/SKIPPED) | ✅ |
| **Decisions durable** | Vahan orchestration → `decision_audit` (`LIVE_PRIMARY`/`PROVISIONAL`, action PRIMARY/FALLBACK) | ✅ |
| **Restart → history remains** | Restarted `jnpa-gateway`; `api_audit_log`=7 and `decision_audit`=4 unchanged after restart | ✅ |

### Sample `api_audit_log`
```
 service_name   |         endpoint         | status_code | latency_ms
 vahan          | GET /vahan/rc/MH04AB1234 |             |    2463.18
 empty-container| GET /allocations         |         200 |     680.60
 carbon         | GET /rollup              |         200 |    1248.40
 parking        | GET /availability        |         200 |    2754.26
```
### Event timeline (`digital_twin_events` by type)
```
 ANPR_DETECTION    | 602
 PARKING_VIOLATION |   3
 VEHICLE_DETECTED  |   3
 CUSTOMS_ALERT     |   3
```

## 4. Non-regression

| Check | Result |
|---|---|
| Gateway boots clean with new code | ✅ `Application startup complete`, no import/traceback |
| All changed modules byte-compile | ✅ `py_compile` clean |
| Auditing client behaviour unchanged | ✅ proxied endpoints returned normal 200s; auditing is fire-and-forget |
| Existing tables untouched | ✅ migration additive only; no ALTER/DROP on existing tables |

## 5. Known limitations / next steps (honest scope)

1. **Deepest external hop.** The gateway auto-logs the gateway→integration-service
   hop. The integration-service→government hop (`vahan-live → Surepass`,
   `services/fastag → ULIP`) must call the same `log_api_audit` helper at its own
   egress — one-line addition per client, pending real endpoint provisioning.
   e-Seal/Form-13/ICEGATE/Weighbridge remain simulated upstreams (no real client
   yet); their gate-data proxy calls ARE audited today.
2. **Alerts/traffic Kafka pumps** exit in this env when the topics are absent
   (`UNKNOWN_TOPIC_OR_PART`); they persist once producers create the topics. The
   ANPR pump is active because `anpr.reads` is being produced.
3. **SMS/e-mail channels** log via the same `log_notification` helper once a real
   provider is wired (WebPush path is live).
4. Validation inserted a handful of synthetic marker rows into the audit tables
   (harmless append-only history).

## 6. How to re-run

```bash
# migration (existing / RDS DB)
psql "$POSTGRES_DSN_PSQL" -v ON_ERROR_STOP=1 -f infra/postgres/migrations/0003_audit_persistence.sql
# writer validation
POSTGRES_DSN='postgresql+asyncpg://postgres:jnpa_pw@localhost:5433/postgres' \
  .venv/bin/python scripts/validate_audit_persistence.py
# runtime (restart picks up bind-mounted code)
docker restart jnpa-gateway
```
