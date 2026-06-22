"""Gateway auth + RBAC tests (Wave 3 — SEC-1, SEC-2).

Proves the flag-gated security layer:

  * AUTH disabled (default)  -> every route open (the demo / existing suite path).
  * AUTH enabled, no token   -> 401.
  * AUTH enabled, bad token  -> 401.
  * AUTH enabled, valid token but wrong role for the path -> 403.
  * AUTH enabled, valid token with a permitted role       -> passes the gate (200/normal).
  * Public paths (/healthz, /api/auth/*) stay open even with AUTH enabled.
  * Rate limiting returns 429 over budget.

Runs fully in-process via Starlette TestClient; no PyJWT / docker needed (auth.py
falls back to a stdlib HS256 verifier).
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

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from gateway.auth import Role, encode_token, roles_for_path  # noqa: E402

# Track clients + env so each test cleans up — AUTH_ENABLED must NEVER leak into
# the other gateway test modules (they assume auth off). See the autouse fixture.
_OPEN_CLIENTS: list[TestClient] = []


_AUTH_ENV_KEYS = (
    "AUTH_ENABLED",
    "AUTH_RATE_LIMIT_PER_MIN",
    "AUTH_JWT_SECRET",
    "AUTH_DEV_TOKENS",
    "APP_ENV",
)

# A non-default secret so the startup guard (validate_auth_config) is satisfied
# whenever a test enables auth in-process.
_TEST_SECRET = "test-secret-not-the-default-0123456789abcdef"


@pytest.fixture(autouse=True)
def _restore_env_and_clients():
    saved = {k: os.environ.get(k) for k in _AUTH_ENV_KEYS}
    try:
        yield
    finally:
        # Tear down any lifespan clients opened by the test.
        while _OPEN_CLIENTS:
            try:
                _OPEN_CLIENTS.pop().__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        # Restore env so AUTH_ENABLED does not bleed into other test modules, then
        # reload the gateway so its module-level state matches the restored env.
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib

        import gateway.main as mainmod

        importlib.reload(mainmod)


def _client(enabled: bool, rate_per_min: int = 10_000) -> TestClient:
    """A fresh app whose auth middleware is configured via env, then run through
    its lifespan so app.state.gw exists (routes that pass the gate can execute).

    The middleware reads AUTH_ENABLED at request time (no forced flag), so setting
    the env before reload is enough to flip enforcement deterministically. The
    autouse fixture restores the env + reloads the module after every test.
    """
    import importlib

    os.environ["AUTH_ENABLED"] = "true" if enabled else "false"
    os.environ["AUTH_RATE_LIMIT_PER_MIN"] = str(rate_per_min)
    # Provide a non-default secret so the startup guard accepts an auth-enabled
    # in-process app (the guard rejects the well-known default when enabled).
    os.environ["AUTH_JWT_SECRET"] = _TEST_SECRET

    import gateway.main as mainmod

    importlib.reload(mainmod)
    client = TestClient(mainmod.app)
    client.__enter__()  # run lifespan -> builds app.state.gw
    _OPEN_CLIENTS.append(client)
    return client


def _bearer(role: str, **kw) -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('tester', role, **kw)}"}


# --------------------------------------------------------------------------- policy
def test_policy_map_scoping():
    assert Role.TRAFFIC_POLICE.value in roles_for_path("/api/reports/police")
    assert Role.DRIVER.value not in roles_for_path("/api/reports/police")
    assert roles_for_path("/api/control/fault") == frozenset(
        {Role.JNPA_TRAFFIC.value, Role.DTCCC_ADMIN.value, Role.TERMINAL_OPS.value}
    )
    # Identity (DPDP-sensitive) is customs/admin only.
    assert roles_for_path("/api/identity/verify") == frozenset(
        {Role.CUSTOMS.value, Role.DTCCC_ADMIN.value}
    )
    # An unscoped operational path defaults to any authenticated role (6).
    assert len(roles_for_path("/api/traffic/snapshots")) == 6


# --------------------------------------------------------------------------- disabled
def test_auth_disabled_lets_everything_through():
    c = _client(enabled=False)
    # No token, yet a protected path is reachable (the gate is a no-op).
    r = c.get("/api/reports/police")
    assert r.status_code != 401 and r.status_code != 403


# --------------------------------------------------------------------------- enabled
def test_public_paths_open_when_enabled():
    c = _client(enabled=True)
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/auth/roles").status_code == 200


def test_missing_token_is_401():
    c = _client(enabled=True)
    r = c.get("/api/reports/police")
    assert r.status_code == 401


def test_bad_token_is_401():
    c = _client(enabled=True)
    r = c.get("/api/reports/police", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_wrong_role_is_403():
    c = _client(enabled=True)
    # A DRIVER may not read police reports.
    r = c.get("/api/reports/police", headers=_bearer(Role.DRIVER.value))
    assert r.status_code == 403


def test_right_role_passes_gate():
    c = _client(enabled=True)
    # TRAFFIC_POLICE is permitted on /api/reports — the auth gate must NOT block
    # it (the route may still 5xx if its DB is down, but never 401/403).
    r = c.get("/api/reports/police", headers=_bearer(Role.TRAFFIC_POLICE.value))
    assert r.status_code not in (401, 403)


def test_control_room_only_for_fault_control():
    c = _client(enabled=True)
    assert c.get("/api/control/fault", headers=_bearer(Role.DRIVER.value)).status_code == 403
    assert c.get(
        "/api/control/fault", headers=_bearer(Role.DTCCC_ADMIN.value)
    ).status_code not in (401, 403)


def test_login_mints_role_token():
    c = _client(enabled=True)
    r = c.post("/api/auth/login", json={"username": "police", "password": "police"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == Role.TRAFFIC_POLICE.value
    # The minted token works against a police-scoped route.
    r2 = c.get("/api/reports/police", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r2.status_code not in (401, 403)


def test_login_rejects_bad_credentials():
    c = _client(enabled=True)
    r = c.post("/api/auth/login", json={"username": "police", "password": "wrong"})
    assert r.status_code == 401


def test_rate_limit_returns_429():
    c = _client(enabled=True, rate_per_min=3)
    hdr = _bearer(Role.JNPA_TRAFFIC.value)
    codes = [c.get("/api/traffic/snapshots", headers=hdr).status_code for _ in range(8)]
    assert 429 in codes, f"expected a 429 once the bucket drains, got {codes}"


# --------------------------------------------------------------------------- DPDP (SEC-3)
def test_dpdp_default_request_is_synthetic_and_allowed():
    c = _client(enabled=False)  # focus on the DPDP guard, not the auth gate
    r = c.post("/api/identity/verify", json={"driver_id": "DRV-1001", "simulate": "genuine"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_synthetic"] is True
    assert body["purpose"] == "GATE_VERIFICATION"


def test_dpdp_unknown_purpose_rejected():
    c = _client(enabled=False)
    r = c.post(
        "/api/identity/verify",
        json={"driver_id": "DRV-1001", "purpose": "MARKETING"},
    )
    assert r.status_code == 400


def test_dpdp_real_biometrics_refused_by_default():
    c = _client(enabled=False)
    r = c.post(
        "/api/identity/verify",
        json={"driver_id": "DRV-1001", "is_synthetic": False},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- NOTIF-5
def test_alerts_carry_i18n_key():
    from gateway.routers.alerts import _decorate, _i18n_key

    out = _decorate({"kind": "WRONG_WAY", "id": "1"})
    assert out["i18n_key"] == "alertKind.WRONG_WAY"
    assert _i18n_key(None) == "alertKind.ALERT"


def test_alerts_role_filter_scopes_kinds():
    from gateway.routers.alerts import _role_can_see

    # Customs sees customs flags; a driver does not see wrong-way enforcement.
    assert _role_can_see("CUSTOMS", "CUSTOMS_FLAG") is True
    assert _role_can_see("DRIVER", "WRONG_WAY") is False
    assert _role_can_see("TRAFFIC_POLICE", "WRONG_WAY") is True
    # No role context => unfiltered (dashboard / auth-disabled).
    assert _role_can_see(None, "CUSTOMS_FLAG") is True
    # Unknown kind => visible to all.
    assert _role_can_see("DRIVER", "SOME_NEW_KIND") is True


def test_alert_ack_endpoint_degrades_gracefully():
    c = _client(enabled=False)
    # No live Postgres in the in-process suite -> persisted False, ack True, 200.
    r = c.post("/api/alerts/abc-123/ack")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "abc-123" and body["ack"] is True


# --------------------------------------------------------------------------- C1/C2/C3 startup guard
# These exercise validate_auth_config() directly (the function main.py calls at
# import to fail fast). Each sets the relevant env then asserts the guard raises
# AuthConfigError — i.e. the gateway process would refuse to start.
def _set_env(**kw) -> None:
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_startup_ok_for_local_development_defaults():
    from gateway.auth import validate_auth_config

    _set_env(APP_ENV="development", AUTH_ENABLED="false", AUTH_JWT_SECRET=None)
    # Local dev with enforcement off is a no-op (the demo / existing suite path).
    validate_auth_config()


def test_startup_fails_in_production_without_auth_enabled():
    # C1: staging/production may not start unauthenticated.
    from gateway.auth import AuthConfigError, validate_auth_config

    _set_env(APP_ENV="production", AUTH_ENABLED="false")
    with pytest.raises(AuthConfigError):
        validate_auth_config()


def test_startup_fails_when_secret_missing():
    # C2: AUTH_ENABLED=true but no secret -> fatal.
    from gateway.auth import AuthConfigError, validate_auth_config

    _set_env(APP_ENV="production", AUTH_ENABLED="true", AUTH_DEV_TOKENS="false",
             AUTH_JWT_SECRET=None)
    with pytest.raises(AuthConfigError):
        validate_auth_config()


def test_startup_fails_when_secret_is_default():
    # C2: AUTH_ENABLED=true but the well-known default secret -> fatal.
    from gateway.auth import AuthConfigError, _DEFAULT_JWT_SECRET, validate_auth_config

    _set_env(APP_ENV="production", AUTH_ENABLED="true", AUTH_DEV_TOKENS="false",
             AUTH_JWT_SECRET=_DEFAULT_JWT_SECRET)
    with pytest.raises(AuthConfigError):
        validate_auth_config()


def test_startup_fails_in_production_with_dev_tokens_enabled():
    # C3: dev-token seam may never be live in a production-like environment.
    from gateway.auth import AuthConfigError, validate_auth_config

    _set_env(APP_ENV="production", AUTH_ENABLED="true", AUTH_JWT_SECRET=_TEST_SECRET,
             AUTH_DEV_TOKENS="true")
    with pytest.raises(AuthConfigError):
        validate_auth_config()


def test_startup_ok_for_hardened_production():
    from gateway.auth import validate_auth_config

    _set_env(APP_ENV="production", AUTH_ENABLED="true", AUTH_JWT_SECRET=_TEST_SECRET,
             AUTH_DEV_TOKENS="false")
    validate_auth_config()  # fully hardened -> no raise


def test_main_import_aborts_on_unsafe_config():
    # Proves the guard is actually wired into process startup: reloading the
    # gateway entrypoint under an unsafe posture must propagate AuthConfigError.
    import importlib

    from gateway.auth import AuthConfigError

    _set_env(APP_ENV="production", AUTH_ENABLED="false")
    import gateway.main as mainmod

    with pytest.raises(AuthConfigError):
        importlib.reload(mainmod)


# --------------------------------------------------------------------------- C3 dev-token endpoint
def test_dev_token_available_in_development():
    # Local dev (default APP_ENV) with AUTH_DEV_TOKENS=true -> seam works.
    _set_env(APP_ENV="development", AUTH_DEV_TOKENS="true")
    c = _client(enabled=False)
    r = c.post("/api/auth/dev-token", json={"role": Role.DTCCC_ADMIN.value})
    assert r.status_code == 200
    assert r.json()["role"] == Role.DTCCC_ADMIN.value


def test_dev_token_disabled_outside_development():
    # C3: staging/production -> the endpoint 404s regardless of AUTH_DEV_TOKENS.
    _set_env(AUTH_DEV_TOKENS="true", AUTH_JWT_SECRET=_TEST_SECRET)
    c = _client(enabled=True)  # _client forces a non-default secret
    os.environ["APP_ENV"] = "production"
    r = c.post(
        "/api/auth/dev-token",
        json={"role": Role.DTCCC_ADMIN.value},
        headers=_bearer(Role.DTCCC_ADMIN.value),
    )
    assert r.status_code == 404


def test_dev_token_disabled_when_flag_off():
    _set_env(APP_ENV="development", AUTH_DEV_TOKENS="false")
    c = _client(enabled=False)
    r = c.post("/api/auth/dev-token", json={"role": Role.DRIVER.value})
    assert r.status_code == 404
