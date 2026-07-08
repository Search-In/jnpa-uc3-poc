# RDS Single-Source-of-Truth — ER Diagram (Audit & Persistence Framework)

**Migration:** `infra/postgres/migrations/0003_audit_persistence.sql` (also in `infra/postgres/init.sql`).
**Schema:** `jnpa` (PostgreSQL / TimescaleDB / RDS). All timestamps `timestamptz` (UTC).

The five new tables are the cross-cutting audit/event spine. They reference the
existing operational tables by **soft keys** (vehicle plate, zone id, alert id,
driver id) rather than hard FKs, so an audit row always survives even if the
referenced operational row is later changed or purged (audit rows are immutable
history).

## Entity-relationship (new spine + touch-points)

```mermaid
erDiagram
    api_audit_log {
        bigserial id PK
        text      service_name
        text      endpoint
        text      method
        jsonb     request_payload
        jsonb     response_payload
        int       status_code
        numeric   latency_ms
        text      error
        text      transaction_id
        timestamptz created_at
    }
    digital_twin_events {
        bigserial id PK
        text      event_type
        text      vehicle_id
        text      driver_id
        jsonb     location
        jsonb     payload
        timestamptz created_at
    }
    notifications {
        bigserial id PK
        text      event_id
        text      channel
        text      receiver
        text      message
        text      delivery_status
        jsonb     provider_response
        timestamptz created_at
    }
    decision_audit {
        bigserial id PK
        text      request_id
        jsonb     input_data
        text      rule_executed
        text      decision
        text      action_taken
        timestamptz created_at
    }
    geofence_events {
        bigserial id PK
        text      vehicle_id
        text      zone_id
        timestamptz entry_time
        timestamptz exit_time
        text      violation_type
        text      action_taken
        timestamptz created_at
    }

    geofence_zones   ||--o{ geofence_events     : "zone_id (soft)"
    vehicle_master   ||--o{ digital_twin_events : "vehicle_id=plate (soft)"
    vehicle_master   ||--o{ geofence_events     : "vehicle_id=plate (soft)"
    alerts           ||--o{ digital_twin_events : "event_id=alert id (soft)"
    alerts           ||--o{ notifications       : "event_id (soft)"
    digital_twin_events ||--o{ notifications    : "event_id (soft)"
    anpr_reads       ||--o{ digital_twin_events : "ANPR_DETECTION mirror"
    drivers          ||--o{ digital_twin_events : "driver_id (soft)"
```

## Column contracts

### `api_audit_log` — every external API request/response
| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| service_name | text NOT NULL | `vahan` `sarathi` `fastag` `ulip` `eseal` `form13` `icegate` `weighbridge` `parking` `carbon` … |
| endpoint | text | `"<METHOD> <path>"` |
| method | text | GET/POST/… |
| request_payload | jsonb | truncated to 8 KB |
| response_payload | jsonb | truncated to 8 KB |
| status_code | int | NULL on transport error |
| latency_ms | numeric(10,2) | wall-clock of the call |
| error | text | set on non-2xx / exception |
| transaction_id | text | `X-Correlation-ID` / `X-Request-ID` |
| created_at | timestamptz | default now() |

**Indexes:** `(service_name, created_at DESC)`, `(transaction_id)`, `(created_at DESC)`.

### `digital_twin_events` — every operational / AI event
`event_type` ∈ `VEHICLE_DETECTED · ANPR_DETECTION · GEOFENCE_VIOLATION · PARKING_VIOLATION · ROUTE_DEVIATION · CONGESTION_ALERT · CUSTOMS_ALERT · AI_EVENT`.
`location` holds `{lat,lon,gate_id,segment_id,camera_id,zone_id}` (sparse).
**Indexes:** `(event_type, created_at DESC)`, `(vehicle_id, created_at DESC)`, `(driver_id, created_at DESC)`, `(created_at DESC)`.

### `notifications` — delivery audit trail
`channel` ∈ `webpush · sms · ws · email`. `delivery_status` CHECK ∈ `PENDING · SENT · DELIVERED · FAILED · SKIPPED · NO_SUBSCRIPTION`.
**Indexes:** `(created_at DESC)`, `(receiver, created_at DESC)`, `(delivery_status, created_at DESC)`, `(event_id)`.

### `decision_audit` — durable replacement for the in-memory DecisionRing
`rule_executed` = the orchestrated api/chain; `decision` = the chosen path (`LIVE_PRIMARY`/`CACHED`/`PROVISIONAL`/…); `action_taken` = `PRIMARY`/`FALLBACK`.
**Indexes:** `(created_at DESC)`, `(request_id)`, `(rule_executed, created_at DESC)`.

### `geofence_events` — zone enter/exit + dwell violations
`violation_type` ∈ `ENTER · EXIT · ILLEGAL_PARKING · ABANDONED`. `zone_id` soft-refs `geofence_zones.id`.
**Indexes:** `(vehicle_id, entry_time DESC)`, `(zone_id, entry_time DESC)`, `(created_at DESC)`.

> Vehicle-lookup and timestamp indexes are present on every table per the migration
> requirement, so plate-scoped audit queries and time-range analytics are index-served.
