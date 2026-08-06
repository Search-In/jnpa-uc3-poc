#!/usr/bin/env python3
"""Seed console accounts into core.app_user (migration 0123).

This is the ONLY way accounts enter the system, and it is deliberately a script
rather than migration DDL: passwords are generated (or read from the
environment) at run time, so no credential is ever written to source control or
to a .sql file. The old model — a dict of plaintext role-name/role-name pairs in
gateway/routers/auth.py, with admin/admin live in every deployment — is gone.

Usage
-----
Seed the four standard accounts, generating a strong password for each and
printing them once:

    POSTGRES_DSN=postgresql+asyncpg://user:pass@host:5432/db \\
        python scripts/seed_auth_users.py

Supply specific passwords instead of generating them (CI / automation), via
SEED_<USERNAME>_PASSWORD:

    SEED_ADMIN_PASSWORD='...' SEED_OPERATOR_PASSWORD='...' \\
        python scripts/seed_auth_users.py

Create a single account:

    python scripts/seed_auth_users.py --user jane.doe --role OPERATOR \\
        --full-name 'Jane Doe' --email jane@example.com

Re-issue a password for an account that already exists:

    python scripts/seed_auth_users.py --user admin --reset-existing

Seeded accounts are flagged must_change_password=true by default, so the console
shows a "change password" prompt until the holder sets their own. That flag is
advisory — the gateway never blocks a login or an API call on it. To seed demo or
pilot accounts without the prompt:

    SEED_MUST_CHANGE_PASSWORD=false python scripts/seed_auth_users.py
    # or equivalently
    python scripts/seed_auth_users.py --no-force-password-change
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gateway import users  # noqa: E402
from gateway.auth import ALL_ROLES, ROLE_ALIASES, normalize_role  # noqa: E402

# What --role accepts, most useful names first: the four the deployment brief
# names, then every canonical role. ADMIN/OPERATOR/GATE_USER are aliases;
# TRANSPORTER is itself canonical, so it would not appear in ROLE_ALIASES alone.
_PRIMARY_ROLE_NAMES: tuple[str, ...] = ("ADMIN", "OPERATOR", "GATE_USER", "TRANSPORTER")
ACCEPTED_ROLE_NAMES: tuple[str, ...] = (
    *_PRIMARY_ROLE_NAMES,
    *sorted((set(ALL_ROLES) | set(ROLE_ALIASES)) - set(_PRIMARY_ROLE_NAMES)),
)

# The four operator-facing accounts the deployment brief asks for. Roles are the
# alias names; gateway.auth.normalize_role maps them to the canonical values the
# JWT and the RBAC policy use (ADMIN -> DTCCC_ADMIN, OPERATOR -> TERMINAL_OPS,
# GATE_USER -> CUSTOMS, TRANSPORTER -> TRANSPORTER).
DEFAULT_ACCOUNTS: tuple[dict[str, str], ...] = (
    {"username": "admin", "role": "ADMIN", "full_name": "DTCCC Administrator"},
    {"username": "operator", "role": "OPERATOR", "full_name": "Terminal Operator"},
    {"username": "gate", "role": "GATE_USER", "full_name": "Gate Operator"},
    {"username": "transport", "role": "TRANSPORTER", "full_name": "Transport Partner"},
)

# Generated password length in bytes of entropy (token_urlsafe -> ~1.3 chars/byte).
_GENERATED_ENTROPY_BYTES = 12

# Whether seeded accounts are flagged must_change_password. Defaults to true
# (a bootstrap password the holder should replace), but configurable so a demo
# or pilot deployment can seed accounts that are ready to use as-is instead of
# carrying the "change password" prompt. Precedence: --no-force-password-change
# overrides SEED_MUST_CHANGE_PASSWORD, which overrides the default.
#
# This only sets a column value. The flag is advisory everywhere it is read: the
# gateway never blocks a login or an API call on it, so turning it off changes a
# UI hint, not access.
MUST_CHANGE_PASSWORD_ENV = "SEED_MUST_CHANGE_PASSWORD"
_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    if raw:
        raise SystemExit(
            f"{name}={raw!r} is not a boolean. Use one of: "
            f"{', '.join(sorted(_TRUTHY | _FALSEY))}."
        )
    return default


def _password_for(username: str) -> tuple[str, str]:
    """(password, source) for an account: the environment if provided, else a
    freshly generated one. Never a literal in this file."""
    env_key = f"SEED_{username.strip().upper().replace('.', '_').replace('-', '_')}_PASSWORD"
    supplied = os.environ.get(env_key, "").strip()
    if supplied:
        return supplied, f"${env_key}"
    return secrets.token_urlsafe(_GENERATED_ENTROPY_BYTES), "generated"


async def _require_db(dsn: str) -> None:
    """Refuse to run against anything but a real Postgres.

    gateway.users falls back to an in-process dict when the database is
    unreachable in development. That is right for the gateway (it keeps a local
    demo booting) and completely wrong for a seed script, which would appear to
    succeed while writing rows into a dict that dies with the process.
    """
    if not dsn:
        raise SystemExit(
            "POSTGRES_DSN is not set. Pass --dsn or export POSTGRES_DSN "
            "(e.g. postgresql+asyncpg://user:pass@host:5432/dbname)."
        )
    backend = await users._backend(dsn)  # noqa: SLF001 — deliberate: verify real persistence
    if backend != "db":
        raise SystemExit(
            f"Could not reach Postgres at the supplied DSN (resolved backend: {backend!r}).\n"
            "Seeding was aborted so accounts are not written to a throwaway in-memory store.\n"
            "Check the DSN, network access, and that migration 0123 has been applied."
        )


async def _seed_one(dsn: str, spec: dict, *, reset_existing: bool,
                    must_change_password: bool) -> tuple[str, str, str, str]:
    """Create (or optionally re-password) one account.

    Returns (username, role, password_or_marker, status)."""
    username = spec["username"]
    role_input = spec["role"]
    canonical = normalize_role(role_input)
    if canonical is None:
        raise SystemExit(
            f"Unknown role {role_input!r} for user {username!r}. "
            f"Valid names: {', '.join(ACCEPTED_ROLE_NAMES)}."
        )

    existing = await users.get_user(dsn, username)
    if existing is not None and not reset_existing:
        return username, canonical, "(unchanged)", "exists"

    password, source = _password_for(username)
    try:
        users.validate_password(password)
    except ValueError as exc:
        raise SystemExit(f"Password for {username!r} rejected: {exc}") from exc

    if existing is not None:
        await users.set_password(dsn, username, password,
                                 must_change_password=must_change_password)
        return username, canonical, password, f"password reset ({source})"

    await users.create_user(
        dsn,
        username=username,
        password=password,
        role=canonical,
        full_name=spec.get("full_name"),
        email=spec.get("email"),
        must_change_password=must_change_password,
    )
    return username, canonical, password, f"created ({source})"


def _print_table(rows: list[tuple[str, str, str, str]], *,
                 must_change_password: bool) -> None:
    if not rows:
        print("Nothing to do.")
        return
    headers = ("USERNAME", "ROLE", "PASSWORD", "STATUS")
    widths = [
        max(len(headers[i]), max(len(str(r[i])) for r in rows)) for i in range(4)
    ]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print()
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r[i]).ljust(widths[i]) for i in range(4)))
    print()
    if any(r[2] not in ("(unchanged)",) for r in rows):
        print("These passwords are shown ONCE and are not recoverable — only the")
        print("PBKDF2 digest is stored. Record them in your secret manager now.")
        if must_change_password:
            print("Every account is flagged must_change_password: the console shows a")
            print("'change password' prompt until the holder sets their own.")
            print(f"To seed without that prompt: {MUST_CHANGE_PASSWORD_ENV}=false")
        else:
            print(f"Accounts were seeded with must_change_password=false "
                  f"({MUST_CHANGE_PASSWORD_ENV}): no change-password prompt.")
        print()


async def _amain(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.environ.get("POSTGRES_DSN", "").strip()
    await _require_db(dsn)

    if args.list:
        rows = await users.list_users(dsn)
        if not rows:
            print("No accounts exist yet. Run without --list to seed them.")
            return 0
        print(f"\n{len(rows)} account(s):\n")
        for r in rows:
            state = "active" if r.get("is_active") else "DISABLED"
            last = r.get("last_login_at") or "never"
            print(f"  {str(r.get('username')):<16} {str(r.get('role')):<16} "
                  f"{state:<9} last login: {last}")
        print()
        return 0

    if args.user:
        specs = [{
            "username": args.user,
            "role": args.role or "OPERATOR",
            "full_name": args.full_name,
            "email": args.email,
        }]
    else:
        specs = [dict(a) for a in DEFAULT_ACCOUNTS]

    # --no-force-password-change wins over the env var, which wins over the default.
    must_change = (False if args.no_force_password_change
                   else _env_flag(MUST_CHANGE_PASSWORD_ENV, True))

    results = []
    for spec in specs:
        results.append(await _seed_one(dsn, spec, reset_existing=args.reset_existing,
                                       must_change_password=must_change))
    _print_table(results, must_change_password=must_change)

    skipped = [r for r in results if r[3] == "exists"]
    if skipped:
        print(f"{len(skipped)} account(s) already existed and were left alone. "
              "Use --reset-existing to issue new passwords for them.")
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed console accounts into core.app_user. Passwords are generated "
                    "at run time or read from SEED_<USERNAME>_PASSWORD — never hardcoded.",
    )
    parser.add_argument("--dsn", default=None,
                        help="Postgres DSN (defaults to $POSTGRES_DSN).")
    parser.add_argument("--user", default=None,
                        help="Seed a single account with this username instead of the four defaults.")
    parser.add_argument("--role", default=None,
                        help=f"Role for --user. One of: {', '.join(ACCEPTED_ROLE_NAMES)}. "
                             "Defaults to OPERATOR.")
    parser.add_argument("--full-name", default=None, help="Display name for --user.")
    parser.add_argument("--email", default=None, help="Email for --user.")
    parser.add_argument("--reset-existing", action="store_true",
                        help="Issue a new password for accounts that already exist.")
    parser.add_argument("--no-force-password-change", action="store_true",
                        help="Seed accounts with must_change_password=false, so the console "
                             f"shows no change-password prompt. Overrides "
                             f"${MUST_CHANGE_PASSWORD_ENV} (default: true). Intended for demo "
                             "and pilot deployments; the flag is advisory either way and never "
                             "blocks a login.")
    parser.add_argument("--list", action="store_true",
                        help="List existing accounts and exit (no writes).")
    args = parser.parse_args()

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
