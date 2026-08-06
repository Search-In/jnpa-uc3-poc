-- ============================================================
-- 0123  core.app_user — database-backed console accounts
--
-- Replaces the PoC credential store, which was a Python dict of PLAINTEXT
-- passwords in gateway/routers/auth.py (`_seed_users()`): one account per role,
-- including the well-known admin/admin. That dict was overridable only through
-- the AUTH_USERS env var, which is set in no environment file in this repo, so
-- the defaults were live in every deployment including production.
--
-- Passwords are stored ONLY as a PBKDF2-HMAC-SHA256 digest written by
-- gateway/users.py (format: pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>).
-- Nothing in this migration inserts a user: accounts are created exclusively by
-- scripts/seed_auth_users.py, which generates or reads passwords at run time so
-- no credential is ever committed to source control.
--
-- role holds the CANONICAL role name the JWT carries. The four operator-facing
-- names (ADMIN / OPERATOR / GATE_USER / TRANSPORTER) are normalised to these
-- canonical values by gateway.auth.normalize_role() before they reach this
-- table, so the CHECK constraint and gateway.auth.Role stay in lockstep:
--     ADMIN       -> DTCCC_ADMIN
--     OPERATOR    -> TERMINAL_OPS
--     GATE_USER   -> CUSTOMS
--     TRANSPORTER -> TRANSPORTER (new role added alongside the original six)
--
-- DRIVER is permitted here for completeness but is NOT how drivers sign in: the
-- PWA mints a device-bound DRIVER token via /api/auth/device-token, and a DRIVER
-- token is device-scoped (gateway.auth.driver_scope_violation) so it cannot list
-- the fleet. Transport-company staff therefore get TRANSPORTER, never DRIVER.
--
-- Idempotent (IF NOT EXISTS throughout) so a re-run on a partially migrated
-- database is a no-op.
-- ============================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.app_user (
    user_id              bigserial   PRIMARY KEY,
    username             text        NOT NULL,
    password_hash        text        NOT NULL,
    role                 text        NOT NULL,
    full_name            text,
    email                text,
    is_active            boolean     NOT NULL DEFAULT true,
    must_change_password boolean     NOT NULL DEFAULT false,
    last_login_at        timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT app_user_role_chk CHECK (role IN (
        'DTCCC_ADMIN',
        'JNPA_TRAFFIC',
        'TERMINAL_OPS',
        'CUSTOMS',
        'TRAFFIC_POLICE',
        'TRANSPORTER',
        'DRIVER'
    ))
);

-- Login is case-insensitive ("Admin" and "admin" are the same account), so the
-- uniqueness constraint has to be too — a plain UNIQUE(username) would let both
-- rows exist and make the login lookup ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_username_lower
    ON core.app_user (lower(username));

-- Serves the admin user list, which filters/sorts on active + role.
CREATE INDEX IF NOT EXISTS idx_app_user_active_role
    ON core.app_user (is_active, role);

COMMENT ON TABLE  core.app_user IS
    'Console (dashboard) user accounts. Passwords are PBKDF2-SHA256 digests written by gateway/users.py; seeded only via scripts/seed_auth_users.py.';
COMMENT ON COLUMN core.app_user.role IS
    'Canonical gateway.auth.Role value. ADMIN/OPERATOR/GATE_USER are operator-facing aliases normalised before insert.';
COMMENT ON COLUMN core.app_user.must_change_password IS
    'Set on seeded/reset accounts; cleared by POST /api/auth/change-password.';

COMMIT;
