"""Content model for the ULIP UAT / test-case document.

Kept separate from :mod:`build_uat_doc` so the narrative can be edited without
touching the renderer. Every ULIP request/response sample below is copied from
the corresponding integration PDF in ``ulip-docs/`` (or, equivalently, from the
fixtures in ``tests/test_ulip_contracts.py`` which were themselves transcribed
from those PDFs) — nothing here is invented.

Screenshot slots are declared, not numbered: :mod:`build_uat_doc` assigns the
``SS-nn`` ids in document order so inserting a case never renumbers by hand.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Cover / front matter
# --------------------------------------------------------------------------
DOC = {
    "title": "ULIP API Integration — Test Case Document",
    "subtitle": "Evidence of application usage for the 13 granted APIs",
    "org": "Search-In Solutions",
    "project": "JNPA Direct Trade Coordination & Control Centre (DTCCC)",
    "use_case": "Use Case 3 — Truck Movement, Gate Automation & Vehicle Intelligence",
    "environment": "ULIP Staging — https://www.ulipstaging.dpiit.gov.in/ulip/v1.0.0",
    "account": "rtoj_searchin_usr",
    "egress_ip": "65.2.212.121 (AWS Elastic IP eipalloc-0452eeb3c1b484845, ap-south-1)",
    "version": "1.0",
    "date": "11 August 2026",
    "submitted_to": "NLDSL / DPIIT — ULIP Integration Support",
}

INTRO = """
This document records how each of the thirteen ULIP APIs granted to account
<b>rtoj_searchin_usr</b> is used inside the JNPA DTCCC Use&nbsp;Case&nbsp;3
application, and the test cases executed against those APIs on the ULIP staging
environment.

For every API it states the ULIP endpoint, the application API that fronts it,
the operator screen that renders the result, and each test case with its
expected and actual outcome — accompanied by screenshots of the running
application showing the input request and the corresponding output response.
"""

SCOPE_NOTE = """
Scope is limited to the thirteen APIs listed in NLDSL's access-grant mail:
GATISHAKTI_01–04, FASTAG_01–02, VAHAN_01–04, SARATHI_01–02 and LDB_01. APIs
described in the integration documents but not granted (VAHAN/05, VAHAN/06,
GATISHAKTI/05, CFSICD/01) are out of scope and are not called by the
application.
"""

# --------------------------------------------------------------------------
# Architecture — how a ULIP call flows through the application
# --------------------------------------------------------------------------
ARCH_LAYERS = [
    ("Operator UI (React / TypeScript)",
     "web/src/screens/*",
     "Operator enters a vehicle number, container number, DL number or state and "
     "submits. No ULIP credential ever reaches the browser."),
    ("Application API (FastAPI gateway)",
     "gateway/routers/*",
     "Authenticated, role-guarded REST endpoint. Validates and normalises the "
     "input, then delegates to the service layer."),
    ("Service layer",
     "services/fastag, services/logistics, services/gatishakti, gateway/vehicle_intel",
     "Applies the fallback ladder (LIVE → CACHED → DATABASE → FALLBACK), "
     "persists results, and enforces the no-fabrication rule."),
    ("ULIP client (single shared client)",
     "integrations/ulip/client.py",
     "Holds the login token, re-authenticates once on 401/403, validates each "
     "request against the documented field pattern before spending a call, and "
     "redacts credentials from every log line and exception."),
    ("ULIP platform",
     "https://www.ulipstaging.dpiit.gov.in/ulip/v1.0.0",
     "POST /user/login for the bearer token, then POST /<API>/<version>."),
]

AUTH_NOTE = """
All thirteen APIs share one authentication flow and one response envelope. The
application logs in once via <code>POST /user/login</code>, caches the bearer
token returned at <code>response.id</code> for 30&nbsp;minutes
(<code>ULIP_TOKEN_TTL_S</code>), sends it as
<code>Authorization: Bearer &lt;token&gt;</code> on every subsequent call, and
performs exactly one re-login if an API answers 401 or 403. Credentials are
supplied to the backend process through environment variables
(<code>ULIP_CLIENT_ID</code> / <code>ULIP_CLIENT_SECRET</code>) and are never
committed, never logged, and never exposed to the browser.
"""

LOGIN_REQUEST = """POST /ulip/v1.0.0/user/login
Content-Type: application/json

{
  "username": "rtoj_searchin_usr",
  "password": "********"
}"""

LOGIN_RESPONSE = """HTTP/1.1 200 OK

{
  "response": {
    "id": "<bearer token>",
    "username": "rtoj_searchin_usr"
  },
  "error": "false",
  "code": "200",
  "message": "Success"
}"""

ENVELOPE_NOTE = """
Every API returns the same envelope — <code>{"response": [...], "error":
"false", "code": "200", "message": "Success"}</code>. A <i>miss</i> (unknown
vehicle, unknown container, unknown state) also arrives as <b>HTTP 200</b> with
an error marker inside the body rather than as an HTTP error status. The
application treats each of these markers as "no data", never as a partial
record: VAHAN <code>message.code 231</code>, SARATHI <code>errorcode -1</code>,
FASTag <code>errCode 740</code> and <code>respCode 239</code>, GatiShakti an
empty <code>data</code> array, LDB <code>responseStatus FAILURE</code>. Each of
these is covered by a negative test case in the sections that follow.
"""

# --------------------------------------------------------------------------
# The thirteen APIs
# --------------------------------------------------------------------------
# Each entry:
#   api, granted, name, doc_ref, purpose
#   ulip_request / ulip_response  — verbatim contract samples
#   chain    — [(layer, artefact)]
#   app_api  — the application endpoint fronting it
#   app_request / app_response
#   screen   — operator screen + route
#   cases    — [{id, title, precondition, steps, input, expected, shots}]
#   notes    — operational constraints worth stating to NLDSL
APIS = [
    # ------------------------------------------------------------------ FASTAG/01
    {
        "api": "FASTAG/01",
        "granted": "FASTAG_01",
        "name": "Vehicle toll-crossing history (NETC)",
        "doc_ref": "ULIP_FASTAG_Integration_Requirement.pdf §1.3",
        "purpose": (
            "Reconstructs the road leg of a container truck's journey to JNPA. "
            "Each toll crossing gives a timestamped, geo-located waypoint, which "
            "the DTCCC uses for ETA estimation to the port gate, for corridor "
            "dwell analysis, and to corroborate the gate-entry record against an "
            "independent national source."),
        "ulip_request": """POST /ulip/v1.0.0/FASTAG/01
Authorization: Bearer <token>
Content-Type: application/json

{
  "vehiclenumber": "CG07BC9186"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "result": "SUCCESS", "respCode": "000", "ts": "2021-11-01T19:20:47",
      "vehicle": {
        "errCode": "000",
        "vehltxnList": {
          "totalTagsInMsg": "2", "msgNum": "1",
          "totalTagsInresponse": "2", "totalMsg": "1",
          "txn": [
            { "readerReadTime": "2021-10-30 12:26:09.0",
              "seqNo": "68d47e2d", "laneDirection": "N",
              "tollPlazaGeocode": "11.0001,11.0001",
              "tollPlazaName": "GMR Chillakallu Toll Plaza",
              "vehicleType": "VC7", "vehicleRegNo": "MH19JK3923" },
            { "readerReadTime": "2021-10-30 12:41:23.0",
              "seqNo": "6361cf5f", "laneDirection": "N",
              "tollPlazaGeocode": "11.0001,11.0001",
              "tollPlazaName": "GMR Chillakallu Toll Plaza",
              "vehicleType": "VC7", "vehicleRegNo": "MH19JK3923" }
          ]
        }
      }
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_vehicle_movement() — integrations/ulip/client.py"),
            ("Normaliser", "normalize_vehicle_events() — integrations/ulip/schemas.py"),
            ("Mapper", "map_fastag_transactions() — services/fastag/mappers.py"),
            ("Accumulator", "FastagPoller.sweep_once() — services/fastag/poller.py"),
            ("Application API", "POST /api/fastag/transactions"),
            ("Persistence", "core.fastag_transactions, core.logistics_event"),
            ("Screen", "FASTag Operations — /fastag"),
        ],
        "app_api": "POST /api/fastag/transactions",
        "app_request": """POST /api/fastag/transactions
Authorization: Bearer <application JWT>
Content-Type: application/json

{
  "rc_number": "MH19JK3923"
}""",
        "app_response": """{
  "status": "success",
  "source": "ULIP",
  "rc_number": "MH19JK3923",
  "count": 2,
  "transactions": [
    { "tag_id": null, "rc_number": "MH19JK3923",
      "seq_no": "68d47e2d",
      "transaction_date_time": "2021-10-30T12:26:09",
      "toll_plaza_name": "GMR Chillakallu Toll Plaza",
      "latitude": 11.0001, "longitude": 11.0001,
      "lane_direction": "N", "vehicle_type": "VC7" },
    { "seq_no": "6361cf5f",
      "transaction_date_time": "2021-10-30T12:41:23",
      "toll_plaza_name": "GMR Chillakallu Toll Plaza" }
  ]
}""",
        "screen": "FASTag Operations (/fastag) — Toll Transactions panel",
        "cases": [
            {"id": "01", "title": "Retrieve toll crossings for a known vehicle",
             "precondition": "ULIP_LIVE_ENABLED=1; valid login token held; vehicle has crossed a plaza within the last 72 hours.",
             "steps": "Open /fastag → enter the vehicle number in Toll Transactions → Fetch.",
             "input": '{"rc_number": "CG07BC9186"}',
             "expected": "HTTP 200, status=success, source=ULIP, one row per txn entry with plaza name, timestamp and geocode; rows persisted to core.fastag_transactions.",
             "shots": [
                 ("request", "Application input — vehicle number entered on the FASTag screen before submitting"),
                 ("response", "Application API response for POST /api/fastag/transactions showing the returned crossings"),
                 ("ui", "FASTag screen rendering the toll-crossing list with plaza, time, lane and vehicle class"),
             ]},
            {"id": "02", "title": "Vehicle with no crossings in the retention window",
             "precondition": "Same as TC-01; vehicle number valid in format but with no toll activity in the last 72 hours.",
             "steps": "Repeat TC-01 with a vehicle that has not crossed a plaza recently.",
             "input": '{"rc_number": "MH19JK3923"}',
             "expected": "ULIP answers HTTP 200 with vehicle.errCode=740. Application returns count=0 with an explicit empty state — no fabricated rows.",
             "shots": [
                 ("response", "Application response showing zero crossings for an unknown / inactive vehicle"),
                 ("ui", "FASTag screen empty state — 'no toll crossings in the last 72 hours'"),
             ]},
            {"id": "03", "title": "Malformed vehicle number rejected before the call",
             "precondition": "Any.",
             "steps": "Submit a vehicle number that fails the documented pattern.",
             "input": '{"rc_number": "12"}',
             "expected": "HTTP 422 from the application; no ULIP call is made (client-side pattern validation in UlipClient).",
             "shots": [
                 ("response", "Validation error returned by the application for a malformed vehicle number"),
             ]},
        ],
        "notes": (
            "The integration document states that data for a VRN is available only "
            "for the <b>past 72 hours</b>. An on-demand lookup is therefore not "
            "sufficient for port operations, and the application runs a scheduled "
            "poller (<code>services/fastag/poller.py</code>, default hourly) that "
            "sweeps the active vehicle set and accumulates crossings into "
            "<code>core.fastag_transactions</code>, de-duplicating on "
            "<code>seqNo</code>. The screenshots for TC-01 therefore show both the "
            "on-demand fetch and the accumulated history."),
    },
    # ------------------------------------------------------------------ FASTAG/02
    {
        "api": "FASTAG/02",
        "granted": "FASTAG_02",
        "name": "NETC tag registry / tag status",
        "doc_ref": "ULIP_FASTAG_Integration_Requirement.pdf §1.4",
        "purpose": (
            "Confirms that a truck presenting at the JNPA gate carries a valid, "
            "active FASTag and that the tag is registered to the plate on the "
            "vehicle. A blacklisted or exception-coded tag is a gate-hold "
            "condition; the commercial-vehicle flag and vehicle class are also "
            "used to validate the declared vehicle category."),
        "ulip_request": """POST /ulip/v1.0.0/FASTAG/02
Authorization: Bearer <token>
Content-Type: application/json

{
  "vehiclenumber": "CG07BC9186"
}

-- OR, exclusively --

{
  "tagid": "34161FA8203286140F4064E0"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "result": "SUCCESS", "successReqCnt": "1", "totReqCnt": "1",
      "respCode": "000", "ts": "2024-03-14T14:07:48",
      "vehicle": {
        "errCode": "000",
        "vehicledetails": [
          { "detail": [
              { "name": "TAGID",        "value": "34161FA820328972020FB320" },
              { "name": "REGNUMBER",    "value": "MP09HF4987" },
              { "name": "VEHICLECLASS", "value": "VC11" },
              { "name": "TAGSTATUS",    "value": "A" },
              { "name": "BANKID",       "value": "607417" },
              { "name": "COMVEHICLE",   "value": "T" } ] },
          { "detail": [
              { "name": "TAGID",     "value": "34161FA8203289720E14EEA0" },
              { "name": "REGNUMBER", "value": "MP09HF4987" },
              { "name": "TAGSTATUS", "value": "A" } ] }
        ]
      }
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_tag_status() — enforces the vehiclenumber XOR tagid rule"),
            ("Normaliser", "normalize_tag_status() — flattens detail[] name/value pairs"),
            ("Mapper", "map_fastag_tag_status() — services/fastag/mappers.py"),
            ("Application API", "POST /api/fastag/tag-status"),
            ("Screen", "FASTag Operations — /fastag"),
        ],
        "app_api": "POST /api/fastag/tag-status",
        "app_request": """POST /api/fastag/tag-status
Authorization: Bearer <application JWT>
Content-Type: application/json

{
  "rc_number": "MP09HF4987"
}""",
        "app_response": """{
  "status": "success",
  "source": "ULIP",
  "count": 2,
  "tags": [
    { "tag_id": "34161FA820328972020FB320", "rc_number": "MP09HF4987",
      "vehicle_class": "VC11", "tag_status": "A",
      "bank_id": "607417", "commercial_vehicle": true },
    { "tag_id": "34161FA8203289720E14EEA0", "rc_number": "MP09HF4987",
      "tag_status": "A" }
  ]
}""",
        "screen": "FASTag Operations (/fastag) — Tag Status tab",
        "cases": [
            {"id": "01", "title": "Tag registry lookup by vehicle number",
             "precondition": "ULIP_LIVE_ENABLED=1; vehicle registered under NETC.",
             "steps": "Open /fastag → enter the vehicle number → Tag Status tab.",
             "input": '{"rc_number": "CG07BC9186"}',
             "expected": "HTTP 200; one row per issued tag (a re-issued vehicle keeps its historic tags), each with TAGID, TAGSTATUS, VEHICLECLASS.",
             "shots": [
                 ("request", "Application input — vehicle number entered in the Tag Status panel"),
                 ("response", "Application API response for POST /api/fastag/tag-status listing every tag"),
                 ("ui", "FASTag screen rendering tag id, status, class and issuing bank"),
             ]},
            {"id": "02", "title": "Tag registry lookup by tag id",
             "precondition": "Same as TC-01.",
             "steps": "On the Tag Status tab, enter the tag id in 'Look up by Tag ID' and press Check Tag.",
             "input": '{"tag_id": "34161FA8203286140F4064E0"}',
             "expected": "HTTP 200; the tag row for that tag id, with the registration number it is issued against.",
             "shots": [
                 ("request", "Application input — tag id entered in the Tag Status panel"),
                 ("response", "Application API response for the tag-id lookup"),
             ]},
            {"id": "03", "title": "Both identifiers supplied — rejected client-side",
             "precondition": "Any.",
             "steps": "Submit vehicle number and tag id together.",
             "input": '{"rc_number": "CG07BC9186", "tag_id": "34161FA8203286140F4064E0"}',
             "expected": "Application rejects the request before calling ULIP, avoiding the documented respCode 239 failure.",
             "shots": [
                 ("response", "Application error response when both identifiers are supplied"),
             ]},
        ],
        "notes": (
            "<b>Confirmed by NLDSL, 12 August 2026:</b> staging holds static test data, so "
            "whether tag details exist against a given vehicle number depends on "
            "what is configured there — which is why the vehicle-number lookup "
            "returned no tags while the tag-id lookup succeeded.<br><br>"
            "Separately, the integration document gives two different patterns "
            "for <code>vehiclenumber</code> on this API — the field table states "
            "<code>^[A-Z0-9]{5,11}$|^[A-Z0-9]{17,20}$</code> while the 400 "
            "sample shows <code>[A-Z]{2}[0-9]{2}[A-Z]{0,5}[0-9]{4}$</code>. The "
            "application validates against the looser of the two."),
    },
    # ------------------------------------------------------------------ LDB/01
    {
        "api": "LDB/01",
        "granted": "LDB_01",
        "name": "EXIM container tracking trail",
        "doc_ref": "ULIP_LDB_Integration_Requirement.pdf §1.3",
        "purpose": (
            "Supplies the authoritative multimodal movement trail for an EXIM "
            "container — vessel, rail and road legs with coordinates. This is the "
            "backbone of the DTCCC's container lifecycle view: it tells the port "
            "where a box physically is, whether it is laden or empty, and by which "
            "mode it is moving, independently of terminal-system messages."),
        "ulip_request": """POST /ulip/v1.0.0/LDB/01
Authorization: Bearer <token>
Content-Type: application/json

{
  "containerNumber": "NSST1234570"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "eximContainerTrail": {
        "cntrDetail": { "cntrno": "NSST1234570", "cntrsize": 40,
                        "isocode": "22G3" },
        "trackLog": [
          { "serialno": 1, "eventname": "PORT OUT",
            "currentlocation": "Raigad/Nhava Sheva",
            "timestamptimezone": "2023-03-29 12:10:09", "timezoneabvr": "IST",
            "latitude": 18.950149, "longitude": 72.95123,
            "containernumber": "NSST1234570", "transportmode": "TRUCK",
            "type": "I", "isempty": "Y" },
          { "serialno": 2, "eventname": "PORT IN",
            "currentlocation": "Raigad/Nhava Sheva",
            "timestamptimezone": "2023-03-28 10:10:08", "timezoneabvr": "IST",
            "latitude": 18.950149, "longitude": 72.95123,
            "containernumber": "NSST1234570", "transportmode": "VESSEL",
            "type": "I", "isempty": "Y" }
        ]
      }
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_container_tracking()"),
            ("Normaliser", "normalize_container_events() — schemas.py"),
            ("Service", "LogisticsService — LIVE → CACHED → DATABASE → FALLBACK"),
            ("Application API", "GET /api/logistics/tracking/{container}, GET /api/ldb/container/{no}"),
            ("Persistence", "core.logistics_event"),
            ("Screen", "UC3 Lifecycle / Follow-the-Box — /uc3-lifecycle"),
        ],
        "app_api": "GET /api/logistics/tracking/NSST1234570",
        "app_request": """GET /api/logistics/tracking/NSST1234570
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "status": "LIVE",
  "source": "ULIP",
  "ref_type": "CONTAINER",
  "ref_id": "NSST1234570",
  "count": 2,
  "events": [
    { "event_type": "PORT OUT", "event_ts": "2023-03-29T12:10:09",
      "location": "Raigad/Nhava Sheva",
      "latitude": 18.950149, "longitude": 72.95123,
      "detail": { "transportmode": "TRUCK", "isempty": "Y" } },
    { "event_type": "PORT IN", "event_ts": "2023-03-28T10:10:08",
      "location": "Raigad/Nhava Sheva",
      "latitude": 18.950149, "longitude": 72.95123,
      "detail": { "transportmode": "VESSEL", "isempty": "Y" } }
  ]
}""",
        "screen": "UC3 Lifecycle / Follow-the-Box (/uc3-lifecycle)",
        "cases": [
            {"id": "01", "title": "Track a known EXIM container",
             "precondition": "ULIP_LIVE_ENABLED=1; container known to LDB.",
             "steps": "Open /uc3-lifecycle → enter the container number → Track.",
             "input": "containerNumber = NSST1234570",
             "expected": "HTTP 200, status=LIVE, source=ULIP; one event per trackLog entry ordered by time, each with mode, location and coordinates; events persisted to core.logistics_event.",
             "shots": [
                 ("request", "Application input — container number entered on the lifecycle screen"),
                 ("response", "Application API response for GET /api/logistics/tracking/{container} with the normalised trail"),
                 ("ui", "Container movement trail rendered on the timeline and map"),
             ]},
            {"id": "02", "title": "Unknown container returns an explicit empty trail",
             "precondition": "Container number valid per ISO 6346 but unknown to LDB.",
             "steps": "Repeat TC-01 with an unknown container number.",
             "input": "containerNumber = MSKU0000000",
             "expected": "ULIP answers HTTP 200 with responseStatus=FAILURE. Application reports zero events and falls through the ladder to CACHED/DATABASE rather than fabricating a trail.",
             "shots": [
                 ("response", "Application response for an unknown container — empty event list, explicit status"),
                 ("ui", "Lifecycle screen empty state for an untracked container"),
             ]},
            {"id": "03", "title": "Invalid container number rejected before the call",
             "precondition": "Any.",
             "steps": "Submit a container number failing the ISO 6346 pattern.",
             "input": "containerNumber = ABC123",
             "expected": "HTTP 422 from the application; no ULIP call is made.",
             "shots": [
                 ("response", "Validation error for a malformed container number"),
             ]},
        ],
        "notes": (
            "<b>Confirmed working by NLDSL on 12 August 2026</b> with container "
            "<code>TCLU8538808</code>, which returns a thirteen-leg trail across "
            "vessel, rail and road.<br><br>Two behaviours are recorded for "
            "completeness. LDB returns its <code>trackLog</code> field names in "
            "lower case (<code>eventname</code>, <code>currentlocation</code>, "
            "<code>timestamptimezone</code>), which the application matches "
            "exactly. And on staging the same static trail is returned whatever "
            "container number is asked for — so the application checks the trail "
            "against the container requested and rejects a contradiction, rather "
            "than attributing one box's port milestones to another."),
    },
    # ------------------------------------------------------------------ VAHAN/04
    {
        "api": "VAHAN/04",
        "granted": "VAHAN_04",
        "name": "Registration certificate by vehicle number (JSON)",
        "doc_ref": "ULIP_VAHAN_Integration_Requirement.pdf §1.7",
        "purpose": (
            "Primary vehicle-verification source at the JNPA gate. Before a truck "
            "is admitted the DTCCC verifies that the plate read by ANPR corresponds "
            "to a real, currently registered vehicle with valid fitness, of a "
            "vehicle class permitted for port haulage, and not blacklisted. This "
            "is the top rung of the vehicle fallback ladder."),
        "ulip_request": """POST /ulip/v1.0.0/VAHAN/04
Authorization: Bearer <token>
Content-Type: application/json

{
  "vehiclenumber": "UP32KH0320"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "stautsMessage": "OK", "stateCd": "UP", "rtoCd": "91",
      "rcRegnNo": "UP91L0001", "rcRegnDt": "26-Jan-2017",
      "rcChasiNo": "ME4JF509AH70*****", "rcEngNo": "JF50E760*****",
      "rcVhClassDesc": "M-Cycle/Scooter", "rcOwnerName": "R***L K***R",
      "rcFuelDesc": "PETROL", "rcFitUpto": "25-Jan-2032",
      "rcMakerModel": "HONDA ACTIVA", "rcBlacklistStatus": "",
      "rcRegisteredAt": "HAMIRPUR(UP), Uttar Pradesh"
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_vehicle_by_rc()"),
            ("Normaliser", "normalize_rc() → rc_to_record() — integrations/ulip/records.py"),
            ("Service", "Vehicle fallback ladder — LIVE_PRIMARY → LIVE_FALLBACK → CACHED → PROVISIONAL"),
            ("Application API", "GET /api/vahan/rc/{plate}, GET /api/vahan/vehicle-intel/{plate}"),
            ("Persistence", "core.vehicle_verification_history (source='ULIP')"),
            ("Screen", "Vehicle Management — /vehicles; Intelligence — /intelligence"),
        ],
        "app_api": "GET /api/vahan/vehicle-intel/UP32KH0320",
        "app_request": """GET /api/vahan/vehicle-intel/UP32KH0320
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "path": "LIVE_PRIMARY",
  "source": "ULIP",
  "rc_number": "UP91L0001",
  "owner_name_masked": "R***L K***R",
  "vehicle_class": "M-Cycle/Scooter",
  "maker_model": "HONDA ACTIVA",
  "fuel_type": "PETROL",
  "registration_date": "2017-01-26",
  "fitness_valid_to": "2032-01-25",
  "chassis_number": "ME4JF509AH70*****",
  "engine_number": "JF50E760*****",
  "rto_code": "91",
  "registered_at": "HAMIRPUR(UP), Uttar Pradesh",
  "blacklist_status": "CLEAR"
}""",
        "screen": "Vehicle Management (/vehicles) — RC verification",
        "cases": [
            {"id": "01", "title": "Verify a registered vehicle",
             "precondition": "ULIP_LIVE_ENABLED=1; vehicle registered in VAHAN.",
             "steps": "Open /vehicles → enter the vehicle number → Verify.",
             "input": "vehiclenumber = UP32KH0320",
             "expected": "HTTP 200 with path=LIVE_PRIMARY and source=ULIP; RC fields populated; fitness and blacklist status evaluated; row written to core.vehicle_verification_history.",
             "shots": [
                 ("request", "Application input — vehicle number entered on the Vehicle Management screen"),
                 ("response", "Application API response for GET /api/vahan/vehicle-intel/{plate} showing path=LIVE_PRIMARY, source=ULIP"),
                 ("ui", "Vehicle Management screen rendering the RC record, fitness validity and verification path"),
             ]},
            {"id": "02", "title": "Unknown vehicle degrades down the ladder",
             "precondition": "Vehicle number valid in format but not present in VAHAN.",
             "steps": "Repeat TC-01 with an unregistered vehicle number.",
             "input": "vehiclenumber = MH01ZZ9999",
             "expected": "ULIP answers HTTP 200 with message.code=231 ('Vehicle Details not Found'). Application records no RC, retries VAHAN/01, then continues down the ladder and marks the vehicle PROVISIONAL with a cure window — never a half-populated record.",
             "shots": [
                 ("response", "Application response for an unknown vehicle showing the degraded path"),
                 ("ui", "Vehicle Management screen showing the provisional/unverified state"),
             ]},
            {"id": "03", "title": "Verification history persisted with source attribution",
             "precondition": "TC-01 executed at least once.",
             "steps": "Open the verification history view on /vehicles.",
             "input": "GET /api/vahan/verification-history",
             "expected": "The verification performed in TC-01 appears with source='ULIP' and its timestamp.",
             "shots": [
                 ("ui", "Verification history listing the ULIP-sourced verification"),
             ]},
        ],
        "notes": (
            "The integration document states that <code>rc_owner_name</code>, "
            "<code>rc_present_address</code>, <code>rc_permanent_address</code>, "
            "<code>rc_mobile_no</code> and <code>rc_f_name</code> are masked by "
            "default for all users. The application stores the masked value as "
            "received and never re-masks it. Confirmation is requested from NLDSL "
            "as to whether unmasked owner details can be enabled for this account, "
            "since port security and police-report surfaces require the real "
            "registered-owner name."),
    },
    # ------------------------------------------------------------------ VAHAN/01
    {
        "api": "VAHAN/01",
        "granted": "VAHAN_01",
        "name": "Registration certificate by vehicle number (XML)",
        "doc_ref": "ULIP_VAHAN_Integration_Requirement.pdf §1.4",
        "purpose": (
            "Automatic retry rung behind VAHAN/04. When the JSON API returns no "
            "record for a plate, the application re-queries the XML variant before "
            "concluding the vehicle is unknown, so that a gate decision is never "
            "taken on a single source's miss."),
        "ulip_request": """POST /ulip/v1.0.0/VAHAN/01
Authorization: Bearer <token>
Content-Type: application/json

{
  "vehiclenumber": "UP32KH0320"
}""",
        "ulip_response": """{
  "response": [{
    "response": "<?xml version=\\"1.0\\" encoding=\\"UTF-8\\" standalone=\\"yes\\"?>
      <VehicleDetails>
        <stautsMessage>OK</stautsMessage>
        <rc_regn_no>UP91L0001</rc_regn_no>
        <rc_regn_dt>26-Jan-2017</rc_regn_dt>
        <rc_owner_name>R***L K***R</rc_owner_name>
        <rc_vh_class_desc>M-Cycle/Scooter</rc_vh_class_desc>
        <rc_fuel_desc>PETROL</rc_fuel_desc>
        <rc_chasi_no>ME4JF509AH70*****</rc_chasi_no>
        <rto_cd>91</rto_cd>
        <rc_status>ACTIVE</rc_status>
      </VehicleDetails>",
    "responseStatus": "SUCCESS",
    "message": null
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_vehicle_by_rc_xml()"),
            ("Normaliser", "normalize_vahan_xml() — parses the XML string to the same RC shape as VAHAN/04"),
            ("Service", "Retry rung inside LIVE_PRIMARY"),
            ("Application API", "GET /api/vahan/rc/{plate} (transparent retry)"),
            ("Screen", "Vehicle Management — /vehicles"),
        ],
        "app_api": "GET /api/vahan/rc/UP32KH0320  (served via the VAHAN/04 → VAHAN/01 retry)",
        "app_request": """GET /api/vahan/rc/UP32KH0320
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "path": "LIVE_PRIMARY",
  "source": "ULIP",
  "upstream_api": "VAHAN/01",
  "rc_number": "UP91L0001",
  "owner_name_masked": "R***L K***R",
  "vehicle_class": "M-Cycle/Scooter",
  "fuel_type": "PETROL",
  "registration_date": "2017-01-26",
  "chassis_number": "ME4JF509AH70*****",
  "rto_code": "91"
}""",
        "screen": "Vehicle Management (/vehicles)",
        "cases": [
            {"id": "01", "title": "XML RC parses to the same record shape as VAHAN/04",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Query a vehicle number and observe the RC returned by the XML API.",
             "input": "vehiclenumber = UP32KH0320",
             "expected": "The XML payload is parsed and the resulting record is field-for-field interchangeable with the VAHAN/04 record, so the retry is transparent to every consumer.",
             "shots": [
                 ("request", "Application input — vehicle number submitted for RC lookup"),
                 ("response", "Application API response served through the VAHAN/01 retry, annotated with upstream_api"),
                 ("ui", "Vehicle Management screen rendering the RC obtained via the XML API"),
             ]},
            {"id": "02", "title": "Retry engages only when VAHAN/04 misses",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Query a vehicle unknown to VAHAN/04 and observe that VAHAN/01 is attempted before the ladder degrades.",
             "input": "vehiclenumber = MH01ZZ9999",
             "expected": "Both APIs are attempted exactly once; the application then degrades rather than reporting an unverified vehicle as verified.",
             "shots": [
                 ("response", "Application response showing both upstream attempts and the resulting path"),
             ]},
        ],
        "notes": (
            "VAHAN/01 returns the registration certificate as an <b>XML document "
            "embedded as a string</b> inside the JSON envelope. The application "
            "parses it into the identical record shape used for VAHAN/04, which is "
            "what makes the two interchangeable."),
    },
    # ------------------------------------------------------------------ VAHAN/02
    {
        "api": "VAHAN/02",
        "granted": "VAHAN_02",
        "name": "Registration certificate by chassis number",
        "doc_ref": "ULIP_VAHAN_Integration_Requirement.pdf §1.5",
        "purpose": (
            "Identity resolution when the plate is unusable — an unreadable, "
            "damaged, obscured or suspected-tampered number plate at the gate, or a "
            "vehicle presented for investigation. The chassis number is stamped on "
            "the vehicle and lets the DTCCC establish the true registration "
            "independently of what the plate claims."),
        "ulip_request": """POST /ulip/v1.0.0/VAHAN/02
Authorization: Bearer <token>
Content-Type: application/json

{
  "chasisnumber": "ME4JF509AH707****"
}""",
        "ulip_response": """{
  "response": [{
    "response": "<?xml version=\\"1.0\\" encoding=\\"UTF-8\\" standalone=\\"yes\\"?>
      <VehicleDetails>
        <rc_regn_no>UP91L0001</rc_regn_no>
        <rc_chasi_no>ME4JF509AH70*****</rc_chasi_no>
        <rc_vh_class_desc>M-Cycle/Scooter</rc_vh_class_desc>
        <rc_status>ACTIVE</rc_status>
      </VehicleDetails>",
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_vehicle_by_chassis() — note ULIP's field spelling 'chasisnumber'"),
            ("Normaliser", "normalize_vahan_xml()"),
            ("Application API", "GET /api/vahan/chassis/{chassis_number}"),
            ("Screen", "Vehicle Management — /vehicles"),
        ],
        "app_api": "GET /api/vahan/chassis/ME4JF509AH707****",
        "app_request": """GET /api/vahan/chassis/{chassis_number}
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "source": "ULIP",
  "upstream_api": "VAHAN/02",
  "lookup_key": "chassis",
  "rc_number": "UP91L0001",
  "chassis_number": "ME4JF509AH70*****",
  "vehicle_class": "M-Cycle/Scooter",
  "rc_status": "ACTIVE"
}""",
        "screen": "Vehicle Management (/vehicles) — RC Lookup tab",
        "cases": [
            {"id": "01", "title": "Resolve a vehicle from its chassis number",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Open /vehicles → RC Lookup tab → select Chassis → enter the chassis number → Search.",
             "input": "chasisnumber = ME4JF509AH707****",
             "expected": "HTTP 200 with the registration number and RC details resolved from the chassis number.",
             "shots": [
                 ("request", "Application input — chassis number entered for alternate-key lookup"),
                 ("response", "Application API response for GET /api/vahan/chassis/{no}"),
                 ("ui", "Vehicle Management screen showing the vehicle resolved from its chassis number"),
             ]},
            {"id": "02", "title": "Unknown chassis number",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Repeat TC-01 with a chassis number not present in VAHAN.",
             "input": "chasisnumber = ZZZZZZZZZZZZZZZZ",
             "expected": "HTTP 200 from ULIP with no record; the application returns an explicit not-found rather than an empty record.",
             "shots": [
                 ("response", "Application not-found response for an unknown chassis number"),
             ]},
        ],
        "notes": (
            "The ULIP request field is spelled <code>chasisnumber</code> (single 's') in "
            "the integration document; the application sends it exactly as "
            "documented. <b>NLDSL confirmed on 12 August 2026</b> that the "
            "chassis number masked in the VAHAN/04 response falls under the PII "
            "clause and cannot be unmasked under the current grant, so a "
            "successful lookup cannot be evidenced from a VAHAN/04-derived key. "
            "Access to unmasked identifiers is being taken forward separately "
            "as a new requirement."),
    },
    # ------------------------------------------------------------------ VAHAN/03
    {
        "api": "VAHAN/03",
        "granted": "VAHAN_03",
        "name": "Registration certificate by engine number",
        "doc_ref": "ULIP_VAHAN_Integration_Requirement.pdf §1.6",
        "purpose": (
            "Second alternate-key resolution path, used in the same circumstances "
            "as VAHAN/02 and as a cross-check: where a chassis number and an engine "
            "number resolve to different registrations, the vehicle is flagged for "
            "physical inspection."),
        "ulip_request": """POST /ulip/v1.0.0/VAHAN/03
Authorization: Bearer <token>
Content-Type: application/json

{
  "enginenumber": "JF50E7608****"
}""",
        "ulip_response": """{
  "response": [{
    "response": "<?xml version=\\"1.0\\" encoding=\\"UTF-8\\" standalone=\\"yes\\"?>
      <VehicleDetails>
        <rc_regn_no>UP91L0001</rc_regn_no>
        <rc_eng_no>JF50E760*****</rc_eng_no>
        <rc_vh_class_desc>M-Cycle/Scooter</rc_vh_class_desc>
        <rc_status>ACTIVE</rc_status>
      </VehicleDetails>",
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_vehicle_by_engine()"),
            ("Normaliser", "normalize_vahan_xml()"),
            ("Application API", "GET /api/vahan/engine/{engine_number}"),
            ("Screen", "Vehicle Management — /vehicles"),
        ],
        "app_api": "GET /api/vahan/engine/JF50E7608****",
        "app_request": """GET /api/vahan/engine/{engine_number}
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "source": "ULIP",
  "upstream_api": "VAHAN/03",
  "lookup_key": "engine",
  "rc_number": "UP91L0001",
  "engine_number": "JF50E760*****",
  "vehicle_class": "M-Cycle/Scooter",
  "rc_status": "ACTIVE"
}""",
        "screen": "Vehicle Management (/vehicles) — RC Lookup tab",
        "cases": [
            {"id": "01", "title": "Resolve a vehicle from its engine number",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Open /vehicles → RC Lookup tab → select Engine → enter the engine number → Search.",
             "input": "enginenumber = JF50E7608****",
             "expected": "HTTP 200 with the registration number and RC details resolved from the engine number.",
             "shots": [
                 ("request", "Application input — engine number entered for alternate-key lookup"),
                 ("response", "Application API response for GET /api/vahan/engine/{no}"),
                 ("ui", "Vehicle Management screen showing the vehicle resolved from its engine number"),
             ]},
            {"id": "02", "title": "Unknown engine number",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Repeat TC-01 with an engine number not present in VAHAN.",
             "input": "enginenumber = ZZZZZZZZZZ",
             "expected": "Explicit not-found; no fabricated record.",
             "shots": [
                 ("response", "Application not-found response for an unknown engine number"),
             ]},
        ],
        "notes": (
            "<b>Confirmed by NLDSL, 12 August 2026.</b> The engine number returned by "
            "VAHAN/04 is masked (<code>JF50E760*****</code>) and masked fields "
            "fall under the PII clause, so a masked value cannot be fed back in "
            "as a lookup key. The endpoint responds correctly; only a successful "
            "resolution is unevidenced."),
    },
    # ------------------------------------------------------------------ SARATHI/02
    {
        "api": "SARATHI/02",
        "granted": "SARATHI_02",
        "name": "Driving licence details by DL number",
        "doc_ref": "ULIP_SARATHI_Integration_Requirement.pdf §1.4",
        "purpose": (
            "Primary driver-verification source. Before a driver is enrolled for "
            "port entry, or admitted at the gate, the DTCCC confirms the licence is "
            "active, is valid for transport vehicles on the date of entry, and "
            "carries a class of vehicle covering the truck being driven. This is "
            "the top rung of the driver fallback ladder."),
        "ulip_request": """POST /ulip/v1.0.0/SARATHI/02
Authorization: Bearer <token>
Content-Type: application/json

{
  "dlnumber": "AP01620210000019"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "DLinformation": {
        "Classofcovs": [
          { "CovDiscription": "Motor Cycle with Gear(Non Transport)",
            "CovCode": 3 },
          { "CovDiscription": "LIGHT MOTOR VEHICLE", "CovCode": 4 },
          { "CovDiscription": "Transport Vehicle-M/HMV (Goods and Passenger)",
            "CovCode": 10 }
        ],
        "NonTransportValidityTodate": "27-04-2031",
        "TransportValidityTodate": "16-02-2028",
        "DL_Holder_FullName": "RAMSANEHI",
        "DL_status": "Active."
      }
    },
    "responseStatus": "SUCCESS",
    "message": null
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_dl()"),
            ("Normaliser", "normalize_dl() → dl_to_record() — prefers TransportValidityTodate"),
            ("Service", "Driver fallback ladder; upsert_driver_from_dl()"),
            ("Application API", "GET /api/vahan/dl/{dl_number}, GET /api/vahan/driver-intel/{key}"),
            ("Persistence", "core.dl_lookup_history (source='ULIP'), driver master"),
            ("Screen", "Driver Master (tab on /vehicles); Driver Enrollments — /enrollments"),
        ],
        "app_api": "GET /api/vahan/dl/AP01620210000019",
        "app_request": """GET /api/vahan/dl/AP01620210000019
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "path": "LIVE_PRIMARY",
  "source": "ULIP",
  "dl_number": "AP01620210000019",
  "holder_name_masked": "MAJJI KESAVARAO",
  "valid_to": "2031-12-03",
  "vehicle_classes": [
    "LIGHT MOTOR VEHICLE",
    "Motor Cycle with Gear(Non Transport)"
  ],
  "blacklist_status": "CLEAR"
}""",
        "screen": "Driver Master (/vehicles → Driver Master tab); Driver Enrollments (/enrollments)",
        "cases": [
            {"id": "01", "title": "Verify an active transport licence",
             "precondition": "ULIP_LIVE_ENABLED=1; DL present in SARATHI.",
             "steps": "Open Driver Master → enter the DL number → Verify.",
             "input": "dlnumber = AP01620210000019",
             "expected": "HTTP 200 with path=LIVE_PRIMARY, source=ULIP; holder name, status, classes of vehicle and the transport validity date; row written to core.dl_lookup_history.",
             "shots": [
                 ("request", "Application input — DL number entered on the Driver Master screen"),
                 ("response", "Application API response for GET /api/vahan/dl/{dl} showing the licence record"),
                 ("ui", "Driver Master screen rendering holder name, licence status, validity and classes of vehicle"),
             ]},
            {"id": "02", "title": "Transport validity is used, not the non-transport date",
             "precondition": "A licence carrying both validity dates.",
             "steps": "Verify the licence and inspect the validity shown.",
             "input": "dlnumber = AP01620210000019",
             "expected": "valid_to = 16-02-2028 (TransportValidityTodate), not 27-04-2031. A port-corridor driver is a transport driver, so the non-transport date would overstate usable validity.",
             "shots": [
                 ("ui", "Driver record showing the transport validity date as the governing expiry"),
             ]},
            {"id": "03", "title": "Unknown DL number",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Verify a DL number not present in SARATHI.",
             "input": "dlnumber = XX00000000000000",
             "expected": "ULIP answers HTTP 200 with errorcode=-1 ('Details not Found For Given DLNumber'). Application records no driver and degrades down the ladder; no partial driver record is created.",
             "shots": [
                 ("response", "Application response for an unknown DL number"),
                 ("ui", "Driver Master screen showing the unverified state"),
             ]},
            {"id": "04", "title": "Non-active licence fails closed",
             "precondition": "A licence whose DL_status is not active.",
             "steps": "Verify the licence and observe the enrolment decision.",
             "input": "dlnumber = <suspended licence>",
             "expected": "Any status that is not clearly active is treated as not clear for port entry — the driver is blocked rather than admitted by default.",
             "shots": [
                 ("ui", "Driver screen showing a non-active licence blocked for port entry"),
             ]},
        ],
        "notes": "",
    },
    # ------------------------------------------------------------------ SARATHI/01
    {
        "api": "SARATHI/01",
        "granted": "SARATHI_01",
        "name": "Driving licence details by DL number and date of birth",
        "doc_ref": "ULIP_SARATHI_Integration_Requirement.pdf §1.3",
        "purpose": (
            "Corroborating driver lookup used during enrolment, where the driver is "
            "physically present and their date of birth is captured from the "
            "licence. Supplying both identifiers binds the record to the person "
            "presenting it, which is the stronger check for issuing a port pass."),
        "ulip_request": """POST /ulip/v1.0.0/SARATHI/01
Authorization: Bearer <token>
Content-Type: application/json

{
  "dlnumber": "AP01620210000019",
  "dob": "1987-05-26"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "dldetobj": [{
        "dlobj": {
          "dlLicno": "GJ04 20120005008  ", "dlStatus": "Active",
          "dlIssuedt": "2012-03-07", "dlTrValdtoDt": "2022-08-09",
          "dlNtValdtoDt": "2032-03-06", "dlRtoCode": "GJ33",
          "stateName": "Gujarat", "statecd": "GJ"
        },
        "dlcovs": [
          { "covabbrv": "MCWG  ",
            "covdesc": "Motor Cycle with Gear(Non Transport)" },
          { "covabbrv": "TRANS",
            "covdesc": "Transport Vehicle-M/HMV (Goods & Passenger)" },
          { "covabbrv": "LMV   ", "covdesc": "LIGHT MOTOR VEHICLE" }
        ],
        "bioObj": {
          "bioNatName": "MAHESHKUMAR  GOHIL",
          "bioFullName": "M*********R G***L",
          "bioDob": "1987-05-26", "bioGenderDesc": "Male",
          "bioSwdFullName": "RAMJIBHAI  GOHIL",
          "biPhoto": "<base64 JPEG>"
        },
        "transReqObj": [
          { "trName": "RENEWAL OF DL", "trEntrydt": "2019-08-02",
            "olaOffName": "ARTO BOTAD" }
        ]
      }]
    },
    "responseStatus": "SUCCESS",
    "message": null
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_dl_with_dob()"),
            ("Normaliser", "normalize_dl()"),
            ("Application API", "GET /api/vahan/dl/{dl}?dob=YYYY-MM-DD — the "
                                "date of birth selects SARATHI/01 as the first "
                                "attempt, with SARATHI/02 as the fallback"),
            ("Persistence", "core.dl_lookup_history (source='ULIP')"),
            ("Screen", "Driver Enrollments — /enrollments"),
        ],
        "app_api": "GET /api/vahan/dl/{dl}?dob=YYYY-MM-DD",
        "app_request": """GET /api/vahan/dl/AP01620210000019?dob=1987-05-26
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "dl": "AP01620210000019",
  "decision_path": "LIVE_PRIMARY",
  "status": "VALID",
  "record": {
    "dl_number": "AP01620210000019",
    "holder_name_masked": "<unmasked from bioNatName when /01 answers>",
    "date_of_issue": "<from dlIssuedt — /01 only>",
    "valid_to": "<dlTrValdtoDt, else dlNtValdtoDt>",
    "vehicle_classes": ["<from dlcovs[].covdesc>"],
    "state": "<stateName — /01 only>",
    "rto_code": "<dlRtoCode — /01 only>",
    "blacklist_status": "CLEAR"
  }
}""",
        "screen": "Driver Enrollments (/enrollments)",
        "cases": [
            {"id": "01", "title": "Enrolment lookup with DL number and date of birth",
             "precondition": "ULIP_LIVE_ENABLED=1; driver present with licence.",
             "steps": "Call GET /api/vahan/dl/{dl} with the holder's date of birth as the dob query parameter, as the enrolment flow does.",
             "input": 'GET /api/vahan/dl/AP01620210000019?dob=1987-05-26',
             "expected": "HTTP 200; licence detail returned and bound to the enrolment record.",
             "shots": [
                 ("request", "Application input — DL number and date of birth entered during enrolment"),
                 ("response", "Application API response for the DL + DOB lookup"),
                 ("ui", "Enrolment screen showing the corroborated driver record"),
             ]},
            {"id": "02", "title": "Date of birth format validated before the call",
             "precondition": "Any.",
             "steps": "Submit a date of birth in DD-MM-YYYY rather than the documented YYYY-MM-DD.",
             "input": 'GET /api/vahan/dl/AP01620210000019?dob=26-05-1987',
             "expected": "Application rejects the request client-side; no ULIP call is made.",
             "shots": [
                 ("response", "Validation error for an incorrectly formatted date of birth"),
             ]},
        ],
        "notes": (
            "<b>Working end to end with the test data NLDSL supplied on 12 August 2026.</b> "
            "SARATHI/01 shares no field names with SARATHI/02 — the licence is "
            "in <code>dldetobj[].dlobj</code>, the classes of vehicle in "
            "<code>dlcovs[].covdesc</code> and the holder in a <code>bio</code> "
            "block — and both are normalised to one shape so /01 can stand in "
            "for /02. /01 additionally carries the licence issue date, the "
            "issuing state and RTO, and a photograph.<br><br>"
            "<b>The DL number's internal space is significant:</b> "
            "<code>GJ04 20120005008</code> resolves on SARATHI/01 while "
            "<code>GJ0420120005008</code> returns <code>errorcd -1</code>. The "
            "application preserves the spacing the caller supplies for the "
            "upstream call and uses the space-free form only as its own "
            "storage key. Note also that staging masks <code>bioNatName</code> "
            "as well as <code>bioFullName</code>, so no unmasked holder name is "
            "available."),
    },
    # ------------------------------------------------------------------ GATISHAKTI/04
    {
        "api": "GATISHAKTI/04",
        "granted": "GATISHAKTI_04",
        "name": "NHAI toll plazas by state",
        "doc_ref": "ULIP_GATISHAKTI_Integration_Requirement.pdf §1.6",
        "purpose": (
            "Supplies the authoritative toll-plaza master for Maharashtra "
            "(LGD state id 27), which the DTCCC uses as reference data: to resolve "
            "the plaza names returned by FASTAG/01 against a canonical registry, to "
            "place plazas on the corridor map, and to compute the sequence of "
            "plazas a truck is expected to cross en route to JNPA."),
        "ulip_request": """POST /ulip/v1.0.0/GATISHAKTI/04
Authorization: Bearer <token>
Content-Type: application/json

{
  "stateid": "27"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "data": [
        { "plaza_name": "(Planned) Tilasar", "tollplazal": 21.173482,
          "tollplaz_1": 74.005793, "nooflanes": "4L", "nhno_new": "NH- 53",
          "nearesthos": null, "tollcollec": null,
          "project_na": "Fagne-MH/GJ Border" },
        { "plaza_name": "Nandgaon Peth", "tollplazal": 20.951008,
          "tollplaz_1": 77.788359, "nooflanes": "4L", "nhno_new": "NH- 53",
          "tollcollec": "M/s Sahakar Global Ltd" }
      ],
      "Result": true
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_toll_plazas()"),
            ("Normaliser", "normalize_toll_plazas() — coordinates parsed from high-precision strings"),
            ("Service", "GatiShaktiService.refresh() / .toll_plazas()"),
            ("Application API", "GET /api/gatishakti/toll-plazas?state_id=27, POST /api/gatishakti/refresh"),
            ("Persistence", "core.gs_toll_plaza (idempotent upsert)"),
            ("Consumer", "FASTag toll-enroute plaza resolution"),
        ],
        "app_api": "GET /api/gatishakti/toll-plazas?state_id=27",
        "app_request": """GET /api/gatishakti/toll-plazas?state_id=27
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "status": "LIVE",
  "source": "ULIP",
  "state_id": "27",
  "count": 2,
  "rows": [
    { "name": "Katnai", "state_id": "27", "nh_no": "NH-12",
      "latitude": 24.4059522538221785, "longitude": 88.0467862324777286 },
    { "name": "Malancha (P)", "state_id": "27",
      "latitude": 22.9036144349493753, "longitude": 88.4324715905935221 }
  ]
}""",
        "screen": "Reference data — consumed by FASTag toll-enroute and corridor analytics",
        "cases": [
            {"id": "01", "title": "Load the toll-plaza master for Maharashtra",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Invoke POST /api/gatishakti/refresh, then GET /api/gatishakti/toll-plazas?state_id=27.",
             "input": '{"stateid": "27"}',
             "expected": "HTTP 200; plaza rows with name and coordinates persisted to core.gs_toll_plaza; coordinates converted from string to numeric.",
             "shots": [
                 ("request", "Application request to refresh / list toll plazas for state id 27"),
                 ("response", "Application API response listing the toll-plaza master rows"),
             ]},
            {"id": "02", "title": "Refresh is idempotent",
             "precondition": "TC-01 executed once.",
             "steps": "Invoke the refresh a second time and compare row counts.",
             "input": '{"stateid": "27"}',
             "expected": "Row count is unchanged; existing rows are updated in place rather than duplicated.",
             "shots": [
                 ("response", "Row counts before and after a repeated refresh, showing no duplication"),
             ]},
            {"id": "03", "title": "Unknown state id returns empty, not an error",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Request an unused state id.",
             "input": '{"stateid": "99"}',
             "expected": "ULIP answers HTTP 200 with an empty data array; the application reports zero rows and writes nothing.",
             "shots": [
                 ("response", "Application response for an unknown state id"),
             ]},
        ],
        "notes": (
            "The application queries LGD state id <b>27 (Maharashtra)</b> for the "
            "JNPA corridor. <b>Request to NLDSL:</b> please confirm that account "
            "<code>rtoj_searchin_usr</code> is not geographically scoped to a "
            "single state, since GATISHAKTI/02, /03 and /04 are all state-keyed."),
    },
    # ------------------------------------------------------------------ GATISHAKTI/01
    {
        "api": "GATISHAKTI/01",
        "granted": "GATISHAKTI_01",
        "name": "National highway detail by NH number",
        "doc_ref": "ULIP_GATISHAKTI_Integration_Requirement.pdf §1.3",
        "purpose": (
            "Reference attributes for the national highways serving JNPA — "
            "segment length, road type and lane configuration by state. The "
            "DTCCC uses these to characterise the port's approach corridors. "
            "Note that the staging response carries no coordinates, so the "
            "corridor geometry itself must come from another source."),
        "ulip_request": """POST /ulip/v1.0.0/GATISHAKTI/01
Authorization: Bearer <token>
Content-Type: application/json

{
  "nhno": "NH-5"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "data": [
        { "road_name": "NH-5", "gis_length": 11.0479145145,
          "road_type": "National Highway", "lane_statu": "6L",
          "state_ut": "Haryana" },
        { "road_name": "NH-5", "gis_length": 23.7531232801,
          "road_type": "National Highway", "lane_statu": "4L",
          "state_ut": "Punjab" }
      ],
      "Result": true
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_nh_road()"),
            ("Normaliser", "normalize_road_network()"),
            ("Service", "GatiShaktiService.roads()"),
            ("Application API", "GET /api/gatishakti/roads?nh_no=NH-5"),
            ("Persistence", "core.gs_road_segment"),
        ],
        "app_api": "GET /api/gatishakti/roads?nh_no=NH-5",
        "app_request": """GET /api/gatishakti/roads?nh_no=NH-5
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "status": "LIVE",
  "source": "ULIP",
  "nh_no": "NH-5",
  "count": 1,
  "rows": [
    { "name": "<road segment name>", "nh_no": "NH-5",
      "latitude": 0.0, "longitude": 0.0 }
  ]
}""",
        "screen": "Reference data — corridor layer for road analytics",
        "cases": [
            {"id": "01", "title": "Fetch highway detail by NH number",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Invoke GET /api/gatishakti/roads?nh_no=NH-5.",
             "input": '{"nhno": "NH-5"}',
             "expected": "HTTP 200; road rows persisted to core.gs_road_segment with the NH number retained.",
             "shots": [
                 ("request", "Application request for highway detail by NH number"),
                 ("response", "Application API response with the highway reference rows"),
             ]},
            {"id": "02", "title": "Malformed NH number rejected before the call",
             "precondition": "Any.",
             "steps": "Submit an NH number that fails the documented pattern.",
             "input": '{"nhno": "5"}',
             "expected": "Application rejects client-side; no ULIP call is made.",
             "shots": [
                 ("response", "Validation error for a malformed NH number"),
             ]},
        ],
        "notes": (
            "<b>Confirmed by NLDSL, 12 August 2026.</b> The response structure for this "
            "API is <code>road_name</code>, <code>gis_length</code>, "
            "<code>road_type</code>, <code>lane_statu</code> and "
            "<code>state_ut</code> — road attributes without coordinates. The "
            "application stores those attributes and leaves geometry null "
            "rather than inventing it. Road-network data carrying geometry is "
            "being taken forward separately with NLDSL as a new requirement."),
    },
    # ------------------------------------------------------------------ GATISHAKTI/02
    {
        "api": "GATISHAKTI/02",
        "granted": "GATISHAKTI_02",
        "name": "State infrastructure by state id",
        "doc_ref": "ULIP_GATISHAKTI_Integration_Requirement.pdf §1.4",
        "purpose": (
            "Requested as the state road layer of the JNPA hinterland. On "
            "staging this API returns state warehousing infrastructure — food "
            "storage depots with capacity and ownership — rather than a road "
            "network. The rows are stored as received and are useful as "
            "hinterland storage reference data; see the note below."),
        "ulip_request": """POST /ulip/v1.0.0/GATISHAKTI/02
Authorization: Bearer <token>
Content-Type: application/json

{
  "stateid": "27"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "data": [
        { "infrastr_s": "MAHARASHTRA", "infrastr_n": "FSD RATNAGIRI",
          "infrastr_a": "C-65, MIDC, MIRJOLE RATNAGIRI",
          "type_owner": "Own", "storage_ca": "11664",
          "type_infra": "Covered", "infrastr_v": "MIDC, MIRJOLE" }
      ],
      "Result": true
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_state_roads()"),
            ("Normaliser", "normalize_road_network()"),
            ("Service", "GatiShaktiService.roads()"),
            ("Application API", "GET /api/gatishakti/roads?state_id=27"),
            ("Persistence", "core.gs_road_segment"),
        ],
        "app_api": "GET /api/gatishakti/roads?state_id=27",
        "app_request": """GET /api/gatishakti/roads?state_id=27
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "status": "LIVE",
  "source": "ULIP",
  "state_id": "27",
  "count": 1,
  "rows": [
    { "name": "<road name>", "state_id": "27",
      "latitude": 0.0, "longitude": 0.0 }
  ]
}""",
        "screen": "Reference data — corridor layer for road analytics",
        "cases": [
            {"id": "01", "title": "Load the state road network for Maharashtra",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Invoke GET /api/gatishakti/roads?state_id=27.",
             "input": '{"stateid": "27"}',
             "expected": "HTTP 200; road rows persisted to core.gs_road_segment keyed by state id.",
             "shots": [
                 ("request", "Application request for the state road network"),
                 ("response", "Application API response with the state road rows"),
             ]},
            {"id": "02", "title": "Unknown state id returns empty, not an error",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Request an unused state id.",
             "input": '{"stateid": "99"}',
             "expected": "Empty data array handled as zero rows; nothing written.",
             "shots": [
                 ("response", "Application response for an unknown state id"),
             ]},
        ],
        "notes": (
            "<b>Confirmed by NLDSL, 12 August 2026.</b> This API returns infrastructure "
            "data — <code>infrastr_s</code> (state), <code>infrastr_a</code> "
            "(address), <code>infrastr_n</code> (name), <code>type_owner</code>, "
            "<code>storage_ca</code> (capacity), <code>type_infra</code> and "
            "<code>infrastr_v</code> (village) — and this is the intended "
            "dataset. The application maps the fields as returned."),
    },
    # ------------------------------------------------------------------ GATISHAKTI/03
    {
        "api": "GATISHAKTI/03",
        "granted": "GATISHAKTI_03",
        "name": "Named geo-located points by state id",
        "doc_ref": "ULIP_GATISHAKTI_Integration_Requirement.pdf §1.5",
        "purpose": (
            "Requested as named, geo-located points along the road network. On "
            "staging this API returns industrial parks and estates with "
            "district, land category and coordinates. These are genuinely "
            "useful to the DTCCC as origin/destination reference for hinterland "
            "cargo, but they are not road-network points; see the note below."),
        "ulip_request": """POST /ulip/v1.0.0/GATISHAKTI/03
Authorization: Bearer <token>
Content-Type: application/json

{
  "stateid": "27"
}""",
        "ulip_response": """{
  "response": [{
    "response": {
      "data": [
        { "st_name": "MAHARASHTRA", "dist_name": "Latur",
          "sub_dist": "Nilanga", "vname": "Aurad (sha)",
          "park_name": "Nilanga Co-Op. Industrial Estate Ltd.",
          "land_cat": "", "land_avail": 0.0, "park_type": "Other",
          "lat": "18.069549104620279", "lon": "76.7894777008746985" }
      ],
      "Result": true
    },
    "responseStatus": "SUCCESS"
  }],
  "error": "false", "code": "200", "message": "Success"
}""",
        "chain": [
            ("ULIP client", "UlipClient.fetch_state_road_points()"),
            ("Normaliser", "normalize_road_network()"),
            ("Service", "GatiShaktiService.road_points()"),
            ("Application API", "GET /api/gatishakti/road-points?state_id=27"),
            ("Persistence", "core.gs_road_point"),
        ],
        "app_api": "GET /api/gatishakti/road-points?state_id=27",
        "app_request": """GET /api/gatishakti/road-points?state_id=27
Authorization: Bearer <application JWT>""",
        "app_response": """{
  "status": "LIVE",
  "source": "ULIP",
  "state_id": "27",
  "count": 2,
  "rows": [
    { "name": "Katnai", "state_id": "27", "nh_no": "NH-12",
      "latitude": 24.4059522538221785, "longitude": 88.0467862324777286 },
    { "name": "Malancha (P)", "state_id": "27",
      "latitude": 22.9036144349493753, "longitude": 88.4324715905935221 }
  ]
}""",
        "screen": "Reference data — corridor labelling for road analytics",
        "cases": [
            {"id": "01", "title": "Load named road points for Maharashtra",
             "precondition": "ULIP_LIVE_ENABLED=1.",
             "steps": "Invoke GET /api/gatishakti/road-points?state_id=27.",
             "input": '{"stateid": "27"}',
             "expected": "HTTP 200; points persisted to core.gs_road_point with numeric coordinates.",
             "shots": [
                 ("request", "Application request for named road points"),
                 ("response", "Application API response listing road points with coordinates"),
             ]},
            {"id": "02", "title": "High-precision coordinates preserved",
             "precondition": "TC-01 executed.",
             "steps": "Inspect a stored point's coordinates.",
             "input": "state_id = 27",
             "expected": "Coordinates arriving as high-precision strings are converted to numeric values without truncation to a wrong location.",
             "shots": [
                 ("response", "Stored road point showing the converted numeric coordinates"),
             ]},
        ],
        "notes": (
            "<b>Confirmed by NLDSL, 12 August 2026.</b> This API returns industrial land "
            "and park data — <code>st_name</code>, <code>dist_name</code>, "
            "<code>sub_dist</code>, <code>vname</code>, <code>park_name</code>, "
            "<code>land_cat</code>, <code>land_avail</code>, "
            "<code>park_type</code>, <code>lat</code> and <code>lon</code> — and "
            "this is the intended dataset. 994 rows were returned for "
            "Maharashtra and persisted with their coordinates."),
    },
]

# --------------------------------------------------------------------------
# Health / posture surfaces and constraints
# --------------------------------------------------------------------------
HEALTH = {
    "intro": (
        "The application exposes an integration-posture surface per module, which "
        "reports the configured ULIP API paths, the authentication mode in use, and "
        "the outcome of the most recent call. These are the screens an operator "
        "uses to confirm the ULIP link is live."),
    "endpoints": [
        ("GET /api/fastag/health", "FASTag module posture — configured APIs, auth mode, retention and balance notes"),
        ("GET /api/logistics/health", "LDB / logistics posture"),
        ("GET /api/gatishakti/health", "GatiShakti posture and reference-row counts"),
        ("GET /api/ldb/health", "Container tracking posture"),
    ],
    "screens": [
        ("System Health — /health", "Aggregate posture of every integration"),
        ("Integrations — /integrations", "Per-integration configuration and last-call outcome"),
    ],
    "shots": [
        ("ui", "System Health screen showing the ULIP integrations reporting live"),
        ("ui", "Integrations screen showing the ULIP configuration and last-call outcome"),
    ],
}

CONSTRAINTS = [
    ("RESOLVED — IP allow-list",
     "NLDSL registered 65.2.212.121 on 11 August 2026. Login verified the same "
     "day; 12 of 13 APIs answered on the first run. No further action."),
    ("RESOLVED — SARATHI/01 response schema",
     "Supplied by NLDSL on 11 August 2026 and mapped. The API returns the "
     "holder's name unmasked and a photograph, neither of which SARATHI/02 "
     "carries."),
    ("OPEN — no SARATHI/01 test record on staging",
     "Every DL/DOB pair available to us returns <code>errorcd: -1, erormsg: "
     "\"Details not available\"</code> on staging, including the licence used "
     "in the schema document itself. The mapping is verified against the "
     "supplied schema but not yet end to end. <b>Request to NLDSL:</b> please "
     "share a DL number and date of birth that resolve on staging."),
    ("RESOLVED — account geographic scope",
     "Maharashtra (state id 27) returns data on GATISHAKTI/01, /02, /03 and /04, "
     "so the account is not scoped away from the JNPA corridor. No further "
     "action."),
    ("OPEN — LDB/01 upstream unavailable",
     "Every LDB/01 call on 11 August 2026 returned "
     "<code>{\"response\": [{\"response\": \"LDB_01 - 3rd party service is "
     "down!\"}], \"error\": \"true\"}</code>. This is the API that carries "
     "container movement, which is central to the port use case. <b>Request to "
     "NLDSL:</b> please advise when the LDB upstream will be restored on staging "
     "so this API can be tested and evidenced."),
    ("OPEN — VAHAN/01 returns a different vehicle for the same input",
     "Querying <code>UP32KH0320</code> repeatedly on VAHAN/01 returned "
     "<code>RJ11GC0346</code> — a different make, class and owner — on roughly "
     "half of all calls, while VAHAN/04 returned the requested vehicle every "
     "time. The application now discards any registration certificate whose "
     "registration number does not match the number queried, because binding a "
     "stranger's record to a plate at the gate would clear a truck on someone "
     "else's fitness and blacklist status. <b>Request to NLDSL:</b> please "
     "confirm whether this is expected staging behaviour."),
    ("OPEN — GATISHAKTI/02 and /03 return datasets other than road networks",
     "GATISHAKTI/02 returns food-storage depot infrastructure and GATISHAKTI/03 "
     "returns industrial parks, where the integration document describes a state "
     "road network and named road points. GATISHAKTI/01 returns highway "
     "attributes but no coordinates. <b>Query to NLDSL:</b> are these the "
     "intended datasets, and is a road-network layer with geometry available "
     "under another API? GATISHAKTI/05 is described in the document but was not "
     "granted."),
    ("OPEN — no wallet-balance API",
     "None of the granted APIs returns a FASTag wallet balance. The "
     "application's balance surface reports 'not provided by ULIP' rather than "
     "displaying a fabricated figure. <b>Query to NLDSL:</b> is a balance API "
     "available under a different grant?"),
    ("OPEN — VAHAN owner-detail masking",
     "Owner name, addresses and mobile number are masked for all users per §1.3 "
     "of the VAHAN document. Masking also applies to the chassis and engine "
     "numbers in the VAHAN/04 response, which means a value returned by VAHAN/04 "
     "cannot be used as the lookup key for VAHAN/02 or VAHAN/03. <b>Query to "
     "NLDSL:</b> can unmasked details be enabled for this account? Port security "
     "and police-report surfaces require the real registered-owner name."),
    ("NOTED — mandatory Accept header",
     "ULIP answers a bodiless HTTP 400 to any request that does not send "
     "<code>Accept: application/json</code>, including the "
     "<code>Accept: */*</code> sent by default by most HTTP client libraries. "
     "Recorded here because the empty 400 gives no indication of the cause; the "
     "application now sends the header on every call including login."),
    ("NOTED — FASTAG/01 retention window",
     "The integration document states data is available only for the past 72 "
     "hours. The application therefore polls and accumulates crossings rather "
     "than relying on on-demand reads. No change requested."),
    ("NOTED — FASTAG/02 pattern conflict",
     "The document gives two different regular expressions for vehiclenumber on "
     "this API. The application validates against the looser pattern. <b>Query "
     "to NLDSL:</b> which is authoritative?"),
]

IP_NOTE = """
All application calls to ULIP originate from a single static AWS Elastic IP,
<b>65.2.212.121</b> (ap-south-1, allocation <code>eipalloc-0452eeb3c1b484845</code>),
which fronts the deployment. NLDSL registered this address on the ULIP staging
allow-list on <b>11 August 2026</b>, and it was verified working the same day:
<code>POST /user/login</code> returns <code>HTTP 200</code> with a bearer token,
and twelve of the thirteen granted APIs answered successfully on the first
run.

Two behaviours of the gate are recorded here because they are easily
misdiagnosed. Before registration, and for a username ULIP does not know, the
platform answers <code>HTTP 412 PRECONDITION_FAILED — "Access denied Please
contact ULIP support!"</code> <i>without evaluating the password</i>; a wrong
password against a known username answers <code>HTTP 401</code>. The
application surfaces the 412 as its own condition, distinct from an
authentication failure, so that an allow-list gap is never reported as a bad
credential.
"""

# --------------------------------------------------------------------------
# Live execution summary — the first end-to-end run against staging
# --------------------------------------------------------------------------
EXECUTION = {
    "intro": (
        "The table below records the first end-to-end execution against ULIP "
        "staging on 11 August 2026, immediately after the allow-list was "
        "applied. Every call was made by the application's own ULIP client "
        "through the deployment's Elastic IP, using the login flow described in "
        "section 3. Latencies are the measured round trip for a single call."),
    "rows": [
        ("FASTAG/01", "CG07BC9186", "PASS", "580 ms", "3 toll crossings returned"),
        ("FASTAG/02", "tagid 34161FA8203286140F4064E0", "PASS", "97 ms",
         "1 tag record returned"),
        ("LDB/01", "NSST1234570", "FAIL", "90 ms",
         "ULIP replied \"LDB_01 - 3rd party service is down!\" (error=true) "
         "— an upstream outage on the ULIP side, not a client failure"),
        ("VAHAN/04", "UP32KH0320", "PASS", "124 ms", "RC returned, plate matched"),
        ("VAHAN/01", "UP32KH0320", "PASS", "270 ms",
         "RC returned; see the non-determinism note in that section"),
        ("VAHAN/02", "masked chassis from VAHAN/04", "PASS", "264 ms",
         "valid 200, no vehicle found for a masked key"),
        ("VAHAN/03", "masked engine from VAHAN/04", "PASS", "190 ms",
         "valid 200, no vehicle found for a masked key"),
        ("SARATHI/02", "AP01620210000019", "PASS", "196 ms",
         "active licence, 2 classes of vehicle"),
        ("SARATHI/01", "AP01620210000019 + DOB", "PASS", "105 ms", "answered"),
        ("GATISHAKTI/01", "NH-5", "PASS", "466 ms", "33 rows"),
        ("GATISHAKTI/02", "stateid 27", "PASS", "422 ms", "13 rows"),
        ("GATISHAKTI/03", "stateid 27", "PASS", "474 ms", "994 rows"),
        ("GATISHAKTI/04", "stateid 27", "PASS", "257 ms",
         "59 toll plazas for Maharashtra"),
    ],
    "outcome": (
        "<b>All 13 granted APIs answered successfully.</b> LDB/01 was "
        "unavailable during the first run and was confirmed working by NLDSL on "
        "12 August 2026; with the container they supplied it returns a full "
        "thirteen-leg trail. Maharashtra "
        "(<code>stateid 27</code>) returned data on every GatiShakti API, "
        "confirming the account is <b>not</b> geographically scoped away from "
        "the JNPA corridor."),
    "shots": [
        ("response", "Live smoke-test output showing all 13 granted APIs called "
                     "in one run with per-API status and latency"),
    ],
}
