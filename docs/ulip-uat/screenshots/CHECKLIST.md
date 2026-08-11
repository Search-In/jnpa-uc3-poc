# Screenshot capture checklist

NLDSL asked for "screenshots of the created application clearly showing the
input request and the corresponding output/response". Each row below is one
slot in the document.

**How to use.** Capture the screenshot described, save it into this folder as
`<Id>.png` (any of .png/.jpg/.jpeg/.webp works; anything after the id in the
filename is ignored, so `SS-03-fastag-input.png` is fine too), then re-run:

    python3 docs/ulip-uat/build_uat_doc.py

The image is embedded in place of the placeholder and the index page updates
itself. Slots with no file yet stay as placeholders, so the document is always
in a submittable state.

**Before capturing:** set `ULIP_LIVE_ENABLED=1` with real credentials in
`.env.local`, and confirm the calls are reaching ULIP (not the fallback rungs)
— every response screenshot must show `source: ULIP`. Redact the bearer token
and the password in anything that shows a raw request.

| Id | Type | What to capture | Where |
|----|------|-----------------|-------|
| SS-01 | Input request | Login request issued by the application to ULIP | Application log or API console showing POST /user/login (password redacted) |
| SS-02 | Output / response | Login response — token issued | Response to POST /user/login with the token masked |
| SS-03 | Output / response | Live smoke-test output showing all 13 granted APIs called in one run with per-API status and latency | Live smoke-test run against ULIP staging |
| SS-04 | Input request | Application input — vehicle number entered on the FASTag screen before submitting | TC-FASTAG01-01 — FASTag Operations (/fastag) — Toll Transactions panel |
| SS-05 | Output / response | Application API response for POST /api/fastag/transactions showing the returned crossings | TC-FASTAG01-01 — FASTag Operations (/fastag) — Toll Transactions panel |
| SS-06 | Application screen | FASTag screen rendering the toll-crossing list with plaza, time, lane and vehicle class | TC-FASTAG01-01 — FASTag Operations (/fastag) — Toll Transactions panel |
| SS-07 | Output / response | Application response showing zero crossings for an unknown / inactive vehicle | TC-FASTAG01-02 — FASTag Operations (/fastag) — Toll Transactions panel |
| SS-08 | Application screen | FASTag screen empty state — 'no toll crossings in the last 72 hours' | TC-FASTAG01-02 — FASTag Operations (/fastag) — Toll Transactions panel |
| SS-09 | Output / response | Validation error returned by the application for a malformed vehicle number | TC-FASTAG01-03 — FASTag Operations (/fastag) — Toll Transactions panel |
| SS-10 | Input request | Application input — vehicle number entered in the Tag Status panel | TC-FASTAG02-01 — FASTag Operations (/fastag) — Tag Status tab |
| SS-11 | Output / response | Application API response for POST /api/fastag/tag-status listing every tag | TC-FASTAG02-01 — FASTag Operations (/fastag) — Tag Status tab |
| SS-12 | Application screen | FASTag screen rendering tag id, status, class and issuing bank | TC-FASTAG02-01 — FASTag Operations (/fastag) — Tag Status tab |
| SS-13 | Input request | Application input — tag id entered in the Tag Status panel | TC-FASTAG02-02 — FASTag Operations (/fastag) — Tag Status tab |
| SS-14 | Output / response | Application API response for the tag-id lookup | TC-FASTAG02-02 — FASTag Operations (/fastag) — Tag Status tab |
| SS-15 | Output / response | Application error response when both identifiers are supplied | TC-FASTAG02-03 — FASTag Operations (/fastag) — Tag Status tab |
| SS-16 | Input request | Application input — container number entered on the lifecycle screen | TC-LDB01-01 — UC3 Lifecycle / Follow-the-Box (/uc3-lifecycle) |
| SS-17 | Output / response | Application API response for GET /api/logistics/tracking/{container} with the normalised trail | TC-LDB01-01 — UC3 Lifecycle / Follow-the-Box (/uc3-lifecycle) |
| SS-18 | Application screen | Container movement trail rendered on the timeline and map | TC-LDB01-01 — UC3 Lifecycle / Follow-the-Box (/uc3-lifecycle) |
| SS-19 | Output / response | Application response for an unknown container — empty event list, explicit status | TC-LDB01-02 — UC3 Lifecycle / Follow-the-Box (/uc3-lifecycle) |
| SS-20 | Application screen | Lifecycle screen empty state for an untracked container | TC-LDB01-02 — UC3 Lifecycle / Follow-the-Box (/uc3-lifecycle) |
| SS-21 | Output / response | Validation error for a malformed container number | TC-LDB01-03 — UC3 Lifecycle / Follow-the-Box (/uc3-lifecycle) |
| SS-22 | Input request | Application input — vehicle number entered on the Vehicle Management screen | TC-VAHAN04-01 — Vehicle Management (/vehicles) — RC verification |
| SS-23 | Output / response | Application API response for GET /api/vahan/vehicle-intel/{plate} showing path=LIVE_PRIMARY, source=ULIP | TC-VAHAN04-01 — Vehicle Management (/vehicles) — RC verification |
| SS-24 | Application screen | Vehicle Management screen rendering the RC record, fitness validity and verification path | TC-VAHAN04-01 — Vehicle Management (/vehicles) — RC verification |
| SS-25 | Output / response | Application response for an unknown vehicle showing the degraded path | TC-VAHAN04-02 — Vehicle Management (/vehicles) — RC verification |
| SS-26 | Application screen | Vehicle Management screen showing the provisional/unverified state | TC-VAHAN04-02 — Vehicle Management (/vehicles) — RC verification |
| SS-27 | Application screen | Verification history listing the ULIP-sourced verification | TC-VAHAN04-03 — Vehicle Management (/vehicles) — RC verification |
| SS-28 | Input request | Application input — vehicle number submitted for RC lookup | TC-VAHAN01-01 — Vehicle Management (/vehicles) |
| SS-29 | Output / response | Application API response served through the VAHAN/01 retry, annotated with upstream_api | TC-VAHAN01-01 — Vehicle Management (/vehicles) |
| SS-30 | Application screen | Vehicle Management screen rendering the RC obtained via the XML API | TC-VAHAN01-01 — Vehicle Management (/vehicles) |
| SS-31 | Output / response | Application response showing both upstream attempts and the resulting path | TC-VAHAN01-02 — Vehicle Management (/vehicles) |
| SS-32 | Input request | Application input — chassis number entered for alternate-key lookup | TC-VAHAN02-01 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-33 | Output / response | Application API response for GET /api/vahan/chassis/{no} | TC-VAHAN02-01 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-34 | Application screen | Vehicle Management screen showing the vehicle resolved from its chassis number | TC-VAHAN02-01 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-35 | Output / response | Application not-found response for an unknown chassis number | TC-VAHAN02-02 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-36 | Input request | Application input — engine number entered for alternate-key lookup | TC-VAHAN03-01 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-37 | Output / response | Application API response for GET /api/vahan/engine/{no} | TC-VAHAN03-01 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-38 | Application screen | Vehicle Management screen showing the vehicle resolved from its engine number | TC-VAHAN03-01 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-39 | Output / response | Application not-found response for an unknown engine number | TC-VAHAN03-02 — Vehicle Management (/vehicles) — RC Lookup tab |
| SS-40 | Input request | Application input — DL number entered on the Driver Master screen | TC-SARATHI02-01 — Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments) |
| SS-41 | Output / response | Application API response for GET /api/vahan/dl/{dl} showing the licence record | TC-SARATHI02-01 — Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments) |
| SS-42 | Application screen | Driver Master screen rendering holder name, licence status, validity and classes of vehicle | TC-SARATHI02-01 — Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments) |
| SS-43 | Application screen | Driver record showing the transport validity date as the governing expiry | TC-SARATHI02-02 — Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments) |
| SS-44 | Output / response | Application response for an unknown DL number | TC-SARATHI02-03 — Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments) |
| SS-45 | Application screen | Driver Master screen showing the unverified state | TC-SARATHI02-03 — Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments) |
| SS-46 | Application screen | Driver screen showing a non-active licence blocked for port entry | TC-SARATHI02-04 — Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments) |
| SS-47 | Input request | Application input — DL number and date of birth entered during enrolment | TC-SARATHI01-01 — Driver Enrollments (/enrollments) |
| SS-48 | Output / response | Application API response for the DL + DOB lookup | TC-SARATHI01-01 — Driver Enrollments (/enrollments) |
| SS-49 | Application screen | Enrolment screen showing the corroborated driver record | TC-SARATHI01-01 — Driver Enrollments (/enrollments) |
| SS-50 | Output / response | Validation error for an incorrectly formatted date of birth | TC-SARATHI01-02 — Driver Enrollments (/enrollments) |
| SS-51 | Input request | Application request to refresh / list toll plazas for state id 27 | TC-GATISHAKTI04-01 — Reference data — consumed by FASTag toll-enroute and corridor analytics |
| SS-52 | Output / response | Application API response listing the toll-plaza master rows | TC-GATISHAKTI04-01 — Reference data — consumed by FASTag toll-enroute and corridor analytics |
| SS-53 | Output / response | Row counts before and after a repeated refresh, showing no duplication | TC-GATISHAKTI04-02 — Reference data — consumed by FASTag toll-enroute and corridor analytics |
| SS-54 | Output / response | Application response for an unknown state id | TC-GATISHAKTI04-03 — Reference data — consumed by FASTag toll-enroute and corridor analytics |
| SS-55 | Input request | Application request for highway detail by NH number | TC-GATISHAKTI01-01 — Reference data — corridor layer for road analytics |
| SS-56 | Output / response | Application API response with the highway reference rows | TC-GATISHAKTI01-01 — Reference data — corridor layer for road analytics |
| SS-57 | Output / response | Validation error for a malformed NH number | TC-GATISHAKTI01-02 — Reference data — corridor layer for road analytics |
| SS-58 | Input request | Application request for the state road network | TC-GATISHAKTI02-01 — Reference data — corridor layer for road analytics |
| SS-59 | Output / response | Application API response with the state road rows | TC-GATISHAKTI02-01 — Reference data — corridor layer for road analytics |
| SS-60 | Output / response | Application response for an unknown state id | TC-GATISHAKTI02-02 — Reference data — corridor layer for road analytics |
| SS-61 | Input request | Application request for named road points | TC-GATISHAKTI03-01 — Reference data — corridor labelling for road analytics |
| SS-62 | Output / response | Application API response listing road points with coordinates | TC-GATISHAKTI03-01 — Reference data — corridor labelling for road analytics |
| SS-63 | Output / response | Stored road point showing the converted numeric coordinates | TC-GATISHAKTI03-02 — Reference data — corridor labelling for road analytics |
| SS-64 | Application screen | System Health screen showing the ULIP integrations reporting live | Integration posture screens |
| SS-65 | Application screen | Integrations screen showing the ULIP configuration and last-call outcome | Integration posture screens |
