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
| `created_at` | string (ISO-8601) | server-set |
| `updated_at` | string (ISO-8601) | server-set (DB trigger) |

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
| `limit` | int (1–1000) | 100 | page size |
| `offset` | int (≥0) | 0 | page start |

**Response header:** `X-Total-Count` — total rows matching the filters *before*
pagination (use it to render page controls).

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
  "eta": "2026-07-12T08:30:00Z" }
→ 201 Created  (CargoOut)
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

### 5. Delete — `DELETE /api/cargo/{container_number}`
```
DELETE /api/cargo/MAEU6123458
→ 200 OK { "deleted": true, "container_number": "MAEU6123458" }
→ 404 Not Found
→ 400 Bad Request (malformed id)
```

---

## Status codes & error shape

| Code | When |
|---|---|
| 200 | successful GET / PUT / DELETE |
| 201 | successful POST (create) |
| 400 | validation failure — bad ISO-6346, bad enum, wrong type, missing PK, malformed JSON |
| 404 | container not found (GET/PUT/DELETE) |
| 409 | duplicate `container_number` on create |
| 500 | unexpected server/DB error |

400 body: `{ "error": "validation_error", "detail": <field errors> }`.
404/409 body: `{ "detail": { "error": "not_found" | "duplicate_container", "container_number": "..." } }`.

## Validation rules
- `container_number`: required on create; must pass ISO-6346 check-digit; normalised to upper-case, spaces stripped.
- `customs_status`: must be one of `PENDING | CLEARED | HELD | UNDER_INSPECTION`.
- `is_released`: boolean; `eta`: ISO-8601 datetime; string fields length-capped.
- `vehicle_number`: normalised (upper, de-spaced), nullable.

## Interactive reference
- **Swagger UI:** `{BASE_URL}/docs` (tag: **cargo**)
- **OpenAPI JSON:** `{BASE_URL}/openapi.json` — generate a typed client if desired.

## Suggested POC-2 wiring
Add one block to your existing API wrapper (do **not** duplicate fetch logic):
`list → GET /api/cargo`, `details → GET /api/cargo/{id}`, `release → PUT /api/cargo/{id} {is_released:true}`,
`search → GET /api/cargo?container_number=...`. Cargo List → Details → Release →
Container Search → Journey all read from these same endpoints.
