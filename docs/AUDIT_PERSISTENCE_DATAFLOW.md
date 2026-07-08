# RDS Single-Source-of-Truth — Data Flow

How each subsystem's runtime output becomes a permanent RDS row. Every writer is
best-effort and fire-and-forget (`gateway/audit.py::spawn`) so persistence never
slows or breaks a live request/broadcast.

## 1. External API calls → `api_audit_log`  (automatic middleware)

```
Router (vahan/fastag/ulip/gate-data/parking/carbon/…)
    → state.http  (AuditingAsyncClient, gateway/audit_client.py)
        → super().send()  [real upstream call]
        → spawn(log_api_audit(service, endpoint, req, resp, status, latency, error, txn))
            → jnpa.api_audit_log   ✅ permanent
```
* One chokepoint: the gateway's shared `httpx.AsyncClient` is swapped for
  `AuditingAsyncClient`. **No per-router change** — every outbound hop is logged.
* Host→service map (`_HOST_SERVICE`) labels the row; `X-Audit-Service` header overrides.
* Health/metrics probes are skipped. Bodies truncated to 8 KB.
* Deepest hop (e.g. `vahan-live → Surepass`, `services/fastag → ULIP`) reuses the
  same `log_api_audit` helper once real endpoints are provisioned, so the
  government-facing request/response is captured too.

## 2. AI video analytics / ANPR → `anpr_reads` + `digital_twin_events`

```
Camera/clip → ai/anpr (YOLOv8 + PaddleOCR) → ingest/anpr → Kafka "anpr.reads"
    → gateway KafkaPump(topic=anpr.reads, persist=persist_anpr_read, broadcast=False)
        → jnpa.anpr_reads              ✅ permanent  (the table finally has a writer)
        → record_event(ANPR_DETECTION) → jnpa.digital_twin_events  ✅
```
This closes the audit's headline gap: `anpr_reads` previously had **no writer**.

## 3. Alerts (customs / geofence / congestion / AI) → events (+ geofence_events)

```
ai/anomaly · gate-data · provisional · violation-console → Kafka "alerts"
    → gateway KafkaPump(topic=alerts, ws_type=alert, persist=persist_alert_event)
        → WS broadcast (unchanged)
        → record_event(<mapped type>)  → jnpa.digital_twin_events   ✅
        → if geofence-family (ILLEGAL_PARKING/ABANDONED/GEOFENCE):
              record_geofence_event()  → jnpa.geofence_events        ✅
```
Alert kind → event_type map lives in `gateway/audit.py::_ALERT_KIND_TO_EVENT`.

## 4. Geo-fencing → `geofence_events`

```
GPS/anomaly producer → POST /api/geo/events  (gateway/routers/geo.py)
    → record_geofence_event(vehicle_id, zone_id, entry/exit, violation_type, action)
        → jnpa.geofence_events   ✅
GET /api/geo/events → read path (audit/analytics)
```
Zones (`geofence_zones`) already persisted via `PUT /api/zones`; this adds the
missing **event** stream (enter/exit/dwell) that the audit flagged.

## 5. Notifications → `notifications`

```
Alert / reroute → push.deliver(state, device_id, payload)   (gateway/routers/push.py)
    → attempt WebPush
    → spawn(log_notification(channel=webpush, event_id, receiver, message,
                             delivery_status=SENT|FAILED|NO_SUBSCRIPTION|SKIPPED, provider))
        → jnpa.notifications   ✅  (proves an advisory/challan notice was dispatched)
```
Same helper is the wiring point for SMS/e-mail providers when added.

## 6. Orchestration decisions → `decision_audit`

```
Any fallback chain → state.record_decision(api, decision_path, key, latency, detail)
    → in-memory DecisionRing (kept, for /api/debug/decisions)
    → spawn(record_decision_audit(request_id=key, rule_executed=api,
                                  decision=decision_path, action=PRIMARY|FALLBACK))
        → jnpa.decision_audit   ✅  (durable; survives restart)
```

## Boot sequence (schema top-up)

```
gateway lifespan startup
    → audit.configure(POSTGRES_DSN)
    → audit.ensure_audit_schema(dsn)   # idempotent CREATE ... IF NOT EXISTS
        (mirrors gateway/enforcement.py — an existing/RDS DB is topped up on boot,
         no manual migration step required; the .sql migration is the record + the
         standalone RDS apply path.)
```

## Guarantees

| Property | How |
|---|---|
| Never breaks a request | writers swallow all exceptions; `spawn()` fire-and-forget |
| Never blocks the hot path | DB write scheduled on the loop, not awaited inline |
| Survives restart | data lives in Postgres/RDS, independent of process memory |
| Idempotent schema | every DDL `IF NOT EXISTS`; safe to re-run; no data touched |
| Bounded rows | request/response bodies truncated to 8 KB |
