# ULIP production cutover runbook

Account `teqn_searchin_usr`, base `https://www.ulip.dpiit.gov.in/ulip/v1.0.0`,
13 granted APIs: `FASTAG/01-02`, `GATISHAKTI/01-04`, `LDB/01`, `SARATHI/01-02`,
`VAHAN/01-04` — the same set as staging, so **no code change is needed for the
grant**; `DEFAULT_API_PATHS` in `integrations/ulip/client.py` already covers it.

## Current status — 2026-08-12: LIVE, 13/13

NLDSL registered `65.2.212.121` on the production allowlist. `teqn_searchin_usr`
logs in, and **all thirteen granted APIs answer against production**, verified
end to end from the ULIP client through the gateway routes to the rendered UI.

| API | Production result |
|---|---|
| `FASTAG/01` | 4 real crossings for `CG07BC9186` (Pundag, Patrachauli, Anjan Dham, Lodam) with geocodes and lane directions |
| `FASTAG/02` | tag `34161FA8…` → `MAT735701`, status `Activated` |
| `LDB/01` | 13-leg trail for `TCLU8538808`; **10-20 s** response time |
| `VAHAN/04` | full RC for `UP32KH0320` |
| `VAHAN/01` | same vehicle, **deterministic** — the staging wrong-vehicle bug does NOT reproduce |
| `VAHAN/02` / `/03` | answer correctly; return nothing for identifiers that do not exist |
| `SARATHI/01` | `GJ04 20120005008` → issue 2012-03-07, Gujarat, GJ33, 3 classes |
| `SARATHI/02` | status `Active.`, valid to 2027-09-04 |
| `GATISHAKTI/01-04` | 33 / 13 / 994 / 59 rows; 1066 persisted for state 27 |

**Staging findings re-checked against production:**

- **VAHAN/01 is deterministic on production** — six consecutive calls all
  returned the plate asked for. `_matching_rc()` stays as a guard, but the
  staging defect is staging-only.
- **LDB/01 returns per-container trails on production** — `TCLU8538808` and
  `CXRU1145597` return genuinely different journeys. The staging "same trail for
  every container" behaviour does not reproduce. `_trail_is_about()` stays.
- **SARATHI/01 is NOT spacing-sensitive on production** — `GJ04 20120005008`
  and `GJ0420120005008` both resolve. Staging-only.
- **GATISHAKTI/04 uses the same live field names** as staging
  (`plaza_name` / `tollplazal` / `tollplaz_1`), not the PDF's sample.
- **VAHAN/04 still masks chassis and engine** (`MBLHAR087JHK*****`,
  `HA10AGJHK*****`), so VAHAN/02 and /03 remain undemonstrable from our own
  output and the `bd@nldsl.in` requirement note stands.

**Operational hazard worth knowing.** Engine numbers are sequential, so a
guessed or mistyped suffix returns a *real but different* vehicle:
`HA10AGJHK00001` is a Tamil Nadu vehicle, `…00002` is an Uttar Pradesh one. The
lookup cannot tell a typo from a legitimate query, so an engine-number result
must never be treated as identification on its own.

## Cutover — the validation to re-run after any config change

Production traffic must egress from `65.2.212.121`. On the app server that is
automatic. From a laptop, tunnel through it:

```bash
ssh -i ~/Downloads/jnpa3.pem -D 127.0.0.1:1081 -N ec2-user@65.2.212.121 &
curl --socks5-hostname 127.0.0.1:1081 https://api.ipify.org   # must print 65.2.212.121
```

**1. Validate all 13 APIs** (the single command that proves the credentials):

```bash
set -a && . ./.env.local && set +a
ULIP_PROXY=socks5://127.0.0.1:1081 python3 scripts/ulip_smoke.py --state-id 27
```

Exit `0` = every granted API answered. Exit `2` = still blocked on the
allowlist. A "not found" answer is a PASS — it proves the call was
authenticated, routed and understood.

**2. Confirm the live rungs.** All of these were verified against production on
2026-08-12:

| Route | Expected |
|---|---|
| `GET /api/vahan/rc/UP32KH0320` | `decision_path: LIVE_PRIMARY`, full RC |
| `GET /api/vahan/dl/GJ04%2020120005008?dob=1987-05-26` | `LIVE_PRIMARY`, `status: VALID` |
| `GET /api/logistics/tracking/TCLU8538808` | `status: LIVE`, `source: ULIP`, 13 events |
| `GET /api/ldb/container/TCLU8538808` | `source: ULIP` (**not** `MOCK`) |
| `GET /api/ldb/health` | `primary: ULIP`, `mode: LIVE` |
| `POST /api/fastag/tag-status {"tag_id":"34161FA8203286140F4064E0"}` | 1 tag, `Activated` |
| `POST /api/fastag/transactions {"rc_number":"CG07BC9186"}` | 4 crossings inserted |
| `POST /api/gatishakti/refresh?state_id=27` | `written: 1066` |
| `GET /api/gatishakti/toll-plazas?state_id=27` | 59 plazas, `source: ULIP_DB` |

**3. Seed the GatiShakti reference tables** for Maharashtra —
`POST /api/gatishakti/refresh?state_id=27`. Until this runs the toll-plaza and
road surfaces answer `path: FALLBACK`, `status: OFFLINE` with zero rows: they
read from the database, not from a live call. **This writes to the application
database, so get sign-off before running it against production RDS.**

**4. FASTAG/01 keeps only 72 hours.** Toll history must accumulate via the
poller (`ULIP_LIVE_ENABLED=1` starts it in `gateway/main.py`), not be fetched on
demand at demo time. The 4 crossings above are all that existed in the window.

**5. Two timeouts are load-bearing** and must both stay wider than LDB's 10-20 s:
`ULIP_LDB_TIMEOUT_S=30` on the gateway and `LDB_TIMEOUT_MS=35_000` in
`web/src/lib/api.ts`. The frontend's 15 s default silently failed container
tracking in the browser while the gateway was answering correctly.

## Credential handling

`ULIP_CLIENT_ID` / `ULIP_CLIENT_SECRET` live in **`.env.local` only**, which is
gitignored — never committed, never logged, never returned by a health
endpoint, never exposed to the browser as a `VITE_` variable. The production
password arrived over plaintext email; ask ULIP to rotate it once production is
confirmed working.
