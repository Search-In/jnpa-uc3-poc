"""PII masking (DPDP) — unit tests for the maskers + API tests for the gate.

Covers the audit finding that /api/drivers/master served ~31.8k real licence
numbers and dates of birth unmasked and unauthenticated.

Two layers:
  * ``jnpa_shared.pii``  — pure value/structure masking (no framework).
  * ``gateway.pii``      — WHO gets cleartext (role gate, fails closed).
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from jnpa_shared.pii import (
    mask_address,
    mask_aadhaar,
    mask_dob,
    mask_email,
    mask_licence,
    mask_mobile,
    mask_payload,
    masking_enabled,
    unmask_roles,
)


# =========================================================== value maskers
class TestMaskLicence:
    def test_matches_the_documented_example(self):
        # The exact transformation named in the remediation brief.
        assert mask_licence("MH23 20170012229") == "MH23******229"

    def test_keeps_rto_head_and_short_tail(self):
        assert mask_licence("MH1220110012345") == "MH12******345"

    def test_mask_is_fixed_width_so_length_does_not_leak(self):
        short = mask_licence("MH12345678901")
        long = mask_licence("MH12345678901234567890")
        assert short.count("*") == long.count("*") == 6

    def test_too_short_to_split_is_fully_masked(self):
        assert mask_licence("MH1234") == "******"

    def test_none_and_empty_pass_through(self):
        assert mask_licence(None) is None
        assert mask_licence("") == ""

    def test_idempotent(self):
        once = mask_licence("MH23 20170012229")
        assert mask_licence(once) == once


class TestMaskDob:
    def test_date_object_reduced_to_year(self):
        assert mask_dob(dt.date(1994, 9, 10)) == "1994-**-**"

    def test_datetime_reduced_to_year(self):
        assert mask_dob(dt.datetime(1994, 9, 10, 6, 30)) == "1994-**-**"

    def test_iso_string_reduced_to_year(self):
        assert mask_dob("1994-09-10") == "1994-**-**"

    def test_unparseable_is_fully_masked(self):
        assert mask_dob("sometime in 94") == "******"

    def test_none_passes_through(self):
        assert mask_dob(None) is None

    def test_idempotent(self):
        assert mask_dob("1994-**-**") == "1994-**-**"


class TestOtherMaskers:
    def test_mobile_keeps_last_three(self):
        assert mask_mobile("9876543210") == "******210"

    def test_mobile_strips_formatting_before_masking(self):
        assert mask_mobile("+91 98765-43210") == "******210"

    def test_email_keeps_domain_not_person(self):
        assert mask_email("ravi.kumar@acme.co.in") == "r******@acme.co.in"

    def test_email_without_at_is_fully_masked(self):
        assert mask_email("not-an-email") == "******"

    def test_address_keeps_only_city_tail(self):
        assert mask_address("Plot 14, MIDC Taloja, Panvel 410208") == "******, Panvel 410208"

    def test_single_line_address_fully_masked(self):
        assert mask_address("Plot 14 MIDC Taloja") == "******"

    def test_aadhaar_keeps_uidai_convention_last_four(self):
        assert mask_aadhaar("123412341234") == "******1234"


# ====================================================== structure walker
class TestMaskPayload:
    def test_masks_every_known_field_in_a_flat_row(self):
        out = mask_payload({
            "licence_no": "MH23 20170012229",
            "dob": "1994-09-10",
            "mobile": "9876543210",
            "email": "a@b.com",
            "address": "Plot 1, Panvel",
        })
        assert out["licence_no"] == "MH23******229"
        assert out["dob"] == "1994-**-**"
        assert out["mobile"] == "******210"
        assert out["email"] == "a******@b.com"
        assert out["address"] == "******, Panvel"

    def test_non_pii_fields_are_untouched(self):
        row = {"name": "AABHIMAN BATULE", "pdp_status": "EXPIRED",
               "company_name": "NITIN TRANSPORT CO", "id": 15354,
               "licence_valid_to": "2030-01-01"}
        assert mask_payload(row) == row

    def test_walks_nested_envelopes(self):
        out = mask_payload({
            "driver": {"dob": "1994-09-10"},
            "licence": {"licence_no": "MH23 20170012229"},
        })
        assert out["driver"]["dob"] == "1994-**-**"
        assert out["licence"]["licence_no"] == "MH23******229"

    def test_walks_lists_of_rows(self):
        out = mask_payload({"items": [{"licence_no": "MH23 20170012229"},
                                      {"licence_no": "MH12 20110012345"}]})
        assert [i["licence_no"] for i in out["items"]] == ["MH23******229", "MH12******345"]

    def test_input_is_never_mutated(self):
        # A cached row shared between an entitled and a non-entitled caller must
        # not be corrupted by whichever is served first.
        src = {"licence_no": "MH23 20170012229", "nested": {"dob": "1994-09-10"}}
        mask_payload(src)
        assert src["licence_no"] == "MH23 20170012229"
        assert src["nested"]["dob"] == "1994-09-10"

    def test_licence_aliases_all_covered(self):
        for field in ("licence_number", "licence_no", "license_no", "driver_licence",
                      "licence_no_norm", "dl_no"):
            out = mask_payload({field: "MH23 20170012229"})
            assert out[field] == "MH23******229", field

    def test_prehashed_aadhaar_masked_column_is_left_alone(self):
        # core.driver_identity.aadhaar_masked is already masked at rest.
        assert mask_payload({"aadhaar_masked": "XXXX1234"}) == {"aadhaar_masked": "XXXX1234"}

    def test_scalars_and_unknown_types_pass_through(self):
        assert mask_payload(42) == 42
        assert mask_payload("plain") == "plain"
        assert mask_payload(None) is None


# ============================================== config / entitlement rules
class TestConfig:
    def test_masking_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("PII_MASKING_ENABLED", raising=False)
        assert masking_enabled() is True

    def test_masking_is_opt_out_not_opt_in(self, monkeypatch):
        # A typo must keep masking ON — fail closed.
        monkeypatch.setenv("PII_MASKING_ENABLED", "flase")
        assert masking_enabled() is True
        monkeypatch.setenv("PII_MASKING_ENABLED", "false")
        assert masking_enabled() is False

    def test_default_unmask_roles(self, monkeypatch):
        monkeypatch.delenv("PII_UNMASK_ROLES", raising=False)
        assert unmask_roles() == frozenset({"DTCCC_ADMIN", "CUSTOMS"})

    def test_empty_unmask_roles_means_nobody(self, monkeypatch):
        monkeypatch.setenv("PII_UNMASK_ROLES", "")
        assert unmask_roles() == frozenset()


# ================================================== gateway request gate
def _app():
    """Minimal app exposing the PII gate, so the test does not need the full
    gateway (DB, Kafka, MinIO) just to assert the entitlement rule."""
    from gateway.pii import mask_for_request

    app = FastAPI()
    row = {"licence_no": "MH23 20170012229", "dob": "1994-09-10", "name": "A B"}

    @app.get("/probe")
    async def probe(request: Request):
        return mask_for_request(request, dict(row))

    return app


class _P:
    def __init__(self, role):
        self.role = role
        self.sub = "test"
        self.device_id = None


@pytest.fixture(autouse=True)
def _clean_pii_env(monkeypatch):
    monkeypatch.delenv("PII_MASKING_ENABLED", raising=False)
    monkeypatch.delenv("PII_UNMASK_ROLES", raising=False)


def _with_principal(app, principal):
    """Install middleware that stands in for gateway.auth's principal attach."""
    @app.middleware("http")
    async def _attach(request: Request, call_next):
        if principal is not None:
            request.state.principal = principal
        return await call_next(request)
    return app


def test_no_principal_is_masked_this_is_the_audit_finding():
    # AUTH_ENABLED=false leaves no principal. Before the fix this served the real
    # licence number to any unauthenticated caller.
    with TestClient(_app()) as c:
        body = c.get("/probe").json()
    assert body["licence_no"] == "MH23******229"
    assert body["dob"] == "1994-**-**"


def test_driver_role_is_masked():
    with TestClient(_with_principal(_app(), _P("DRIVER"))) as c:
        body = c.get("/probe").json()
    assert body["licence_no"] == "MH23******229"


def test_traffic_police_role_is_masked():
    with TestClient(_with_principal(_app(), _P("TRAFFIC_POLICE"))) as c:
        body = c.get("/probe").json()
    assert body["licence_no"] == "MH23******229"


@pytest.mark.parametrize("role", ["DTCCC_ADMIN", "CUSTOMS"])
def test_entitled_roles_see_cleartext(role):
    with TestClient(_with_principal(_app(), _P(role))) as c:
        body = c.get("/probe").json()
    assert body["licence_no"] == "MH23 20170012229"
    assert body["dob"] == "1994-09-10"


def test_unmask_roles_is_configurable(monkeypatch):
    monkeypatch.setenv("PII_UNMASK_ROLES", "TERMINAL_OPS")
    with TestClient(_with_principal(_app(), _P("TERMINAL_OPS"))) as c:
        assert c.get("/probe").json()["licence_no"] == "MH23 20170012229"
    with TestClient(_with_principal(_app(), _P("DTCCC_ADMIN"))) as c:
        assert c.get("/probe").json()["licence_no"] == "MH23******229"


def test_global_kill_switch_disables_masking(monkeypatch):
    monkeypatch.setenv("PII_MASKING_ENABLED", "false")
    with TestClient(_app()) as c:
        assert c.get("/probe").json()["licence_no"] == "MH23 20170012229"


def test_non_pii_fields_survive_masking():
    with TestClient(_app()) as c:
        assert c.get("/probe").json()["name"] == "A B"


# ============================================ router wiring (no DB needed)
def test_every_pii_serving_router_imports_the_gate():
    """Regression guard: a new endpoint on these routers must not bypass masking.

    Asserts the import exists rather than exercising the DB — the routers are
    RDS-backed and this suite runs without a database.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("gateway/routers/drivers_master.py",
                "gateway/routers/transporters.py",
                "gateway/routers/gate_documents.py",
                "gateway/routers/identity.py"):
        src = (root / rel).read_text()
        assert "mask_for_request" in src, f"{rel} does not route responses through the PII gate"
