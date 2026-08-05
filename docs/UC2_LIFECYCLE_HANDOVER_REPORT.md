# POC-3 Cargo Backend — UC-II Lifecycle → UC-III Handover

**Objective:** make POC-3 the single owner of the complete UC-II cargo lifecycle,
from *Add Cargo* to *UC-III handover*, so POC-2 completes UC-II **only** by
consuming POC-3 APIs. Everything below is **additive** and **backward compatible**
— no existing migration, column, API shape, or working module was rewritten.

Date: 2026-07-16 · Owner: POC-3 Cargo Backend

---

## 1. Architecture audit (before coding)

POC-3 already had a clean, layered Cargo backend — the work extended it in place:

| Layer | File | Role |
|---|---|---|
| Router (thin) | `gateway/routers/cargo.py` | Pydantic DTOs, ISO-6346 validation, HTTP status mapping |
| Service (orchestration) | `services/cargo/service.py` | Business logic, event emission, observability, typed errors |
| Repository (only SQL) | `services/cargo/repository.py` | Raw parameterised SQL over the shared async engine |
| Schema | `infra/postgres/migrations/00xx_*.sql` + `infra/postgres/init.sql` | Migrations mirrored into `init.sql` for fresh boots |
| Tests | `tests/test_cargo.py` | `FakeCargoRepo` via `dependency_overrides` + real-DB integration |

**Events architecture:** POC-3 uses a **DB-backed append-only event log**
(`jnpa.cargo_events`), polled by POC-2 via `GET /api/cargo/events?since=<cursor>`.
This is the "existing event architecture" — it was reused, not replaced. (The
repo's Kafka is only in the ANPR ingest path, not cargo.)

**Pre-existing surface:** Cargo CRUD, events, yard-assignment, workflow
(TRIGGER→APPROVE/REJECT), notifications, yard/rake/reefer planning, yard
optimization, role scoping, auth.

## 2. Issues found (gaps vs. UC-II lifecycle)

1. **No unified lifecycle** — only `customs_status`, `is_released`, and
   `workflow_status` existed; there was no single validated state machine.
2. **No vessel-discharge endpoint.**
3. **Yard-assignment did not advance a lifecycle** (only set `yard_block`).
4. **Yard planning had no block/row/slot/position** and no
   `cargo.yard_position_allocated` event.
5. **No scan-queue endpoint.**
6. **No scan/customs verification endpoint** or records.
7. **Release was lenient** (`PUT is_released=true`) — no yard+verify precondition,
   no duplicate-release protection.
8. **No `?status=` handover filter** and the released event lacked the handover
   payload (yard location + vehicle details).

## 3. Implementation completed

A single, forward-only, audited lifecycle is now the source of truth:

```
CREATED → VESSEL_DISCHARGED → YARD_ASSIGNED
        → [YARD_POSITION_ALLOCATED | REEFER_PLANNED | RAKE_ASSIGNED]  (optional)
        → SCAN_PENDING (derived queue label) → VERIFIED → RELEASED
```

- **State machine** (`services/cargo/service.py`): each state has a rank; transitions
  are strictly forward and may **never skip a mandatory gate**
  (`CREATED, VESSEL_DISCHARGED, YARD_ASSIGNED, VERIFIED, RELEASED`). Optional
  planning states between yard-assign and verify may be skipped. Illegal moves
  raise `CargoTransitionError` → **HTTP 409**; unknown container → **404**.
- **Atomic + audited transitions** (`repository.transition_lifecycle`): `SELECT … FOR
  UPDATE`, membership check against the service-supplied predecessor set, `UPDATE`,
  and an audit-row insert — all in one transaction, so the record and its audit
  trail can never diverge. NULL `lifecycle_status` (legacy rows) is resolved from
  the legacy columns (`is_released`/`yard_block`).
- **Backward compatibility:** `lifecycle_status` has a DB `DEFAULT 'CREATED'` and is
  **not** client-writable via `PUT`. The legacy `PUT is_released=true` path still
  works and now also best-effort drives the lifecycle to `RELEASED`, so the
  handover query is consistent regardless of which release path was used.
- **UC-III handover:** the validated `POST /release` emits `cargo.released` with
  `{status, yard_location, vehicle_details, is_released}`; `GET /api/cargo?status=RELEASED`
  returns released cargo. Both reuse the existing event log + list surface.

## 4. Files modified

| File | Change |
|---|---|
| `infra/postgres/migrations/0023_cargo_lifecycle.sql` | **new** — additive migration |
| `infra/postgres/init.sql` | mirrored the 0023 DDL for fresh boots |
| `services/cargo/repository.py` | `lifecycle_status` column; `CargoTransitionError`; `_infer_lifecycle`; `transition_lifecycle`, `list_lifecycle_events`, `list_scan_queue`, `record_scan_verification`, `create_yard_position`; `lifecycle_status` filter |
| `services/cargo/service.py` | state machine (`can_transition`/`allowed_predecessors`); new events; `discharge_cargo`, `assign_yard`, `allocate_yard_position`, `scan_queue`, `verify_cargo`, `release_cargo`, `list_lifecycle_history`; reefer/rake lifecycle advance; legacy-PUT release sync |
| `services/cargo/__init__.py` | exports for the new symbols |
| `gateway/routers/cargo.py` | DTOs + endpoints (below); `lifecycle_status` on `CargoOut`; `?status=` filter; yard-assignment now advances lifecycle |
| `tests/test_cargo.py` | extended `FakeCargoRepo`; 11 new lifecycle tests + 1 real-DB lifecycle test; env-overridable real-DB DSN; fixed a latent invalid-ISO in the pre-existing real-DB test |
| `docs/CARGO_API.md` | documented the new endpoints |

## 5. APIs added / changed

| Method & path | Purpose | Task |
|---|---|---|
| `POST /api/cargo/{cn}/discharge` | Mark discharged from vessel → `VESSEL_DISCHARGED` (does **not** auto-assign yard) | #2 |
| `PUT /api/cargo/{cn}/yard-assignment` | *(existing)* now also advances → `YARD_ASSIGNED` | #3 |
| `POST /api/cargo/{cn}/yard-position` | Allocate block/row/slot/position → `YARD_POSITION_ALLOCATED` | #4 |
| `GET /api/cargo/scan-queue` | Yard-assigned, not released, not verified → `status: SCAN_PENDING` | #5 |
| `POST /api/cargo/{cn}/verify` | Customs/scan verification → `VERIFIED` | #6 |
| `POST /api/cargo/{cn}/release` | **Validated** release (requires VERIFIED; duplicate → 409) | #7 |
| `GET /api/cargo?status=RELEASED` | UC-III handover list | #8 |
| `GET /api/cargo/{cn}/lifecycle` | Append-only lifecycle audit history | #1 |

All existing `/api/cargo` routes are unchanged and backward compatible.

## 6. Migration list

- **`0023_cargo_lifecycle.sql`** (new, additive, idempotent — `IF NOT EXISTS`):
  - `jnpa.cargo.lifecycle_status text DEFAULT 'CREATED'` + CHECK + index + one-time
    backfill of pre-existing rows from `is_released`/`yard_block`
  - `jnpa.cargo_yard_plans.{yard_row, yard_slot, yard_position}` (nullable)
  - `jnpa.cargo_lifecycle_events` (append-only transition audit)
  - `jnpa.cargo_scan_verifications` (scan/customs verification records)
- Mirrored into `infra/postgres/init.sql` (fresh-boot bootstrap).
- No existing migration was modified.

## 7. Events list (all on the existing `cargo.*` log)

New: `cargo.vessel_discharged`, `cargo.yard_position_allocated`, `cargo.verified`,
`cargo.reefer_planned`, `cargo.rake_assigned`, `cargo.lifecycle_changed` (fires on
every accepted transition). Enriched: `cargo.released` now carries
`{status, yard_location, vehicle_details, is_released}`. All legacy topics
(`cargo.created`, `cargo.yard_assigned`, `cargo.status_changed`, `cargo.gate_*`,
`cargo.updated`, `cargo.deleted`, `cargo.queue_updated`, …) still fire unchanged.

## 8. Test results

`tests/test_cargo.py` — **61 passed** (was 49), including **2 real-DB integration
tests** against live Postgres. New coverage:

- Full lifecycle create → discharge → yard-assign → yard-position → scan-queue →
  verify → release → handover (fake **and** real DB).
- State-machine unit tests (forward-only, mandatory gates, predecessor sets).
- Invalid transitions: verify-before-yard (409), release-before-verify (409),
  duplicate release (409), double discharge (409), discharge unknown (404).
- Scan-queue inclusion/exclusion; verify-rejected does not advance.
- Regression: legacy `PUT is_released=true` still works and stays lifecycle-consistent;
  discharge does not auto-assign a yard; reefer/rake advance the optional states.

```
61 passed, 1 warning
```

(The single repo-wide collection error, `tests/test_empty_container.py`, is a
pre-existing unrelated `ModuleNotFoundError: empty_container` and is not caused by
this work.)

## 9. Deployment verification (local stack)

| Check | Result |
|---|---|
| Docker containers | up (`jnpa-postgres`, `jnpa-gateway`, …) |
| PostgreSQL | reachable on host `5433`; cargo schema present |
| Migration 0023 | applied to the running DB (idempotent) |
| Gateway | restarted, `GET /healthz` → 200 |
| OpenAPI | all 6 new paths present at `:8000/openapi.json` |
| Authentication | `/api/cargo` unchanged (any authenticated role; open dev profile) |
| Live APIs | full lifecycle driven end-to-end through the gateway on `:8000`, all guards (409/404) correct, handover query + audit + events verified |

### EC2 deployment steps (to apply there)

```bash
# 1. Ship the code (services/, gateway/ are volume-mounted read-only)
git pull   # or rsync the changed files

# 2. Apply the additive migration (idempotent)
psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 \
  -f infra/postgres/migrations/0023_cargo_lifecycle.sql

# 3. Restart the gateway to load the new router/service code
docker compose restart gateway   # or the AWS compose file

# 4. Smoke test
curl -s "$BASE/api/cargo/scan-queue" | jq .
```

Fresh environments get everything automatically via `infra/postgres/init.sql`.

## 10. Confirmation — POC-2 completes UC-II only through POC-3

POC-3 now owns the **entire** cargo lifecycle. POC-2 needs no cargo backend or DB;
it drives every UC-II step and reads the UC-III handover purely through POC-3 HTTP
APIs and the `cargo.*` event log:

`POST /api/cargo` → `POST /{cn}/discharge` → `PUT /{cn}/yard-assignment` →
`POST /{cn}/yard-position` → *(optional `reefer-planning`/`rake-planning`)* →
`GET /scan-queue` → `POST /{cn}/verify` → `POST /{cn}/release` →
`GET /api/cargo?status=RELEASED` + `cargo.released` event.

Business rules enforced server-side (POC-3): mandatory-gate ordering, no step
skipping, yard-assign + verify required before release, duplicate release rejected,
full audit trail. **POC-2 consumes; POC-3 owns.**
