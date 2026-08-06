"""Database-backed multi-user authentication tests (core.app_user / migration 0123).

Covers the login matrix the deployment brief asks for — admin, operator, gate
user, transporter, wrong password, inactive account, and role restriction — plus
the properties that make the rewrite worth doing: passwords are stored as
PBKDF2 digests rather than plaintext, and there is no longer any credential
compiled into the source tree.

Runs fully in-process. There is no Postgres in the suite, so gateway.users
resolves to its development in-memory backend; every test seeds the accounts it
needs explicitly through the same create_user() path the seed script uses. That
in-memory store starts EMPTY and is never auto-populated, which is the point of
the change: a store with no rows must fail every login closed rather than
re-materialise a default credential.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")
# Keep PBKDF2 cheap in-process. Each digest records the iteration count it was
# written with, so this only affects test runtime, never verification.
os.environ.setdefault("AUTH_PBKDF2_ITERATIONS", "1000")

from starlette.testclient import TestClient  # noqa: E402

from gateway import users  # noqa: E402
from gateway.auth import Role, normalize_role  # noqa: E402

_OPEN_CLIENTS: list[TestClient] = []

_AUTH_ENV_KEYS = (
    "AUTH_ENABLED",
    "AUTH_RATE_LIMIT_PER_MIN",
    "AUTH_JWT_SECRET",
    "AUTH_DEV_TOKENS",
    "APP_ENV",
)

_TEST_SECRET = "test-secret-not-the-default-0123456789abcdef"

# Long enough to clear MIN_PASSWORD_LENGTH; never a real credential.
_PW = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _restore_env_and_store():
    saved = {k: os.environ.get(k) for k in _AUTH_ENV_KEYS}
    users.reset_memory_store()
    try:
        yield
    finally:
        while _OPEN_CLIENTS:
            try:
                _OPEN_CLIENTS.pop().__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        users.reset_memory_store()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib

        import gateway.main as mainmod

        importlib.reload(mainmod)


def _client(enabled: bool = True, rate_per_min: int = 10_000) -> TestClient:
    """A fresh app under the requested auth posture, run through its lifespan."""
    import importlib

    os.environ["AUTH_ENABLED"] = "true" if enabled else "false"
    os.environ["AUTH_RATE_LIMIT_PER_MIN"] = str(rate_per_min)
    os.environ["AUTH_JWT_SECRET"] = _TEST_SECRET

    import gateway.main as mainmod

    importlib.reload(mainmod)
    # raise_server_exceptions=False: there is no Postgres in-process, so a handler
    # the auth gate correctly ALLOWS may still blow up reaching for it. These tests
    # assert on the authorization decision (401/403 vs anything else), which the
    # middleware makes before routing — so a 5xx from the handler is a pass, not an
    # error to propagate.
    client = TestClient(mainmod.app, raise_server_exceptions=False)
    client.__enter__()
    _OPEN_CLIENTS.append(client)
    return client


def _dsn(client: TestClient) -> str:
    """The DSN the running app will use, so seeded rows land where login reads."""
    return client.app.state.gw.cfg.postgres_dsn


def _seed(client: TestClient, username: str, role: str, *, password: str = _PW,
          active: bool = True) -> None:
    dsn = _dsn(client)
    asyncio.run(users.create_user(
        dsn, username=username, password=password, role=role,
        full_name=f"Test {username}", must_change_password=False))
    if not active:
        asyncio.run(users.set_active(dsn, username, False))


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- hashing
def test_password_is_hashed_not_stored_plaintext():
    digest = users.hash_password(_PW)
    assert _PW not in digest
    assert digest.startswith("pbkdf2_sha256$")
    assert users.verify_password(_PW, digest) is True
    assert users.verify_password("wrong", digest) is False


def test_same_password_hashes_differently_per_user():
    """Distinct random salts — two accounts sharing a password must not share a
    digest, or the store leaks that fact."""
    assert users.hash_password(_PW) != users.hash_password(_PW)


def test_verify_rejects_malformed_digest_without_raising():
    for junk in ("", "not-a-hash", "pbkdf2_sha256$abc$def", "md5$1$x$y"):
        assert users.verify_password(_PW, junk) is False


# --------------------------------------------------------------- role aliasing
def test_role_aliases_resolve_to_canonical_roles():
    assert normalize_role("ADMIN") == Role.DTCCC_ADMIN.value
    assert normalize_role("operator") == Role.TERMINAL_OPS.value
    assert normalize_role("gate_user") == Role.CUSTOMS.value
    assert normalize_role("Gate User") == Role.CUSTOMS.value
    assert normalize_role("TRANSPORTER") == Role.TRANSPORTER.value
    # Canonical names pass through unchanged.
    assert normalize_role("DTCCC_ADMIN") == Role.DTCCC_ADMIN.value
    assert normalize_role("nonsense") is None
    assert normalize_role(None) is None


def test_transporter_is_not_the_driver_role():
    """The brief's reason for a new role: DRIVER is device-scoped and may not
    enumerate the fleet, which is exactly what a transport user must do."""
    from gateway.auth import driver_scope_violation

    assert Role.TRANSPORTER.value != Role.DRIVER.value
    assert driver_scope_violation("/api/trucks", None) is not None  # DRIVER blocked
    # The scope rule is only consulted for DRIVER principals, so TRANSPORTER is
    # unaffected by it — asserted end-to-end in test_transporter_may_list_fleet.


# --------------------------------------------------------------- the login matrix
@pytest.mark.parametrize(
    ("username", "role_input", "expected_role"),
    [
        ("admin", "ADMIN", Role.DTCCC_ADMIN.value),
        ("operator", "OPERATOR", Role.TERMINAL_OPS.value),
        ("gate", "GATE_USER", Role.CUSTOMS.value),
        ("transport", "TRANSPORTER", Role.TRANSPORTER.value),
    ],
)
def test_login_succeeds_for_each_seeded_role(username, role_input, expected_role):
    c = _client(enabled=True)
    _seed(c, username, role_input)

    r = _login(c, username, _PW)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == expected_role
    assert body["username"] == username
    assert body["access_token"]

    # The minted token authenticates a real request (not just the login route).
    me = c.get("/api/auth/me", headers=_auth(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == username
    assert me.json()["role"] == expected_role


def test_login_is_case_insensitive_on_username():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    assert _login(c, "ADMIN", _PW).status_code == 200
    assert _login(c, "Admin", _PW).status_code == 200


def test_wrong_password_is_rejected():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    r = _login(c, "admin", "not-the-password")
    assert r.status_code == 401
    assert "access_token" not in r.json()


def test_unknown_user_is_rejected_with_the_same_message_as_a_wrong_password():
    """No account-existence oracle: both failures return an identical 401 body."""
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    unknown = _login(c, "no-such-user", _PW)
    wrong = _login(c, "admin", "not-the-password")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_inactive_user_cannot_log_in():
    c = _client(enabled=True)
    _seed(c, "operator", "OPERATOR", active=False)
    r = _login(c, "operator", _PW)
    assert r.status_code == 401


def test_disabling_a_user_invalidates_the_session_at_the_next_me_check():
    """Tokens are stateless and cannot be revoked, so disabling has to bite on the
    next /api/auth/me — which is what the console calls on load."""
    c = _client(enabled=True)
    _seed(c, "operator", "OPERATOR")
    token = _login(c, "operator", _PW).json()["access_token"]
    assert c.get("/api/auth/me", headers=_auth(token)).status_code == 200

    asyncio.run(users.set_active(_dsn(c), "operator", False))
    assert c.get("/api/auth/me", headers=_auth(token)).status_code == 401


def test_login_fails_closed_when_no_accounts_exist():
    """The whole point of the rewrite: an empty store authenticates nobody. The
    old code answered admin/admin here."""
    c = _client(enabled=True)
    for username, password in (("admin", "admin"), ("terminal", "terminal"),
                               ("customs", "customs"), ("police", "police"),
                               ("driver", "driver"), ("traffic", "traffic")):
        assert _login(c, username, password).status_code == 401, (
            f"seeded PoC credential {username}/{password} still authenticates")


def test_no_hardcoded_credentials_remain_in_the_auth_router():
    """Guards against the dict coming back.

    Checks the module's code, not its prose: the docstring deliberately explains
    what ``_seed_users()`` was and why it is gone, so a raw substring search over
    the whole file would match that explanation."""
    import gateway.routers.auth as auth_router_mod

    assert not hasattr(auth_router_mod, "_seed_users")

    src = (REPO_ROOT / "gateway" / "routers" / "auth.py").read_text(encoding="utf-8")
    # Drop the module docstring (the first triple-quoted block) before searching.
    code = src.split('"""', 2)[-1]
    assert "_seed_users" not in code
    assert "AUTH_USERS" not in code, "the plaintext env-var credential seam must stay removed"
    for literal in ('"admin": ("admin"', "'admin': ('admin'", '"police": ("police"'):
        assert literal not in code


# --------------------------------------------------------------- role restriction
def test_role_restriction_blocks_out_of_scope_paths():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    _seed(c, "operator", "OPERATOR")
    _seed(c, "gate", "GATE_USER")
    _seed(c, "transport", "TRANSPORTER")

    tokens = {
        name: _login(c, name, _PW).json()["access_token"]
        for name in ("admin", "operator", "gate", "transport")
    }

    # Customs clearance: the gate user reaches it, the transport partner does not.
    assert c.get("/api/customs/summary", headers=_auth(tokens["gate"])).status_code not in (401, 403)
    assert c.get("/api/customs/summary", headers=_auth(tokens["transport"])).status_code == 403

    # Control-room surface: the operator reaches it, the gate user does not.
    assert c.get("/api/control/fault", headers=_auth(tokens["operator"])).status_code not in (401, 403)
    assert c.get("/api/control/fault", headers=_auth(tokens["gate"])).status_code == 403

    # Biometric identity data is customs+admin: transport and operator are out.
    assert c.get("/api/identity/enrollments", headers=_auth(tokens["transport"])).status_code == 403
    assert c.get("/api/identity/enrollments", headers=_auth(tokens["operator"])).status_code == 403


def test_user_administration_is_admin_only():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    _seed(c, "operator", "OPERATOR")
    _seed(c, "transport", "TRANSPORTER")

    admin_t = _login(c, "admin", _PW).json()["access_token"]
    op_t = _login(c, "operator", _PW).json()["access_token"]
    tr_t = _login(c, "transport", _PW).json()["access_token"]

    assert c.get("/api/users", headers=_auth(admin_t)).status_code == 200
    assert c.get("/api/users", headers=_auth(op_t)).status_code == 403
    assert c.get("/api/users", headers=_auth(tr_t)).status_code == 403
    # And unauthenticated.
    assert c.get("/api/users").status_code == 401


def test_transporter_may_list_fleet_where_a_driver_may_not():
    """Confirms TRANSPORTER was the right call over reusing DRIVER."""
    c = _client(enabled=True)
    _seed(c, "transport", "TRANSPORTER")
    tr_t = _login(c, "transport", _PW).json()["access_token"]

    from gateway.auth import encode_token

    driver_t = encode_token(sub="device:TRK-000001", role=Role.DRIVER.value,
                            device_id="TRK-000001")

    assert c.get("/api/trucks", headers=_auth(tr_t)).status_code not in (401, 403)
    assert c.get("/api/trucks", headers=_auth(driver_t)).status_code == 403


# --------------------------------------------------------------- self-service
def test_me_requires_a_valid_bearer():
    c = _client(enabled=True)
    assert c.get("/api/auth/me").status_code == 401
    assert c.get("/api/auth/me", headers=_auth("not.a.jwt")).status_code == 401


def test_change_password_rotates_the_credential():
    c = _client(enabled=True)
    _seed(c, "gate", "GATE_USER")
    token = _login(c, "gate", _PW).json()["access_token"]

    new_pw = "a-brand-new-passphrase"
    r = c.post("/api/auth/change-password", headers=_auth(token),
               json={"current_password": _PW, "new_password": new_pw})
    assert r.status_code == 200, r.text

    assert _login(c, "gate", _PW).status_code == 401       # old one is dead
    assert _login(c, "gate", new_pw).status_code == 200    # new one works


def test_change_password_requires_the_current_password():
    c = _client(enabled=True)
    _seed(c, "gate", "GATE_USER")
    token = _login(c, "gate", _PW).json()["access_token"]
    r = c.post("/api/auth/change-password", headers=_auth(token),
               json={"current_password": "wrong", "new_password": "another-passphrase"})
    assert r.status_code == 401
    assert _login(c, "gate", _PW).status_code == 200  # unchanged


def test_change_password_enforces_minimum_length():
    c = _client(enabled=True)
    _seed(c, "gate", "GATE_USER")
    token = _login(c, "gate", _PW).json()["access_token"]
    r = c.post("/api/auth/change-password", headers=_auth(token),
               json={"current_password": _PW, "new_password": "short"})
    assert r.status_code == 400


def test_must_change_password_flag_surfaces_then_clears():
    c = _client(enabled=True)
    dsn = _dsn(c)
    asyncio.run(users.create_user(dsn, username="seeded", password=_PW,
                                  role="ADMIN", must_change_password=True))
    body = _login(c, "seeded", _PW).json()
    assert body["must_change_password"] is True

    c.post("/api/auth/change-password", headers=_auth(body["access_token"]),
           json={"current_password": _PW, "new_password": "set-by-the-operator"})
    assert _login(c, "seeded", "set-by-the-operator").json()["must_change_password"] is False


# --------------------------------------------------------------- admin user CRUD
def test_admin_can_create_and_disable_users_over_the_api():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    admin_t = _login(c, "admin", _PW).json()["access_token"]

    created = c.post("/api/users", headers=_auth(admin_t), json={
        "username": "gate2", "password": "gate2-initial-secret",
        "role": "GATE_USER", "full_name": "Second Gate"})
    assert created.status_code == 201, created.text
    assert created.json()["role"] == Role.CUSTOMS.value
    # The hash must never travel to a client.
    assert "password_hash" not in created.json()

    assert _login(c, "gate2", "gate2-initial-secret").status_code == 200

    disabled = c.post("/api/users/gate2/disable", headers=_auth(admin_t))
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    assert _login(c, "gate2", "gate2-initial-secret").status_code == 401

    enabled = c.post("/api/users/gate2/enable", headers=_auth(admin_t))
    assert enabled.status_code == 200
    assert _login(c, "gate2", "gate2-initial-secret").status_code == 200


def test_create_user_rejects_duplicates_and_unknown_roles():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    admin_t = _login(c, "admin", _PW).json()["access_token"]

    dup = c.post("/api/users", headers=_auth(admin_t), json={
        "username": "admin", "password": "another-long-password", "role": "ADMIN"})
    assert dup.status_code == 400

    bad_role = c.post("/api/users", headers=_auth(admin_t), json={
        "username": "someone", "password": "another-long-password", "role": "WIZARD"})
    assert bad_role.status_code == 400

    weak = c.post("/api/users", headers=_auth(admin_t), json={
        "username": "someone", "password": "short", "role": "OPERATOR"})
    assert weak.status_code == 400


def test_admin_cannot_disable_their_own_account():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    admin_t = _login(c, "admin", _PW).json()["access_token"]
    r = c.post("/api/users/admin/disable", headers=_auth(admin_t))
    assert r.status_code == 400
    assert _login(c, "admin", _PW).status_code == 200


def test_user_list_never_exposes_password_hashes():
    c = _client(enabled=True)
    _seed(c, "admin", "ADMIN")
    _seed(c, "operator", "OPERATOR")
    admin_t = _login(c, "admin", _PW).json()["access_token"]
    body = c.get("/api/users", headers=_auth(admin_t)).json()
    assert body["count"] == 2
    for row in body["users"]:
        assert "password_hash" not in row
        assert _PW not in str(row)
