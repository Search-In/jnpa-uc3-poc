"""Request-body contract for POST /api/geo/zones/notify.

WHY THIS EXISTS
    A 422 was reported from the deployed dashboard when the operator pressed
    "Trigger" in Geo Analytics -> Vehicles in Zone:

        {"type": "dict_type", "loc": ["body"],
         "msg": "Input should be a valid dictionary"}

    The endpoint declares ``body: Dict[str, Any] = Body(...)`` (gateway/routers/
    geo.py), i.e. the WHOLE request body is the object -- not embedded under a
    key, not a model. These tests pin that contract from both directions:

      * the exact payload the browser sends MUST get past body validation, and
      * a non-dict body (array / double-stringified JSON / absent) MUST still be
        rejected, so the fix for the former can never be "loosen the model".

    ``dict_type`` on ``loc: ["body"]`` is reproducible ONLY from a non-dict body,
    so these cases document precisely what shapes produce the reported error.

These run in-process via Starlette's TestClient -- no docker stack, no DB.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unroutable DSN: the ledger write must fail fast rather than reach a real DB.
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("AUTH_ENABLED", "false")

from starlette.testclient import TestClient  # noqa: E402

# The exact body the dashboard sends (web/src/lib/api.ts :: geoNotifyZone).
BROWSER_PAYLOAD = {
    "vehicle_id": "MH06JK9371",
    "zone_id": "NPZ-GATE-NSICT",
    "entry_time": "2026-08-11T18:52:10.550621+00:00",
}
HEADERS = {"x-data-mode": "DEMO"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    from gateway import main as mainmod

    with TestClient(mainmod.app) as c:
        yield c


def _detail(response) -> object:
    try:
        return response.json().get("detail")
    except Exception:  # noqa: BLE001
        return response.text


def test_browser_payload_passes_body_validation(client: TestClient) -> None:
    """The real dashboard payload must never be rejected as a body-shape error.

    A 409 (``vehicle_not_in_zone`` / ``occupancy_changed``) is a legitimate
    BUSINESS outcome and proves the body parsed: the handler only reaches that
    check after the dict has been validated. What must never happen is a 422
    carrying ``dict_type``.
    """
    r = client.post("/api/geo/zones/notify", json=BROWSER_PAYLOAD, headers=HEADERS)

    assert r.status_code != 422, (
        f"the dashboard payload was rejected by body validation: {_detail(r)}")
    # It reached the handler's own logic.
    assert r.status_code in (200, 409), f"unexpected status {r.status_code}: {_detail(r)}"


def test_double_stringified_body_is_rejected(client: TestClient) -> None:
    """A JSON *string* (body stringified twice) reproduces the reported error.

    This is the shape that yields dict_type on loc ["body"]; it must keep being
    rejected rather than coerced.
    """
    r = client.post("/api/geo/zones/notify", json='{"vehicle_id":"x"}', headers=HEADERS)

    assert r.status_code == 422
    assert any(e.get("type") == "dict_type" for e in r.json()["detail"])


def test_array_body_is_rejected(client: TestClient) -> None:
    r = client.post("/api/geo/zones/notify", json=[BROWSER_PAYLOAD], headers=HEADERS)

    assert r.status_code == 422
    assert any(e.get("type") == "dict_type" for e in r.json()["detail"])


def test_absent_body_is_rejected(client: TestClient) -> None:
    r = client.post("/api/geo/zones/notify", headers=HEADERS)

    assert r.status_code == 422
    assert any(e.get("type") == "missing" for e in r.json()["detail"])


@pytest.mark.parametrize("body", [
    {},
    {"vehicle_id": "MH06JK9371"},
    {"vehicle_id": "MH06JK9371", "zone_id": "NPZ-GATE-NSICT"},
    {"vehicle_id": "", "zone_id": "", "entry_time": ""},
])
def test_incomplete_body_rejected_by_the_endpoints_own_guard(
    client: TestClient, body: dict,
) -> None:
    """A well-formed dict missing required keys is the handler's 422, not
    pydantic's -- the distinction the operator-facing error text relies on."""
    r = client.post("/api/geo/zones/notify", json=body, headers=HEADERS)

    assert r.status_code == 422
    assert _detail(r) == {"error": "vehicle_id_zone_id_and_entry_time_required"}
