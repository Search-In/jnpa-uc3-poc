# UC-3 — Peak Yard Utilisation & Truck Arrival Management

**Demo runbook + implementation notes.** Everything below runs against the
deployed environment with real database records and the existing APIs.

---

## 1. What was added (and what was reused)

### New (additive only)

| Layer | Artefact |
| --- | --- |
| Schema | `infra/postgres/v3/0144_uc3_yard_arrival_management.sql` — `core.yard_capacity_state`, `core.yard_capacity_event`, `core.truck_arrival_hold`, `core.truck_arrival_hold_event`, plus the four seeded terminal yards |
| Domain | `services/yard_capacity/model.py` — pure decisions (bands, operating ceiling, congestion pressure, parking choice, hold plan, release arithmetic) |
| Persistence | `services/yard_capacity/repository.py` |
| Orchestration | `services/yard_capacity/service.py` — every external effect injected, never imports the gateway |
| API | `gateway/routers/yard.py` → `/api/yard/capacity/*`, `/api/yard/arrivals/*` |
| Dashboard | `web/src/components/panels/YardArrivalPanel.tsx`, mounted on the **Congestion Rerouting** screen (`web/src/screens/DriverAdvisory.tsx`) |
| Driver PWA | two notification kinds in `mobile-pwa/src/lib/notify.ts` |
| Seed | `scripts/seed_uc3_yard_demo.sql` |
| Tests | `tests/test_yard_arrival_management.py` (22 tests, no DB required) |

### Reused, not duplicated

| Need | Existing thing used |
| --- | --- |
| Approaching trucks | `GET /api/trucks?state=AT_GATE_QUEUE` — the same call the Driver-Advisory console makes, returning **simulator trucks** in `devices` and **enrolled PWA driver vehicles** in `registered_devices` |
| Congestion alert | `services/congestion_alert.raise_congestion_alerts()` — the yard constraint raises the **same `TRAFFIC_CONGESTION`** alert row in `core.alert` + `core.notification`, with the same per-hour dedup |
| Parking | `GET /api/parking/availability` (RDS-backed: `core.parking_facility` / `core.parking_slot`) |
| Driver notification | `gateway.notifications.dispatch()` → WebSocket + WebPush + Firebase FCM |
| Live dashboard refresh | `state.ws.broadcast` (`yard_capacity`, `arrival_management` frames) |
| Yard capacity master | `core.yard_block` (migration 0130) is **read** when populated; this migration never writes to it |

No existing endpoint, table, column or RBAC rule was changed. `POST /api/yard/movements`
(the UC-II job surface) keeps its original audience — verified by a test.

---

## 2. Configuration (nothing hardcoded)

| Env var | Default | Meaning |
| --- | --- | --- |
| `YARD_HIGH_UTILIZATION_PCT` | `90` | at/above this the yard is flagged constrained |
| `YARD_CRITICAL_UTILIZATION_PCT` | `95` | red band **and** the operating ceiling the yard plans up to |
| `YARD_SLOTS_PER_TRUCK` | `2` | ground slots one laden trailer consumes |
| `YARD_RELEASE_RATE_SLOTS_PER_HOUR` | `0` | only feeds the estimated-wait figure; `0` ⇒ no estimate shown (never fabricated) |
| `YARD_PREFERRED_PARKING_FACILITY` | `PK-CPP` | authorised facility recommended first when it has room |
| `YARD_PRESSURE_ALERT_THRESHOLD` | `0.80` | pressure score above which the `TRAFFIC_CONGESTION` alert fires |

Per-yard overrides live in `core.yard_capacity_state.high_threshold_pct` /
`critical_threshold_pct` (NULL ⇒ use the env value).

### The operating-ceiling rule (why 95% actually stops trucks)

A terminal does not plan to fill its last ground slot. The yard plans up to
`capacity × YARD_CRITICAL_UTILIZATION_PCT`. So the board reports **two** numbers:

* `available_slots` — physical free space (what an operator sees on the ground);
* `headroom_slots` — bookable space below the ceiling (what admission uses).

At 95% of a 4 800-slot yard there are 240 slots physically free but **0 bookable**,
so every approaching truck is held. That is the headline demo behaviour, and it is
one configured number rather than a special case in code.

---

## 3. One-time setup

```bash
# 1) apply the schema (idempotent, ledgered)
make migrate MIGRATE_DSN="$DSN"          # applies 0144

# 2) seed 5 ACTIVE enrolled driver vehicles + reset the yards to NORMAL
psql "$PSQL_DSN" -f scripts/seed_uc3_yard_demo.sql
```

Verify the enrolled fleet (acceptance criterion 1):

```sql
SELECT p.device_id, d.name AS driver, v.status
FROM core.push_subscription p
JOIN core.vehicle v ON v.vehicle_id = p.device_id
LEFT JOIN core.driver_identity d ON d.vehicle_no_norm = p.device_id AND d.status='ACTIVE'
WHERE p.device_id LIKE 'TRK-9000%';
-- 5 rows: TRK-900001..TRK-900005, ACTIVE, each with a named driver
```

---

## 4. Demo sequence

Open the dashboard → **Congestion Rerouting**. The two new panels sit above the
existing gate-queue table.

### Step 1 — NORMAL yard

```bash
curl -s "$GW/api/yard/capacity/board" | jq '.yard'
```

Dashboard shows: utilisation ≈ **70.0 %**, total capacity **4800**, occupied **3360**,
available **1440**, status **NORMAL** (green). The capacity tile is labelled
*“declared · core.yard_capacity_state”* — the denominator is declared, and says so.

### Step 2 — bring trucks toward the gates

Simulator trucks (existing truck-sim control plane):

```bash
curl -s -XPOST "$TRUCKSIM/devices/inject" \
  -H 'content-type: application/json' \
  -d '{"count": 12, "tag": "UC3-YARD", "gate_id": "G-NSICT", "state": "AT_GATE_QUEUE"}'
```

Enrolled PWA vehicles are already available from the seed — or sign a real driver
in on the PWA with Vehicle ID `TRK-900001`.

Confirm both populations are visible:

```bash
curl -s "$GW/api/trucks?state=AT_GATE_QUEUE&limit=500" \
  | jq '{queued: .count, enrolled: .registered_count}'
```

### Step 3 — drive the yard to peak

**UI:** click **“Increase to peak (95%)”**.
**API equivalent:**

```bash
curl -s -XPOST "$GW/api/yard/capacity/JNPA-NSICT-YARD/adjust" \
  -H 'content-type: application/json' \
  -d '{"target_utilization_pct": 95, "event_type": "INCREASE", "reason": "demo peak"}'
```

Board flips to **95.0 % / CRITICAL** (red), occupied **4560**, available **240**,
`headroom_slots: 0`. An audit row lands in `core.yard_capacity_event` with
before/after/actor.

### Step 4 — detection, alert, holds, parking, driver push

The UI button in step 3 chains straight into this. Standalone:

```bash
curl -s -XPOST "$GW/api/yard/capacity/JNPA-NSICT-YARD/evaluate" -d '{}' \
  -H 'content-type: application/json' | jq
```

What happens, in order:

1. reads `AT_GATE_QUEUE` (simulator + enrolled PWA);
2. computes `congestion_pressure` (0.6 × utilisation + 0.4 × arrival surplus);
3. raises a **`TRAFFIC_CONGESTION`** alert through the existing service —
   `core.alert` (kind `TRAFFIC_CONGESTION`, segment `YARD-NSICT`) + `core.notification`,
   WS `alert` frame, per-driver WebPush/FCM, admin email if configured;
4. holds the surplus trucks (`core.truck_arrival_hold`, reason
   **“Yard capacity is currently constrained.”**), nearest trucks keep moving;
5. recommends the **Common Parking Plaza (`PK-CPP`)** from live parking availability —
   or explicitly reports *“No authorised parking facility currently has available
   capacity”* rather than naming a facility that cannot take the truck;
6. pushes each affected driver:
   > *“JNPA yard capacity is currently at 95%. Please proceed to Common Parking
   > Plaza (CPP) and wait until yard capacity becomes available.”*
   and audits the delivery result per truck in `core.truck_arrival_hold_event`.

`{"dry_run": true}` previews steps 1–2 and writes nothing.

### Step 5 — dashboard

**Truck Arrival Management** table shows every affected truck with:
Device/Vehicle · Driver · Current Gate · ETA · Yard Status (`WAITING` + reason) ·
Yard Utilisation · Recommended Parking (with free bays and wait estimate) ·
Source (`Simulator` / `PWA` chip, plus notified/pending) · Action.

### Step 6 — driver PWA

The signed-in driver gets a **parking** notification (toast in foreground, system
notification when backgrounded) and the advisory in the Inbox. Polling fallback:

```bash
curl -s "$GW/api/yard/arrivals/holds/TRK-900001" | jq '.hold'
```

A DRIVER token may read **only its own** device here; the list path is refused.

### Step 7 — capacity recovery

**UI:** click **“Release 5 containers”**.
**API equivalent** (5 containers × `YARD_SLOTS_PER_TRUCK`):

```bash
curl -s -XPOST "$GW/api/yard/capacity/JNPA-NSICT-YARD/release" \
  -H 'content-type: application/json' \
  -d '{"free_slots": 10, "reason": "release 5 containers"}'
```

Frees the slots (audited `RELEASE` event), releases as many held trucks as the
recovered headroom absorbs (oldest hold first), flips each to `RELEASED`, and
pushes the recovery advisory:

> *“Yard capacity is available (250 slots free, 94.8% utilised). You may now
> proceed to G-NSICT.”*

(Trucks whose gate was never measured — enrolled PWA devices — read “your
assigned terminal gate” instead of a gate id, rather than being shown a guess.)

Both panels and the gate-queue table refresh automatically (5 s poll + WS frames).

### Step 8 — full reset

**UI:** **“Reset to normal”** — takes the yard back to 70 % and releases every
outstanding hold. Or re-run `scripts/seed_uc3_yard_demo.sql`.

---

## 5. API reference

| Method | Path | Roles | Purpose |
| --- | --- | --- | --- |
| GET | `/api/yard/capacity/board` | control room, customs | utilisation board (all yards + audit tail) |
| GET | `/api/yard/capacity/{yard_id}/events` | control room, customs | occupancy audit trail |
| POST | `/api/yard/capacity/{yard_id}/adjust` | **control room only** | audited occupancy change |
| POST | `/api/yard/capacity/{yard_id}/evaluate` | **control room only** | detect → alert → hold → recommend → notify |
| POST | `/api/yard/capacity/{yard_id}/release` | **control room only** | free slots → release holds → notify |
| GET | `/api/yard/arrivals/holds` | control room, customs | arrival-management table |
| GET | `/api/yard/arrivals/holds/{device_id}` | + **DRIVER (own device)** | driver-facing hold view |

Every write sits under the fixed `/api/yard/capacity` prefix precisely so the RBAC
overlay pins it by prefix without a variable path segment defeating the match.

---

## 6. Audit trail

```sql
-- why did the yard change?
SELECT created_at, event_type, delta_slots, occupied_before, occupied_after,
       utilization_pct, status, reason, actor
FROM core.yard_capacity_event WHERE yard_id='JNPA-NSICT-YARD' ORDER BY id DESC LIMIT 10;

-- who was held, where were they sent, were they told?
SELECT device_id, source, status, yard_utilization_pct, recommended_facility_name,
       facility_available, estimated_wait_min, notified, release_notified, alert_id
FROM core.truck_arrival_hold ORDER BY held_at DESC LIMIT 20;

-- the per-truck delivery trail
SELECT device_id, action, detail, created_at
FROM core.truck_arrival_hold_event ORDER BY id DESC LIMIT 30;

-- the alert this was raised under
SELECT id, ts, kind, severity, payload FROM core.alert
WHERE kind='TRAFFIC_CONGESTION' ORDER BY ts DESC LIMIT 5;
```

---

## 7. Acceptance criteria

| # | Criterion | Where it is met |
| --- | --- | --- |
| 1 | 3–5 ACTIVE enrolled trucks participate | `scripts/seed_uc3_yard_demo.sql` seeds 5; they surface via the existing `registered_devices` rung |
| 2 | Yard reaches ~95 % | step 3 (`target_utilization_pct`), configurable |
| 3 | Trucks appear in `AT_GATE_QUEUE` | step 2, existing truck-sim + existing fleet list |
| 4 | Constraint detected automatically | `evaluate` — bands + pressure, both configured |
| 5 | Parking/wait recommendation | live `core.parking_facility`, CPP preferred; explicit “none available” when nothing has room |
| 6 | PWA driver receives the advisory | `notifications.dispatch` (WS + WebPush + FCM) + PWA copy |
| 7 | Dashboard shows affected trucks + status | Truck Arrival Management table |
| 8 | Releasing capacity changes status automatically | step 7 |
| 9 | Recovery/proceed notification | `YARD_CAPACITY_RELEASE` advisory |
| 10 | No UC-1/UC-2 regression | additive schema/routes; `/api/yard/movements` RBAC asserted unchanged in tests; full backend + web typecheck green |
| 11 | Exact calls / actions / data / steps | this document |

---

## 8. Honesty notes (what is declared vs measured)

* **Yard capacity is a declared figure.** No TOS ground-slot feed reaches this
  deployment. `capacity_declared: true` and `capacity_source` are in every payload,
  and the tile is labelled *declared*. When `core.yard_block` is loaded with a real
  block layout the service switches to `capacity_source: "core.yard_block"` with no
  code change.
* **Opening occupancy is declared**, and the seed says so in `source_note`.
* **Estimated wait is only shown when a release rate is configured** —
  `YARD_RELEASE_RATE_SLOTS_PER_HOUR=0` renders “—”, never a guess.
* **Parking is never invented.** If no authorised facility has room the response
  and the table say so explicitly.
* **A registration is not a queue measurement.** Enrolled PWA devices carry a null
  ETA and are held only after every measured truck has been.
