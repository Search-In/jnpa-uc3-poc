# Cargo API — Integration Guide (POC-2 Cargo-Twin Frontend)

The **POC-3 gateway** is the single common backend. The Cargo record lives once in
the shared PostgreSQL (RDS) and is served over REST at `/api/cargo`. **POC-2 needs
no backend and no database** — it calls these endpoints directly. Creating or
updating cargo through this API is immediately visible to both the POC-3 and POC-2
UIs, because there is exactly one table behind it.

> You do **not** need to read backend code to integrate. Everything required is below.

---

## Base URL

| Environment | Base URL |
|---|---|
| Local dev | `http://localhost:8000` |
| Deployed | the POC-3 gateway origin (e.g. `https://<gateway-host>`) |

**CORS:** enabled. Dev allows all origins (`*`). Production scopes origins via the
gateway's `CORS_ALLOW_ORIGINS` env — ask the backend team to add the POC-2 origin.
The `X-Total-Count` response header is CORS-exposed, so browser JS can read it.

**Auth:** In dev/demo the endpoints are open (no token). When the gateway runs with
`AUTH_ENABLED=true`, send `Authorization: Bearer <jwt>` (any valid stakeholder role
is accepted). Mint/refresh tokens via the gateway's `/api/auth` surface.

---

## Data model (`CargoOut`)

| Field | Type | Notes |
|---|---|---|
| `container_number` | string | **Primary key.** ISO-6346 (e.g. `MAEU6123458`). |
| `vessel_name` | string \| null | |
| `customs_status` | enum string | `PENDING` \| `CLEARED` \| `HELD` \| `UNDER_INSPECTION` |
| `yard_block` | string \| null | |
| `is_released` | boolean | |
| `vehicle_number` | string \| null | allocated haulage plate |
| `gate` | string \| null | gate id |
| `camera_id` | string \| null | ANPR camera id |
| `eta` | string (ISO-8601) \| null | timestamp, e.g. `2026-07-12T08:30:00Z` |
| `eseal_status` | enum string \| null | e-Seal state: `ACTIVE` \| `ARMED` \| `TAMPERED` \| `REMOVED` \| `NONE` |
| `eseal_number` | string \| null | electronic-seal id / number |
| `pre_document_status` | enum string \| null | `NOT_STARTED` \| `PENDING` \| `IN_PROGRESS` \| `COMPLETED` |
| `origin_stream` | string \| null | cargo source stream, e.g. `UC-II`. Input also accepts `originStream`. |
| `created_at` | string (ISO-8601) | server-set |
| `updated_at` | string (ISO-8601) | server-set (DB trigger) |

> **Contract extension (migration 0015).** `eseal_status`, `eseal_number`,
> `pre_document_status`, and `origin_stream` are **additive and nullable** — every
> existing request/response is unchanged. Create/patch them like any other field;
> `origin_stream` accepts either snake_case (`origin_stream`) or camelCase
> (`originStream`) on input and always serialises back as `origin_stream`.

> **ETA note:** `eta` is a full ISO-8601 **timestamp** (`timestamptz`). If the UI
> works in "minutes to arrival", convert to/from a timestamp at the edge
> (`now + N minutes`). The API accepts any valid ISO-8601 value.

---

## Endpoints

### 1. List cargo — `GET /api/cargo`
Returns `CargoOut[]`. Supports filtering + pagination (all optional, backward compatible).

**Query parameters**

| Param | Type | Default | Meaning |
|---|---|---|---|
| `container_number` | string | — | exact ISO-6346 match (validated → 400 if malformed) |
| `customs_status` | enum | — | one of the four statuses |
| `yard_block` | string | — | exact match |
| `is_released` | boolean | — | `true` / `false` |
| `vehicle_number` | string | — | exact match (normalised: upper, de-spaced) |
| `eseal_status` | enum | — | one of the five e-Seal states |
| `pre_document_status` | enum | — | one of the four pre-document states |
| `origin_stream` | string | — | exact match, e.g. `UC-II` |
| `role` | string | — | scope results to a user role (see **Role-based filtering** below) |
| `limit` | int (1–1000) | 100 | page size |
| `offset` | int (≥0) | 0 | page start |

**Response header:** `X-Total-Count` — total rows matching the filters *before*
pagination (use it to render page controls).

**Role-based filtering.** Pass `?role=<role>` to scope the list to the records a
role may see. When the gateway runs with `AUTH_ENABLED=true` the **authenticated
principal's role wins** over the query param (a token's scope can never be widened
by the URL). A role's scope is a **hard constraint** — it overrides any conflicting
client filter. The contract is unchanged for callers that pass no role.

| Role (`?role=`) | Sees |
|---|---|
| `operator` / `terminal_ops` / control room / `police` / *(none)* | all records |
| `customs` | pre-release pipeline only (`is_released=false`) |
| `driver` | released boxes only (`is_released=true`) |

```
GET /api/cargo?customs_status=CLEARED&is_released=true&limit=25&offset=0
→ 200 OK
X-Total-Count: 7
[ { "container_number": "MAEU6123458", "vessel_name": "MAERSK SEMBAWANG",
    "customs_status": "CLEARED", "yard_block": "A-01", "is_released": true,
    "vehicle_number": "MH04AB1234", "gate": "GATE-1", "camera_id": "CAM-ANPR-01",
    "eta": "2026-07-12T06:30:00Z", "created_at": "...", "updated_at": "..." } ]
```

### 2. Get one — `GET /api/cargo/{container_number}`
```
GET /api/cargo/MAEU6123458            → 200 OK  (CargoOut)
GET /api/cargo/MSCU7789010            → 404 { "detail": { "error": "not_found", ... } }
GET /api/cargo/NOTVALID               → 400 { "error": "validation_error", ... }
```
The path id is normalised (case/space-insensitive) before lookup.

### 3. Create — `POST /api/cargo`
Body = `CargoCreate` (`container_number` required + ISO-6346-valid; everything else optional; `customs_status` defaults `PENDING`, `is_released` defaults `false`).
```
POST /api/cargo
{ "container_number": "MAEU6123458", "vessel_name": "MAERSK SEMBAWANG",
  "customs_status": "PENDING", "yard_block": "A-01", "is_released": false,
  "vehicle_number": "MH04AB1234", "gate": "GATE-1", "camera_id": "CAM-ANPR-01",
  "eta": "2026-07-12T08:30:00Z", "eseal_status": "ACTIVE", "eseal_number": "ES-88213",
  "pre_document_status": "COMPLETED", "origin_stream": "UC-II" }
→ 201 Created  (CargoOut — includes the new fields, echoing null when omitted)
→ 409 Conflict { "detail": { "error": "duplicate_container", "container_number": "MAEU6123458" } }
→ 400 Bad Request (invalid ISO-6346 / bad enum / bad type / malformed JSON)
```

### 4. Update — `PUT /api/cargo/{container_number}`
Partial patch: send only the fields to change. The PK is immutable (path-derived).
```
PUT /api/cargo/MAEU6123458
{ "customs_status": "CLEARED", "is_released": true, "yard_block": "B-09" }
→ 200 OK  (full updated CargoOut; updated_at bumped by the DB trigger)
→ 404 Not Found
→ 400 Bad Request
```

### 5. Assign yard block — `PUT /api/cargo/{container_number}/yard-assignment`
Single-purpose write that parks a container in a yard block and persists it to
`jnpa.cargo.yard_block` (same column/record `PUT` patches; no separate yard
table). Returns a compact confirmation rather than the full record. `yard_block`
is required and format-checked (`<LETTERS>-<DIGITS>`, e.g. `A-01`; normalised to
upper-case) — a missing/malformed block is a 400.
```
PUT /api/cargo/GESU5123996/yard-assignment
{ "yard_block": "A-01" }
→ 200 OK { "container_number": "GESU5123996", "yard_block": "A-01", "status": "ASSIGNED" }
→ 404 Not Found { "detail": { "error": "not_found", "container_number": "GESU5123996" } }
→ 400 Bad Request (missing/invalid yard_block, or malformed container id)
```

### 6. Delete — `DELETE /api/cargo/{container_number}`
```
DELETE /api/cargo/MAEU6123458
→ 200 OK { "deleted": true, "container_number": "MAEU6123458" }
→ 404 Not Found
→ 400 Bad Request (malformed id)
```

### 7. Cargo events (notifications) — `GET /api/cargo/events`
Every cargo mutation appends to an **append-only lifecycle event log**. UC-2 polls
this endpoint (no backend/queue of its own) and advances a cursor to receive only
what is new. Events are returned **newest-first**.

**Event types** (`event` field):

| `event` | Emitted when | `payload` |
|---|---|---|
| `cargo.created` | a cargo record is created | `{ customs_status, is_released, origin_stream }` |
| `cargo.released` | `is_released` transitions `false → true` | `{ is_released: true }` |
| `cargo.yard_assigned` | `yard_block` is set/changed (incl. the yard-assignment endpoint) | `{ yard_block }` |
| `cargo.status_changed` | `customs_status` changes | `{ customs_status, previous_customs_status }` |
| `cargo.gate_movement` | `gate` is set/changed | `{ gate, previous_gate }` |
| `cargo.updated` | an update changed something with no more-specific event | `{}` |
| `cargo.deleted` | a cargo record is deleted | `{}` |

> A single `PUT` can emit several events (e.g. cleared **and** released **and**
> yarded). Event recording is best-effort and never blocks or fails the underlying
> cargo write.

**Query parameters**

| Param | Type | Default | Meaning |
|---|---|---|---|
| `container_number` | string | — | only events for this container (normalised) |
| `event` | string | — | only this event type, e.g. `cargo.released` |
| `since` | int (≥0) | — | only events with `id` greater than this cursor |
| `limit` | int (1–1000) | 100 | page size |
| `offset` | int (≥0) | 0 | page start |

**Response header:** `X-Cargo-Event-Cursor` — the largest `id` in the response;
persist it and pass it back as `?since=` on the next poll.

```
GET /api/cargo/events?container_number=GESU5123996
→ 200 OK
X-Cargo-Event-Cursor: 2
[ { "id": 2, "event": "cargo.released", "container_number": "GESU5123996",
    "timestamp": "2026-07-13T10:00:00Z", "payload": { "is_released": true } },
  { "id": 1, "event": "cargo.created", "container_number": "GESU5123996",
    "timestamp": "2026-07-13T09:55:00Z",
    "payload": { "customs_status": "PENDING", "is_released": false, "origin_stream": "UC-II" } } ]
```

**Suggested UC-2 poll loop:** keep the last `id` seen; call
`GET /api/cargo/events?since=<last_id>` on an interval; render each returned event
as a notification and advance `last_id` to `X-Cargo-Event-Cursor`.

---

## UC-II lifecycle → UC-III handover (migration 0023)

POC-3 owns a single, validated cargo lifecycle. Every record carries
`lifecycle_status` (on `CargoOut`), advanced only through the endpoints below —
forward-only, and mandatory gates (discharge, yard-assign, verify, release) cannot
be skipped. Illegal moves return **409**; unknown container **404**.

```
CREATED → VESSEL_DISCHARGED → YARD_ASSIGNED
        → [YARD_POSITION_ALLOCATED | REEFER_PLANNED | RAKE_ASSIGNED]  (optional)
        → SCAN_PENDING (scan-queue label) → VERIFIED → RELEASED
```

| Step | Call | Body | Result |
|---|---|---|---|
| Discharge | `POST /api/cargo/{cn}/discharge` | `{"vessel_name","discharge_time"}` | `→ VESSEL_DISCHARGED` (no auto yard) |
| Yard assign | `PUT /api/cargo/{cn}/yard-assignment` | `{"yard_block":"A-01"}` | `→ YARD_ASSIGNED` |
| Yard position | `POST /api/cargo/{cn}/yard-position` | `{"yard_block","row","slot","position","priority"}` | `→ YARD_POSITION_ALLOCATED` |
| Scan queue | `GET /api/cargo/scan-queue` | — | `[{container_number, yard_block, status:"SCAN_PENDING"}]` |
| Verify | `POST /api/cargo/{cn}/verify` | `{"verified":true,"remarks":"…"}` | `→ VERIFIED` |
| Release | `POST /api/cargo/{cn}/release` | `{"note":"…"}` (optional) | `→ RELEASED`; requires VERIFIED; duplicate → 409 |
| Handover list | `GET /api/cargo?status=RELEASED` | — | released cargo |
| Audit | `GET /api/cargo/{cn}/lifecycle` | — | append-only transition history |

Release response / `cargo.released` payload:
`{container_number, lifecycle_status:"RELEASED", yard_location, vehicle_details, status:"RELEASED"}`.

New lifecycle events (on the same `/api/cargo/events` log): `cargo.vessel_discharged`,
`cargo.yard_position_allocated`, `cargo.verified`, `cargo.reefer_planned`,
`cargo.rake_assigned`, `cargo.lifecycle_changed` (fires on every transition).

The legacy `PUT is_released=true` still works and stays lifecycle-consistent;
`POST /release` is the validated path.

---

## Status codes & error shape

| Code | When |
|---|---|
| 200 | successful GET / PUT / DELETE |
| 201 | successful POST (create) |
| 400 | validation failure — bad ISO-6346, bad enum, wrong type, missing PK, malformed JSON |
| 404 | container not found (GET/PUT/DELETE/lifecycle) |
| 409 | duplicate `container_number` on create, **or** an illegal lifecycle transition (e.g. release before verify, duplicate release) |
| 500 | unexpected server/DB error |

409 lifecycle body: `{ "detail": { "error": "illegal_transition", "container_number": "...", "current_status": "...", "attempted_status": "..." } }`.

400 body: `{ "error": "validation_error", "detail": <field errors> }`.
404/409 body: `{ "detail": { "error": "not_found" | "duplicate_container", "container_number": "..." } }`.

## Validation rules
- `container_number`: required on create; must pass ISO-6346 check-digit; normalised to upper-case, spaces stripped.
- `customs_status`: must be one of `PENDING | CLEARED | HELD | UNDER_INSPECTION`.
- `is_released`: boolean; `eta`: ISO-8601 datetime; string fields length-capped.
- `vehicle_number`: normalised (upper, de-spaced), nullable.
- `eseal_status`: one of `ACTIVE | ARMED | TAMPERED | REMOVED | NONE`, nullable.
- `pre_document_status`: one of `NOT_STARTED | PENDING | IN_PROGRESS | COMPLETED`, nullable.
- `eseal_number` / `origin_stream`: free text, trimmed (empty → null); `origin_stream` also accepts `originStream` on input.

## Interactive reference
- **Swagger UI:** `{BASE_URL}/docs` (tag: **cargo**)
- **OpenAPI JSON:** `{BASE_URL}/openapi.json` — generate a typed client if desired.

## Suggested POC-2 wiring
Add one block to your existing API wrapper (do **not** duplicate fetch logic):
`list → GET /api/cargo`, `details → GET /api/cargo/{id}`, `release → PUT /api/cargo/{id} {is_released:true}`,
`assign-yard → PUT /api/cargo/{id}/yard-assignment {yard_block:"A-01"}`,
`search → GET /api/cargo?container_number=...`, `scoped list → GET /api/cargo?role=<role>`,
`notifications → GET /api/cargo/events?since=<cursor>`. Cargo List → Details → Release →
Yard Assignment → Container Search → Journey → Notifications all read from these same
endpoints — POC-2 adds no Cargo backend or DB of its own. e-Seal, pre-document, and
origin-stream data ride on the same `CargoOut` record (fields above).
