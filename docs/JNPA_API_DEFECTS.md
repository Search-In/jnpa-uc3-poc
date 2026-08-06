# JNPA Simulated Port-Data API v2.0 — Defect & Inconsistency Register

**Prepared by:** Keltron — JNPA Digital Twin PoC
**Against:** API Reference v2.0 (31-Jul-2026), `JNPA_DigitalTwin.postman_collection.json`, `API_EXAMPLES.html`, `keygen.py` / `KEY_GENERATION.md`
**Notice basis:** JNPA Notice 31-Jul-2026 — *"It has known defects, the ones observed must be reported."*

This register is the static half of our defect reporting. Its runtime half is
`core.api_defect_log`, populated automatically by the API client on every sync
(exported via `GET /api/integrations/jnpa/defects?format=md`). Each item below
is reproduced deterministically by our contract-faithful simulator
(`ingest/jnpa_portdata_sim`) and asserted by the contract test suite, so every
claim here is executable, not theoretical.

Severity: **S1** security/credential · **S2** data-integrity/interop ·
**S3** documentation/spec · **S4** tooling.

---

## Security & credential (S1)

| # | Title | Detail | Our handling |
|---|---|---|---|
| D0 | Live client key committed in distributed materials | `JNPA_DigitalTwin.postman_collection.json` ships `clientKey` `MzQ0MTMzODJiNTE1N2U2ZTY1MTY=`, which decodes to the key for `dtinfo@jnport.gov.in` (JNPA's own registered address). Running the collection unmodified authenticates as `cli_34413382b515` / org "JNPA". Contradicts NOTICE §6.1. | We never commit our key; it is backend-only env (`JNPA_PORTDATA_CLIENT_KEY`), never a `VITE_` var. |
| D0b | Undocumented `admin:read` scope granted | Docs say the scope set is `groups:read`,`files:read`; the captured token carries a third `admin:read`. No admin endpoint is documented. | Client logs a `D0B_ADMIN_SCOPE_GRANTED` observation on every token that carries it. |
| D0c | Client key has no secret component | `clientKey = base64(sha256(lower(email))[:20])` — an unsalted, unkeyed function of a guessable identifier. Anyone with a participant's registered email derives their key offline. Relevant to D.1.3 (OWASP API security). | Reported; not exploitable by us, flagged for JNPA remediation. |
| D2 | `recordId`/`fileRef` leak a sequential internal id | Docs call the fileRef "an opaque HMAC-signed token — no path, no row id, no filename". In fact `fileRef` = `recordId` with the prefix swapped, and the first 6 base64 chars decode to a plain decimal integer (6243–6265 observed), globally sequential across groups → total volume is enumerable. | Client treats ids as opaque; sim reproduces the id shape for tests. |
| D2b | `Content-Disposition` leaks the internal filename | Same "no filename" claim is contradicted by `filename="CHPOI03_IGM_1197294_...xml"`, embedding the IGM number. | We *rely* on this filename for parser routing (the useful side of the defect) while noting the disclosure. |

## Data-integrity & interoperability (S2)

| # | Title | Detail | Our handling |
|---|---|---|---|
| D9 | Report groups return a different, undocumented envelope | `berthing-reports` / `daily-reports` return only 5 of the 9 documented envelope fields — no `order`, `matched`, `hasMore`, `nextCursor`. No defined way to detect the last page. | Report envelope fields all Optional; `get_report` never assumes pagination; a truncation-risk observation fires when `count == limit`. |
| D10 | Report record schema entirely unspecified | Every documented report example returns 0 items (all example dates are future of the capture's `asOf`), so no field of a report record is specified anywhere. | Land-raw-then-map: the raw payload is snapshotted before mapping; mappers degrade to RAW_ONLY and log the observed key set. |
| D13 | Cursor shares the fileRef namespace | `nextCursor` equals the last item's `fileRef`; `GET /v2/files/{nextCursor}` downloads that record's file, and the same token yields 400 (as cursor) vs 404 (as path). Boundary-duplication risk if the cursor is inclusive. | Cursor passed verbatim/opaque; recordId dedup absorbs any boundary duplication. |
| D13b | No tie-break sort key | Multiple records share one `publishedAt` (4-way ties observed). With exclusive `since` over a non-unique sort key, resuming at the watermark **silently skips** tied records. | `since = watermark − 1s` + `ON CONFLICT (record_id) DO NOTHING`. Proven by test: naive resume loses 4 records; our defense recovers all, duplicates none. |
| D19 | Three incompatible messageType vocabularies | `type=` filter accepts 8 codes (IGM/OOC/SMTP/FORM11/RAKE/DBR/LEO/E-DO); records carry a different set (CHPOI03, LOOP, voyage-registration, gate-open-report, …); the catalogue lists plain-language names. No mapping published → most received types are unfilterable. | We sync by time (`since`), not `type`; routing keys off filename + record fields, not the filter vocabulary. |
| D22 | `+05:30` sent unencoded in JNPA's own examples | The Postman `lastSeen` default and the HTML examples interpolate `+05:30` raw into the query; a raw `+` decodes to a space server-side, corrupting the offset (only masked because the sample queries returned 0 rows). | Client sends all timestamps via httpx params → `%2B05:30` on the wire. Test asserts no raw `+` ever leaves. |
| D36 | Fill-forward `vesselCall` artefact | Six records across two groups share one identical `vesselCall` + `publishedAt` — a default, not real linkage. `summary` even contradicts `vesselCall` on the `LOOP` record. | `vesselCall` stored but flagged unreliable as a join key; not used for entity resolution without validation. |
| D3 | `requestId` documented but never returned | NOTICE §7 says quote the `requestId` in support requests; it appears in no body/header. | Support workflow adapted to quote the client id + our `api_ingest_run.id` instead. |
| D5 | `RateLimit-Remaining` omitted on errors and 304s | The header is absent on 401/404/429/304 even though they consume quota. | Client treats absence as normal; a client-side sliding-window budget enforces the ceiling regardless. |
| D6 | No `RateLimit-Reset` / `Retry-After` on 429 | Correct backoff is not computable from the response. | Blind 60 s + jitter backoff on 429. |
| D7 | `Cache-Control: no-store` documented but absent, and self-contradictory | The PDF both mandates `no-store` and promotes ETag/If-None-Match revalidation — mutually exclusive; and the header is absent from every capture. | We use ETag/If-None-Match (the useful half); no reliance on `no-store`. |
| D12 | `bathymetry` contradictorily specified | Declared `static` / "not served", yet appears in the catalogue with a working records link that returns 200/empty. | Treated as static: skipped by sync, sample-pack dump remains its source. |

## Documentation & spec (S3)

| # | Title | Detail |
|---|---|---|
| D1 | Base-URL definition conflict | PDF §1.1 defines the base *with* `/v2`; PDF §8 then lists `/v2/...` paths → literal concatenation yields `/v2/v2/...`. Postman/HTML/README define the base *without* `/v2`. (Client normalises + logs `D1_BASE_URL_V2`.) |
| D11 | Group-catalogue object schema absent from the spec | The per-group fields (`description`, `coverage`, `messageTypes`, `note`, `links`) exist only in captures, and the shape is non-uniform (nlp-marine has coverage+messageTypes, no note; bathymetry has note, neither of the others). |
| D14 | PDF fileRef examples are fabricated | PDF refs are 15–16 chars and fail to base64-decode to an integer; real refs are 18 chars and always decode — the PDF examples were hand-written, not captured. |
| D15 | PDF §4.3 internally inconsistent | Declares `"count": 2` with a one-element `items` array. |
| D16 | Same query, two `matched` values | `type=IGM` shows `matched: 102` (PDF) vs `29` (HTML). |
| D17 | Cover "Records 6,625" vs largest observed id 6265 | Likely a 6,625↔6,265 transposition. |
| D18 | Illustration timestamps incoherent | §2.2 token `expiresAt` predates §4.3 `asOf` by ~4 h — not one run. |
| D20 | messageType casing inconsistent within one response | `vessel-Profile` beside `voyage-registration`, `LOOP`, `CHPOI03`. |
| D21 | Near-duplicate type taxonomy | Both "Expected time of arrival" and "…(report)"; both "Vessel profile" and "…(report)". |
| D23 | `until` and `cursor` never exercised | `until` only in the parameter table; `cursor` only as a disabled Postman param. |
| D24 | `date`/`terminal` scoping under-specified | Behavior on an indexed group is unstated; HTML shows `from`/`to` working on a report group the PDF framing doesn't anticipate. |
| D25 | Terminal codes never enumerated | Postman uses BMCT, HTML uses APMT; no authoritative list. |
| D26 | `bad_request` (400) / `payload_too_large` (413) unreachable | The only body-accepting endpoint is the token POST; no size limit stated. |
| D27 | Postman "has a filename" fails on 304 | The 304 carries no `Content-Disposition`, but the request asserts a filename for "200 or 304". (Encoded as a strict `xfail` in our suite.) |
| D28 | …and never sends `If-None-Match` | So 304 is unreachable as shipped; the "200 or 304" assertion is untestable. |
| D29 | Invalid chai assertion | `expect(string).to.be.within(a,b)` — `within` expects numbers; only survives because the array is empty. |
| D30 | Assertions pass vacuously on empty data | `from/to` beyond the data window returns 0 items, so "inside the range" / "newest first" iterate nothing. |
| D31 | Shipped Postman params unusable | Disabled `from=2026-08-01` postdates coverage; `type=IGM` templated into groups where IGM cannot occur. |
| D32 | bathymetry request silently special-cased | Omits the fileRef assertions the other 9 indexed requests carry, unexplained. |
| D33 | Weak coverage | `/v2/groups` asserted only as `length>0`, never `==13`; `GET /` untested; no test for bad_cursor/403/429/limit bounds/until. |
| D34 | Negative-auth intent partly violated | The captured messages distinguish "Client key not recognised" vs "Bearer token required", against the stated "must not disclose". |
| D35 | `summary` contradicts `vesselCall` | LOOP record: `vesselCall` S0800 but summary names call S1083. |
| D37 | `containerCount: 0` on a container-cohort record | The LOOP record's summary is about a box cohort, yet containerCount is 0. |
| D38 | Authoring commentary leaks into `summary` | The gate-open-report summary reads "This is the ONLY artefact in the whole dataset that carries the cut-off…". |
| D39 | Dash inconsistency in `summary` | ASCII hyphen (PDF) vs em dash (HTML) — breaks exact-string parsing. |
| D40 | "Pilot memo" appears in two groups | Listed in nlp-marine.messageTypes and described under port-craft-pilot → double-count risk. |
| D44 | HTML numbers are a 6-second cold-start snapshot | `uptimeSeconds: 6`; treat all HTML figures as one cold start, not steady state. |
| D45 | Simulation-clock vs wall-clock unstated | Whether coverage advances with real time is never stated — verify `asOf`/`coverage` on first live call before trusting the documented date windows. |

## Tooling (S4)

| # | Title | Detail |
|---|---|---|
| D41 | `keygen.py --clients` incompatible with documented `clients.json` | The code iterates a JSON array (`r["email"]`); the docs show a single object → `TypeError`. |
| D42 | Shell keygen recipe broken on macOS / mismatches spec | Uses `sha256sum` (macOS has `shasum -a 256`) and doesn't `.strip()` whitespace, so a padded address yields a different key than `keygen.py`. |
| D43 | `keygen.py` crashes on all-duplicate/blank input | `max(len(...) for r in rows)` raises `ValueError` on empty `rows`. |

---

*45 items. Reproduced by `ingest/jnpa_portdata_sim` and asserted in
`tests/test_jnpa_portdata_*.py`. Runtime observations accrue to
`core.api_defect_log` and are exported alongside this register for submission.*
