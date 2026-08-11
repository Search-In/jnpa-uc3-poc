"""Contract tests for the 13 granted ULIP APIs.

Every fixture below is copied VERBATIM from the sample payloads in
``ulip-docs/*.pdf`` — that is the whole point. The live endpoint cannot be
reached until NLDSL whitelists the deployment's egress IP (it answers HTTP 412
for everyone until then), so the integration documents are the only contract we
have, and these tests are what pin the code to them.

Covered deliberately:
  * the SUCCESS shape of each API
  * the MISS shape of each API — every one of which arrives as HTTP **200**
    with an error marker buried in the body (VAHAN code 231, SARATHI
    errorcode -1, FASTag errCode 740 / respCode 239, GatiShakti empty ``data``).
    A miss that is mistaken for a success is the single most likely way this
    integration goes wrong, so each one is asserted to normalise to
    empty/None rather than to a half-populated record.
  * the 412 access-denied gate, which must surface as its own exception type
  * client-side request validation, which must reject before spending a call
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from integrations.ulip import (  # noqa: E402
    UlipAccessDenied,
    UlipClient,
    UlipInvalidRequest,
)
from integrations.ulip.records import dl_to_record, parse_date, rc_to_record  # noqa: E402
from integrations.ulip.schemas import (  # noqa: E402
    UlipEnvelope,
    normalize_container_events,
    normalize_dl,
    normalize_rc,
    normalize_road_network,
    normalize_tag_status,
    normalize_toll_plazas,
    normalize_vahan_xml,
    normalize_vehicle_events,
)


def env(payload: dict) -> UlipEnvelope:
    return UlipEnvelope.model_validate(payload)


def ok(response) -> dict:
    """The standard success envelope every ULIP API wraps its payload in."""
    return {"response": response, "error": "false", "code": "200",
            "message": "Success"}


# ===========================================================================
# VAHAN — doc §1.4 (XML) and §1.7 (JSON)
# ===========================================================================
VAHAN04_HIT = ok([{"response": {
    "stautsMessage": "OK", "stateCd": "UP", "rtoCd": "91",
    "rcRegnNo": "UP91L0001", "rcRegnDt": "26-Jan-2017",
    "rcChasiNo": "ME4JF509AH70*****", "rcEngNo": "JF50E760*****",
    "rcVhClassDesc": "M-Cycle/Scooter", "rcOwnerName": "R***L K***R",
    "rcFuelDesc": "PETROL", "rcFitUpto": "25-Jan-2032",
    "rcMakerModel": "HONDA ACTIVA", "rcBlacklistStatus": "",
    "rcRegisteredAt": "HAMIRPUR(UP), Uttar Pradesh",
}, "responseStatus": "SUCCESS"}])

# doc §1.7 — a vehicle VAHAN does not know, still HTTP 200.
VAHAN_MISS = ok([{"response": None, "responseStatus": "ERROR",
                  "message": {"id": None, "code": "231", "params": None,
                              "text": "Vehicle Details not Found"}}])

VAHAN01_XML = ok([{"response": (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><VehicleDetails>'
    "<stautsMessage>OK</stautsMessage><rc_regn_no>UP91L0001</rc_regn_no>"
    "<rc_regn_dt>26-Jan-2017</rc_regn_dt><rc_owner_name>R***L K***R</rc_owner_name>"
    "<rc_vh_class_desc>M-Cycle/Scooter</rc_vh_class_desc>"
    "<rc_fuel_desc>PETROL</rc_fuel_desc><rc_chasi_no>ME4JF509AH70*****</rc_chasi_no>"
    "<rto_cd>91</rto_cd><rc_status>ACTIVE</rc_status></VehicleDetails>"
), "responseStatus": "SUCCESS", "message": None}])


def test_vahan04_json_normalises():
    fields = normalize_rc(env(VAHAN04_HIT))
    assert fields["rc_number"] == "UP91L0001"
    assert fields["vehicle_class"] == "M-Cycle/Scooter"
    assert fields["chassis_number"] == "ME4JF509AH70*****"


def test_vahan01_xml_normalises_to_the_same_shape():
    """The XML and JSON variants must be interchangeable upstream — that is
    what lets VAHAN/01 act as the retry rung behind VAHAN/04."""
    json_fields = normalize_rc(env(VAHAN04_HIT))
    xml_fields = normalize_vahan_xml(env(VAHAN01_XML))
    for key in ("rc_number", "owner_name", "vehicle_class", "fuel_type",
                "registration_date", "chassis_number", "rto_code"):
        assert xml_fields[key] == json_fields[key], key


def test_vahan_miss_is_none_not_a_partial_record():
    assert normalize_rc(env(VAHAN_MISS)) is None
    assert rc_to_record(normalize_rc(env(VAHAN_MISS)) or {}) is None


def test_vahan_record_does_not_mask_an_already_masked_owner_name():
    """VAHAN masks rc_owner_name for every user. Masking it again would turn
    ``R***L K***R`` into ``R*********`` and destroy the remaining signal."""
    record = rc_to_record(normalize_rc(env(VAHAN04_HIT)))
    assert record.owner_name_masked == "R***L K***R"


def test_vahan_record_parses_the_doc_date_format():
    record = rc_to_record(normalize_rc(env(VAHAN04_HIT)))
    assert record.registration_date.isoformat() == "2017-01-26"
    assert record.fitness_valid_to.isoformat() == "2032-01-25"


def test_parse_date_drops_vahans_invalid_trailing_clock():
    """rcStatusAsOn is "03-Feb-2026 05:02:62604" — the seconds field is not a
    real time, so only the date part may be trusted."""
    assert parse_date("03-Feb-2026 05:02:62604").isoformat() == "2026-02-03"
    assert parse_date("not a date") is None


# ===========================================================================
# SARATHI — doc §1.4
# ===========================================================================
SARATHI02_HIT = ok([{"response": {"DLinformation": {
    "Classofcovs": [
        {"CovDiscription": "Motor Cycle with Gear(Non Transport)", "CovCode": 3},
        {"CovDiscription": "LIGHT MOTOR VEHICLE", "CovCode": 4},
        {"CovDiscription": "Transport Vehicle-M/HMV (Goods and Passenger)",
         "CovCode": 10},
    ],
    "NonTransportValidityTodate": "27-04-2031",
    "TransportValidityTodate": "16-02-2028",
    "DL_Holder_FullName": "RAMSANEHI", "DL_status": "Active.",
}}, "responseStatus": "SUCCESS", "message": None}])

SARATHI02_MISS = ok([{"response": {
    "errormessage": "Details not Found For Given DLNumber", "errorcode": -1,
}, "responseStatus": "SUCCESS", "message": None}])


def test_sarathi_hit_normalises():
    fields = normalize_dl(env(SARATHI02_HIT))
    assert fields["holder_name"] == "RAMSANEHI"
    assert len(fields["vehicle_classes"]) == 3


def test_sarathi_miss_is_none():
    assert normalize_dl(env(SARATHI02_MISS)) is None


def test_sarathi_record_prefers_the_transport_validity():
    """A port-corridor driver is a transport driver — the non-transport date
    would over-state how long the licence is usable here."""
    record = dl_to_record("AP01620210000019", normalize_dl(env(SARATHI02_HIT)))
    assert record.valid_to.isoformat() == "2028-02-16"
    assert record.blacklist_status.value == "CLEAR"


def test_sarathi_non_active_status_is_not_admitted():
    """Anything that is not clearly active must fail closed."""
    fields = dict(normalize_dl(env(SARATHI02_HIT)))
    fields["dl_status"] = "Suspended"
    record = dl_to_record("AP01620210000019", fields)
    assert record.blacklist_status.value == "BLACKLISTED"


# ===========================================================================
# FASTag — doc §1.3 / §1.4
# ===========================================================================
FASTAG01_HIT = ok([{"response": {
    "result": "SUCCESS", "respCode": "000", "ts": "2021-11-01T19:20:47",
    "vehicle": {"errCode": "000", "vehltxnList": {
        "totalTagsInMsg": "2", "msgNum": "1", "totalTagsInresponse": "2",
        "totalMsg": "1", "txn": [
            {"readerReadTime": "2021-10-30 12:26:09.0", "seqNo": "68d47e2d",
             "laneDirection": "N", "tollPlazaGeocode": "11.0001,11.0001",
             "tollPlazaName": "GMR Chillakallu Toll Plaza",
             "vehicleType": "VC7", "vehicleRegNo": "MH19JK3923"},
            {"readerReadTime": "2021-10-30 12:41:23.0", "seqNo": "6361cf5f",
             "laneDirection": "N", "tollPlazaGeocode": "11.0001,11.0001",
             "tollPlazaName": "GMR Chillakallu Toll Plaza",
             "vehicleType": "VC7", "vehicleRegNo": "MH19JK3923"},
        ]}}}, "responseStatus": "SUCCESS"}])

FASTAG01_MISS = ok([{"response": {
    "result": "FAILURE", "respCode": "000", "ts": "2022-02-15T11:34:31",
    "vehicle": {"errCode": "740"}}, "responseStatus": "SUCCESS"}])

FASTAG02_HIT = ok([{"response": {
    "result": "SUCCESS", "successReqCnt": "1", "totReqCnt": "1",
    "respCode": "000", "ts": "2024-03-14T14:07:48",
    "vehicle": {"errCode": "000", "vehicledetails": [
        {"detail": [
            {"name": "TAGID", "value": "34161FA820328972020FB320"},
            {"name": "REGNUMBER", "value": "MP09HF4987"},
            {"name": "VEHICLECLASS", "value": "VC11"},
            {"name": "TAGSTATUS", "value": "A"},
            {"name": "BANKID", "value": "607417"},
            {"name": "COMVEHICLE", "value": "T"},
        ]},
        {"detail": [
            {"name": "TAGID", "value": "34161FA8203289720E14EEA0"},
            {"name": "REGNUMBER", "value": "MP09HF4987"},
            {"name": "TAGSTATUS", "value": "A"},
        ]},
    ]}}, "responseStatus": "SUCCESS"}])


def test_fastag01_crossings_normalise_with_geocode():
    events = normalize_vehicle_events(env(FASTAG01_HIT), "MH19JK3923")
    assert len(events) == 2
    assert events[0]["location"] == "GMR Chillakallu Toll Plaza"
    assert events[0]["latitude"] == 11.0001 and events[0]["longitude"] == 11.0001


def test_fastag01_unknown_vehicle_yields_no_events():
    """errCode 740 inside a 200 — an unknown vehicle, or simply nothing in the
    72-hour retention window. Either way: zero events, not a failure."""
    assert normalize_vehicle_events(env(FASTAG01_MISS), "MH19JK3923") == []


def test_fastag02_returns_every_tag_for_a_vehicle():
    """A vehicle keeps its old tags after a re-issue, so this is a list."""
    tags = normalize_tag_status(env(FASTAG02_HIT))
    assert len(tags) == 2
    assert tags[0]["tagid"] == "34161FA820328972020FB320"
    assert tags[0]["vehicleclass"] == "VC11"


def test_fastag_transactions_mapper_survives_the_real_envelope():
    """The DTO is fed ``vehltxnList.txn[]`` nested under a LIST-valued
    ``response``; without the adapter it would validate an empty batch and
    report a successful zero-crossing fetch."""
    from services.fastag.mappers import map_fastag_transactions

    mapped = map_fastag_transactions(FASTAG01_HIT, client_id="test")
    assert mapped["status"] == "success"
    assert len(mapped["db"]) == 2
    row = mapped["db"][0]
    assert row["rc_number"] == "MH19JK3923"           # from vehicleRegNo
    assert row["seq_no"] == "68d47e2d"                # string, never coerced
    assert row["transaction_date_time"] is not None   # from readerReadTime


# ===========================================================================
# LDB — doc §1.3
# ===========================================================================
LDB_HIT = ok([{"response": {"eximContainerTrail": {
    "cntrDetail": {"cntrno": "NSST1234570", "cntrsize": 40, "isocode": "22G3"},
    "trackLog": [
        {"serialno": 1, "eventname": "PORT OUT",
         "currentlocation": "Raigad/Nhava Sheva",
         "timestamptimezone": "2023-03-29 12:10:09", "timezoneabvr": "IST",
         "latitude": 18.950149, "longitude": 72.95123,
         "containernumber": "NSST1234570", "transportmode": "TRUCK",
         "type": "I", "isempty": "Y"},
        {"serialno": 2, "eventname": "PORT IN",
         "currentlocation": "Raigad/Nhava Sheva",
         "timestamptimezone": "2023-03-28 10:10:08", "timezoneabvr": "IST",
         "latitude": 18.950149, "longitude": 72.95123,
         "containernumber": "NSST1234570", "transportmode": "VESSEL",
         "type": "I", "isempty": "Y"},
    ]}}, "responseStatus": "SUCCESS"}])

LDB_MISS = ok([{"responseStatus": "FAILURE",
                "message": {"text": "Container details not found for the given input"}}])


def test_ldb_tracklog_normalises_with_coordinates():
    """Regression: the alias lists must include LDB's all-lowercase field
    names (``eventname``/``currentlocation``/``timestamptimezone``). With only
    the camelCase aliases every trackLog entry failed the guard and vanished,
    so a tracked container silently reported zero movements."""
    events = normalize_container_events(env(LDB_HIT), "NSST1234570")
    assert len(events) == 2
    assert {e["detail"]["eventname"] for e in events} == {"PORT IN", "PORT OUT"}
    assert events[0]["latitude"] == 18.950149
    assert events[0]["event_ts"].startswith("2023-03-29")


def test_ldb_miss_yields_no_events():
    assert normalize_container_events(env(LDB_MISS), "NSST1234570") == []


# ===========================================================================
# GATISHAKTI — doc §1.5 / §1.6
# ===========================================================================
GS_HIT = ok([{"response": {"data": [
    {"vname": "Katnai", "lat": "24.4059522538221785",
     "lon": "88.0467862324777286", "nhno": "NH-12"},
    {"vname": "Malancha (P)", "lat": "22.9036144349493753",
     "lon": "88.4324715905935221"},
], "Result": True}, "responseStatus": "SUCCESS"}])

GS_EMPTY = ok([{"response": {"data": [], "Result": True},
                "responseStatus": "SUCCESS"}])


def test_gatishakti_toll_plazas_normalise():
    plazas = normalize_toll_plazas(env(GS_HIT), 27)
    assert len(plazas) == 2
    assert plazas[0]["state_id"] == "27"
    # Coordinates arrive as high-precision strings and must become floats.
    assert isinstance(plazas[0]["latitude"], float)


def test_gatishakti_unknown_state_is_empty_not_an_error():
    assert normalize_toll_plazas(env(GS_EMPTY), 99) == []
    assert normalize_road_network(env(GS_EMPTY), state_id=99) == []


# ===========================================================================
# Client behaviour: the 412 gate and request pre-validation
# ===========================================================================
@pytest.mark.asyncio
async def test_login_412_raises_access_denied_not_auth_error():
    """The whole reason UlipAccessDenied exists: NLDSL returns this same 412
    for a NONEXISTENT username, so it says nothing about the credential. It
    must never be reported as "check the password"."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, json={
            "response": "Access denied Please contact ULIP support!",
            "error": None, "code": "412 PRECONDITION_FAILED", "message": "Failed"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = UlipClient(api_url="https://ulip.test/ulip/v1.0.0",
                            client_id="u", client_secret="p",
                            api_key="", http_client=http)
        with pytest.raises(UlipAccessDenied) as exc:
            await client.fetch_vehicle_by_rc("UP32KH0320")
    assert "egress IP" in str(exc.value)


@pytest.mark.asyncio
async def test_access_denied_message_never_leaks_the_credential():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, json={"response": "Access denied", "code": "412"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = UlipClient(api_url="https://ulip.test/ulip/v1.0.0",
                            client_id="rtoj_user", client_secret="s3cr3t-pw",
                            api_key="", http_client=http)
        with pytest.raises(UlipAccessDenied) as exc:
            await client.fetch_dl("AP01620210000019")
    assert "s3cr3t-pw" not in str(exc.value)


@pytest.mark.asyncio
async def test_fastag02_rejects_both_identifiers_before_calling():
    """The doc is explicit that supplying both answers respCode 239 — a wasted
    call against a rate-limited subscription."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=ok([]))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = UlipClient(api_url="https://ulip.test/ulip/v1.0.0",
                            api_key="static-token", http_client=http)
        with pytest.raises(UlipInvalidRequest):
            await client.fetch_tag_status(vehicle_number="MP09HF4987",
                                          tag_id="34161FA8203286140F4064E0")
        with pytest.raises(UlipInvalidRequest):
            await client.fetch_tag_status()
    assert called is False, "neither invalid request may reach the network"


@pytest.mark.asyncio
async def test_malformed_arguments_are_rejected_before_calling():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=ok([]))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = UlipClient(api_url="https://ulip.test/ulip/v1.0.0",
                            api_key="static-token", http_client=http)
        with pytest.raises(UlipInvalidRequest):
            await client.fetch_nh_road("not-an-nh-number")
        with pytest.raises(UlipInvalidRequest):
            await client.fetch_toll_plazas("Maharashtra")   # needs the LGD code
        with pytest.raises(UlipInvalidRequest):
            await client.fetch_dl_with_dob("AP01620210000019", "26-05-1987")
    assert called is False


def test_every_granted_api_has_a_configured_path():
    """The 13 granted APIs — and deliberately NOT the four NLDSL documents but
    has not granted (VAHAN/05, VAHAN/06, GATISHAKTI/05, CFSICD/01)."""
    client = UlipClient(api_key="x")
    granted = {
        "FASTAG": "FASTAG/01", "FASTAG_TAG": "FASTAG/02", "LDB": "LDB/01",
        "VAHAN_RC": "VAHAN/04", "VAHAN_RC_XML": "VAHAN/01",
        "VAHAN_CHASSIS": "VAHAN/02", "VAHAN_ENGINE": "VAHAN/03",
        "SARATHI_DL": "SARATHI/02", "SARATHI_DL_DOB": "SARATHI/01",
        "GS_NH_ROAD": "GATISHAKTI/01", "GS_STATE_ROADS": "GATISHAKTI/02",
        "GS_ROAD_POINTS": "GATISHAKTI/03", "GS_TOLL_PLAZAS": "GATISHAKTI/04",
    }
    assert len(granted) == 13
    for key, path in granted.items():
        assert client.api_path(key) == path
    assert set(client.api_paths) == set(granted)


def test_api_paths_are_env_overridable(monkeypatch):
    """NLDSL versions these paths independently of the account, so a bumped
    API must never need a code change."""
    monkeypatch.setenv("ULIP_VAHAN_RC_API", "VAHAN/07")
    client = UlipClient(api_key="x")
    assert client.api_path("VAHAN_RC") == "VAHAN/07"


# ===========================================================================
# LIVE shapes — recorded from ULIP staging on 2026-08-11, after NLDSL
# whitelisted the deployment's egress IP.
#
# These differ materially from the integration documents' samples, and each
# difference below was a live defect in this integration before it was
# recorded here. Fixtures come from real answers, not from the PDFs.
# ===========================================================================
GS04_LIVE = ok([{"response": {"data": [
    {"plaza_name": "(Planned) Tilasar", "tollplazal": 21.173482,
     "tollplaz_1": 74.005793, "nooflanes": "4L", "nhno_new": "NH- 53",
     "nearesthos": None, "tollcollec": None,
     "project_na": "Fagne-MH/GJ Border"},
    {"plaza_name": "Nandgaon Peth", "tollplazal": 20.951008,
     "tollplaz_1": 77.788359, "nooflanes": "4L", "nhno_new": "NH- 53"},
], "Result": True}, "responseStatus": "SUCCESS"}])

GS01_LIVE = ok([{"response": {"data": [
    {"road_name": "NH-5", "gis_length": 11.0479145145,
     "road_type": "National Highway", "lane_statu": "6L",
     "state_ut": "Haryana"},
], "Result": True}, "responseStatus": "SUCCESS"}])

GS02_LIVE = ok([{"response": {"data": [
    {"infrastr_s": "MAHARASHTRA", "infrastr_a": "C-65, MIDC,MIRJOLE  RATNAGIRI",
     "infrastr_n": "FSD RATNAGIRI", "type_owner": "Own",
     "storage_ca": "11664", "type_infra": "Covered",
     "infrastr_v": "MIDC ,MIRJOLE"},
], "Result": True}, "responseStatus": "SUCCESS"}])

GS03_LIVE = ok([{"response": {"data": [
    {"st_name": "MAHARASHTRA", "dist_name": "Latur", "sub_dist": "Nilanga",
     "vname": "Aurad (sha)",
     "park_name": "Nilanga Co-Op. Industrial Estate Ltd.",
     "land_cat": "", "land_avail": 0.0, "park_type": "Other",
     "lat": "18.069549104620279", "lon": "76.7894777008746985"},
], "Result": True}, "responseStatus": "SUCCESS"}])


def test_gatishakti04_live_field_names_yield_plazas():
    """Regression: the live rows use ``plaza_name`` / ``tollplazal`` /
    ``tollplaz_1``, not the document's ``vname`` / ``lat`` / ``lon``. Matching
    only the documented names returned ZERO plazas for every state, which read
    as "this state has no toll plazas" rather than as a mapping bug."""
    plazas = normalize_toll_plazas(env(GS04_LIVE), 27)
    assert len(plazas) == 2
    assert plazas[0]["name"] == "(Planned) Tilasar"
    assert plazas[0]["latitude"] == 21.173482
    assert plazas[0]["longitude"] == 74.005793
    assert plazas[0]["nh_no"] == "NH- 53"
    assert plazas[0]["state_id"] == "27"


def test_gatishakti_01_02_03_do_not_share_a_schema():
    """The three 'road' APIs return three unrelated datasets on staging:
    highway segments WITHOUT coordinates, food-storage depots, and industrial
    parks. Each must still yield a usable label, and the raw row must survive
    in ``detail`` because no single mapping fits all three."""
    road = normalize_road_network(env(GS01_LIVE), nh_no="NH-5")
    assert road[0]["name"] == "NH-5"
    assert road[0]["latitude"] is None and road[0]["longitude"] is None
    assert road[0]["detail"]["lane_statu"] == "6L"

    depot = normalize_road_network(env(GS02_LIVE), state_id=27)
    assert depot[0]["name"] == "FSD RATNAGIRI"
    assert depot[0]["detail"]["storage_ca"] == "11664"

    park = normalize_road_network(env(GS03_LIVE), state_id=27)
    assert park[0]["name"] == "Nilanga Co-Op. Industrial Estate Ltd."
    assert park[0]["latitude"] == 18.069549104620279


def test_sarathi_live_empty_transport_validity_falls_back():
    """A licence with no transport endorsement returns
    ``TransportValidityTodate: ""``. The empty string must fall through to the
    non-transport date rather than leaving the record with no expiry."""
    live = ok([{"response": {"DLinformation": {
        "Classofcovs": [{"CovDiscription": "LIGHT MOTOR VEHICLE", "CovCode": 4}],
        "NonTransportValidityTodate": "03-12-2031",
        "TransportValidityTodate": "",
        "DL_Holder_FullName": "MAJJI KESAVARAO", "DL_status": "Active."}},
        "responseStatus": "SUCCESS", "message": None}])
    record = dl_to_record("AP01620210000019", normalize_dl(env(live)))
    assert record.valid_to.isoformat() == "2031-12-03"


@pytest.mark.asyncio
async def test_every_request_sends_an_explicit_json_accept_header():
    """ULIP answers a bodiless HTTP 400 to httpx's default ``Accept: */*``.
    Verified against staging: same payload, ``*/*`` -> 400,
    ``application/json`` -> 200. Login is included — it is the first call
    made, so getting this wrong locks the whole integration out."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept", ""))
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json={"response": {"id": "tok"}})
        return httpx.Response(200, json=ok([]))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = UlipClient(api_url="https://ulip.test/ulip/v1.0.0",
                            client_id="u", client_secret="p", http_client=http)
        await client.fetch_vehicle_by_rc("UP32KH0320")
    assert seen and all(a == "application/json" for a in seen), seen


@pytest.mark.asyncio
async def test_upstream_outage_reports_the_reason_not_message_none():
    """LDB's upstream was down during live testing and ULIP reported it as
    ``{"response": [{"response": "LDB_01 - 3rd party service is down!"}],
    "error": "true", "message": null}``. Surfacing ``message=None`` sent the
    reader hunting for a bug in our own code, so the reason is dug out."""
    body = {"response": [{"response": "LDB_01 - 3rd party service is down!",
                          "responseStatus": "ERROR"}],
            "error": "true", "code": "200", "message": None}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = UlipClient(api_url="https://ulip.test/ulip/v1.0.0",
                            api_key="static", http_client=http)
        with pytest.raises(Exception) as exc:
            await client.fetch_container_tracking("NSST1234570")
    assert "3rd party service is down" in str(exc.value)


# ===========================================================================
# SARATHI/01 — response schema supplied by NLDSL on 2026-08-11 (the .docx that
# was missing from the original integration pack). It shares NO field names
# with SARATHI/02, so it needs its own mapping onto the same flat shape.
# ===========================================================================
SARATHI01_HIT = ok([{"response": {"dldetobj": [{
    "dlobj": {
        "dlLicno": "GJ04 20120005008  ", "dlStatus": "Active",
        "dlIssuedt": "2012-03-07", "dlTrValdtoDt": "2022-08-09",
        "dlNtValdtoDt": "2032-03-06", "dlRtoCode": "GJ33",
        "stateName": "Gujarat", "statecd": "GJ",
    },
    "dlcovs": [
        {"covabbrv": "MCWG  ", "covdesc": "Motor Cycle with Gear(Non Transport)"},
        {"covabbrv": "TRANS", "covdesc": "Transport Vehicle-M/HMV (Goods & Passenger)"},
        {"covabbrv": "LMV   ", "covdesc": "LIGHT MOTOR VEHICLE"},
    ],
    "bioObj": {
        "bioNatName": "MAHESHKUMAR  GOHIL",
        "bioFullName": "M*********R G***L",
        "bioDob": "1987-05-26", "bioGenderDesc": "Male        ",
        "bioSwdFullName": "RAMJIBHAI  GOHIL", "biPhoto": "/9j/4AAQSkZJRgABAQ",
    },
}]}, "responseStatus": "SUCCESS", "message": None}])


def test_sarathi01_maps_onto_the_same_shape_as_sarathi02():
    """/01 keeps the licence in ``dlobj``, the classes in ``dlcovs`` and the
    holder in a ``bio`` block — no field name in common with /02. Both must
    normalise to one shape or /01 cannot stand in for /02."""
    fields = normalize_dl(env(SARATHI01_HIT))
    assert fields["dl_status"] == "Active"
    assert fields["transport_valid_to"] == "2022-08-09"
    assert fields["non_transport_valid_to"] == "2032-03-06"
    assert len(fields["vehicle_classes"]) == 3
    assert "LIGHT MOTOR VEHICLE" in fields["vehicle_classes"]


def test_sarathi01_prefers_the_unmasked_holder_name():
    """``bioFullName`` is masked (``M*********R G***L``) but ``bioNatName``
    carries the real name — the only granted API that returns one unmasked.
    Driver enrolment and police-report surfaces need it."""
    fields = normalize_dl(env(SARATHI01_HIT))
    assert fields["holder_name"] == "MAHESHKUMAR  GOHIL"
    assert fields["photo_base64"].startswith("/9j/")


def test_sarathi01_populates_the_fields_sarathi02_cannot():
    """Issue date, state and RTO are absent from /02 and present in /01, and
    /01's dates are ISO where /02's are DD-MM-YYYY."""
    record = dl_to_record("GJ0420120005008", normalize_dl(env(SARATHI01_HIT)))
    assert record.date_of_issue.isoformat() == "2012-03-07"
    assert record.valid_to.isoformat() == "2022-08-09"   # transport wins
    assert record.state == "Gujarat"
    assert record.rto_code == "GJ33"


def test_gatishakti_queries_never_bind_an_untyped_null_comparison():
    """Regression: ``WHERE (:state_id IS NULL OR state_id = :state_id)`` makes
    asyncpg raise ``AmbiguousParameterError: could not determine data type of
    parameter $1``. Because the repository swallows query errors to mean "the
    DATABASE rung is empty", every GatiShakti list endpoint answered
    ``path: FALLBACK, count: 0`` while the rows sat in the table — a silent
    failure indistinguishable from "this state has no toll plazas". Any
    ``:param IS NULL`` must be cast.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "services" / "gatishakti" / "repository.py").read_text()
    bare = re.findall(r":(\w+)\s+IS\s+NULL", src)
    assert not bare, f"uncast NULL-compared bind parameters: {bare}"
