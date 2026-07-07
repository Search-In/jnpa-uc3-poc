# FASTag ULIP — Contract Verification Checklist (Step 5.2)

Fill this against the **authorised provider's API documentation**. For each field,
confirm the vendor's exact name/type, tick whether our implementation matches, and
note any gap. Our column names come from `infra/postgres/migrations/0001_fastag.sql`;
our DTO fields from `shared/jnpa_shared/fastag.py`; our mapping from
`services/fastag/mappers.py`.

Legend: ✅ matches · ⚠️ mismatch (fix) · ❓ vendor returns it but we don't map it · — n/a

---

## Pre-flight (transport)

| Item | Our value (env-driven) | Vendor spec | Match? |
|---|---|---|---|
| Base URL | `FASTAG_ULIP_URL` | | |
| Enroute path | `FASTAG_ULIP_ENROUTE_PATH` (default `/fastag/toll-enroute`) | | |
| Balance path | `FASTAG_ULIP_BALANCE_PATH` (default `/fastag/balance`) | | |
| Transaction path | `FASTAG_ULIP_TRANSACTION_PATH` (default `/fastag/transactions`) | | |
| HTTP method | POST (all three) | | |
| Auth scheme | `FASTAG_ULIP_AUTH_SCHEME` (bearer\|apikey\|none) | | |
| Auth header (apikey) | `FASTAG_ULIP_AUTH_HEADER` | | |
| Correlation header | `X-Correlation-ID` (we send) | | |
| Request content-type | `application/json` | | |

> ⚠️ The default paths above are **placeholders**. Confirm and set the env vars to
> the real paths — do not rely on the defaults.

---

## 1. Toll Enroute  →  `TollEnrouteResponse` / `jnpa.toll_enroute`

**Request** (we send, camelCase): `clientId`, `sourceState`, `sourceName`,
`destinationState`, `destinationName`, `vehicleType`. Confirm names + any required
extras (date, tag id, auth-scoped client id).

| Vendor response field | Our DTO field (alias) | DB column | Type | Match? |
|---|---|---|---|---|
| clientId | `client_id` | client_id | text | |
| sourceState | `source_state` | source_state | text | |
| sourceName | `source_name` | source_name | text | |
| destinationState | `destination_state` | destination_state | text | |
| destinationName | `destination_name` | destination_name | text | |
| vehicleType | `vehicle_type` | vehicle_type | text | |
| duration | `duration` | duration | text | |
| distance | `distance` | distance | **numeric(10,2)** | |
| tollPlazaDetails[] | `toll_plaza_details[]` | toll_plaza_details (jsonb) | array | |
| ├ tollPlazaName | `TollPlazaDetail.name` (alias `tollPlazaName`) | jsonb.name | text | |
| ├ fare / cost | `TollPlazaDetail.cost` (alias `fare`) | jsonb.cost (string, Decimal-preserving) | **Decimal** | |
| ├ latitude | `TollPlazaDetail.lat` (alias `latitude`) | jsonb.lat | float | |
| └ longitude | `TollPlazaDetail.lng` (alias `longitude`) | jsonb.lng | float | |

> If the vendor names plaza cost differently (e.g. `tollFare`, `amount`) update the
> `validation_alias` on `TollPlazaDetail.cost` in `shared/jnpa_shared/fastag.py`.

---

## 2. RC → FASTag Balance  →  `FastagBalanceResponse` / `jnpa.fastag_balance`

**Request:** `{ "rcNumber": "<RC>" }`. Confirm the exact key.

| Vendor response field | Our DTO field (alias) | DB column | Type | Match? |
|---|---|---|---|---|
| rcNumber | `rc_number` | rc_number (PK) | text | |
| tagId | `tag_id` | tag_id | text | |
| providerName | `provider_name` | provider_name | text | |
| providerCode | `provider_code` (int→str coerced) | provider_code | text | |
| customerName | `customer_name` | customer_name | text | |
| availableBalance | `available_balance` | available_balance | **numeric(10,2)** | |
| availableRechargeLimit | `available_recharge_limit` | available_recharge_limit | **numeric(10,2)** | |
| tagStatus | `tag_status` (normalized) | tag_status | text | |
| vehicleClass | `vehicle_class` | vehicle_class | text | |
| vehicleClassDesc | `vehicle_class_desc` | vehicle_class_desc | text | |
| modelName | `model_name` | model_name | text (nullable) | |

**tag_status normalization** (`services/fastag/mappers.py::_TAG_STATUS_MAP`) — confirm
the vendor's actual status vocabulary maps correctly:
`ACTIVE→Activated`, `LOW_BALANCE→LowBalance`, `BLACKLISTED→Blocked` (unknown values
pass through + are logged). Add any missing vendor status strings to the map.

---

## 3. RC → FASTag Transactions  →  `FastagTransactionBatch` / `jnpa.fastag_transactions`

**Request:** `{ "rcNumber": "<RC>" }` (+ optional `fromDate`/`toDate`). Confirm keys.

**Envelope fields:**

| Vendor field | Our DTO field | Persisted? | Match? |
|---|---|---|---|
| rcNumber | `FastagTransactionBatch.rc_number` | via each row | |
| tagId | `FastagTransactionBatch.tag_id` | via each row | |
| **bank_name** | ❓ NOT MODELLED | ❌ no column | **DECIDE** |
| **status** | ❓ NOT MODELLED | ❌ no column | **DECIDE** |
| transactions[] | `transactions[]` | yes | |

**Per-transaction fields:**

| Vendor field | Our DTO field (alias) | DB column | Type | Match? |
|---|---|---|---|---|
| seqNo | `seq_no` (int→str, UNIQUE) | seq_no (UNIQUE) | text | |
| transactionDateTime | `transaction_date_time` (→UTC) | transaction_date_time | timestamptz | |
| laneDirection | `lane_direction` | lane_direction | text | |
| tollPlazaName | `toll_plaza_name` | toll_plaza_name | text | |
| tollPlazaGeocode | `toll_plaza_geocode` (raw) + split→`geo_lat`/`geo_lng` | toll_plaza_geocode | text | |
| vehicleType | `vehicle_type` | vehicle_type | text | |

### ⚠️ Open decision — `bank_name` / `status` (Blocker B4)
These two envelope fields appear in the audited contract but are **not** modelled or
persisted today. They are currently captured by `extra="allow"` and **logged** as
`unmapped_fields` (not silently dropped). Decide from the vendor doc:

- **If the provider returns them:** extend `FastagTransactionBatch` (2 fields), add
  2 columns to `jnpa.fastag_transactions` (or a batch-level table), and map them in
  `map_fastag_transactions`. This is a small, additive change — no redesign.
- **If it does NOT return them:** do nothing. Record here: "provider does not return
  `bank_name`/`status`; implementation matches the contract." Do not add speculative
  schema.

**Finding:** ________________________________________________
