#!/usr/bin/env python3
"""Validate (or bootstrap) .env.local before the stack starts.

    make env-init    # scripts/check_env.py --init   -> create .env.local + secrets
    make env-check   # scripts/check_env.py          -> validate the one you have

The audit found nine variables that the running system needs but that
``.env.local.example`` never mentioned — including ``PWA_PAIRING_SECRET``,
without which ``POST /api/auth/device-token`` returns 401 and **no driver can log
in to the PWA at all**. A fresh machine had no way to discover them, and
``make preflight`` checked three unrelated things (two API keys and a video clip)
while ignoring the DSN, the one true hard dependency.

This module is the single source of truth for "is this environment runnable?".
It is imported by ``gateway/config.py`` at startup (so a misconfigured container
fails loudly at boot rather than at first request) and by ``tests/test_env_config.py``
(so the example file can never drift from the requirement list again).
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env.local"
ENV_EXAMPLE = REPO_ROOT / ".env.local.example"

#: Values that mean "you did not fill this in".
PLACEHOLDERS = ("__RDS_HOST__", "__RDS_USER__", "__RDS_PASSWORD__", "__GENERATE_ME__")

#: The well-known dev secret the auth layer refuses to start on.
DEFAULT_JWT_SECRET = "jnpa-uc3-dev-secret-change-me"


class Var(NamedTuple):
    name: str
    why: str
    #: True  -> the stack cannot start / a core flow is broken without it.
    #: False -> degrades gracefully; reported as a warning.
    required: bool = True


#: Everything the stack genuinely needs, with the consequence of omitting it.
REQUIRED: tuple[Var, ...] = (
    # --- database -----------------------------------------------------------
    Var("POSTGRES_DSN", "async engine DSN; every service fails to start without it"),
    Var("RFID_POSTGRES_DSN", "libpq DSN used by make psql / make migrate / importers"),
    Var("TRUCK_POSTGRES_DSN", "truck simulator persistence"),
    Var("CONGESTION_POSTGRES_DSN", "congestion forecaster persistence"),
    Var("ANOMALY_POSTGRES_DSN", "anomaly detector persistence"),
    # --- auth ---------------------------------------------------------------
    Var("APP_ENV", "gates the startup security checks (development | staging | production)"),
    Var("AUTH_ENABLED", "with this false the gateway serves driver PII unauthenticated"),
    Var("AUTH_JWT_SECRET", "token signing key; gateway refuses to start on the default"),
    # --- driver PWA ---------------------------------------------------------
    Var("PWA_PAIRING_SECRET",
        "POST /api/auth/device-token returns 401 without it — NO DRIVER CAN LOG IN"),
    # --- infrastructure -----------------------------------------------------
    Var("REDIS_URL", "cache + camera frame bus"),
    Var("KAFKA_BROKERS", "lifecycle event bus (jnpa.uc3.lifecycle)"),
    Var("MINIO_ENDPOINT", "evidence / model / document object store"),
    Var("MINIO_ACCESS_KEY", "compose refuses to start the ANPR service without it"),
    Var("MINIO_SECRET_KEY", "compose refuses to start the ANPR service without it"),
)

#: Needed by a specific screen or flow; absence degrades rather than breaks.
RECOMMENDED: tuple[Var, ...] = (
    Var("PII_MASKING_ENABLED", "DPDP masking kill switch; absent means ON (safe)", False),
    Var("PII_UNMASK_ROLES", "roles entitled to cleartext PII; absent means DTCCC_ADMIN+CUSTOMS", False),
    Var("OSRM_BASE_URL", "reroute ETA recompute; absent falls back to straight-line", False),
    Var("OSRM_TIMEOUT_S", "OSRM call budget", False),
    Var("TRUCK_NUM_DEVICES", "simulated fleet size", False),
    Var("TRUCK_MAX_DEVICES", "correlator ring-buffer ceiling", False),
    Var("TRUCK_ORIGIN_RADIUS_KM", "trip origin sampling radius", False),
    Var("TRUCK_GATE_DWELL_S", "modelled in-gate dwell (feeds the TAT KPI)", False),
    Var("FASTAG_DEMO_MODE", "populates the FASTag tab offline", False),
    Var("CONGESTION_SOURCE_TIMEOUT_S", "per-source timeout for the congestion fan-out", False),
    Var("VITE_AUTH_ENABLED", "must match AUTH_ENABLED or the dashboard sends no token", False),
    Var("GRAFANA_PG_HOST", "Grafana Postgres datasource; compose requires it", False),
)

ALL_VARS: tuple[Var, ...] = REQUIRED + RECOMMENDED


# --------------------------------------------------------------------------- io
def parse_env(path: Path) -> dict[str, str]:
    """Minimal .env parser — KEY=VALUE, ignoring comments and blank lines."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def env_source(explicit: dict[str, str] | None = None) -> dict[str, str]:
    """.env.local overlaid by the real process environment (which wins)."""
    if explicit is not None:
        return explicit
    merged = parse_env(ENV_FILE)
    merged.update({k: v for k, v in os.environ.items() if k in {v_.name for v_ in ALL_VARS}})
    return merged


# ---------------------------------------------------------------- the checks
def validate(env: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)``. Empty errors means the stack can start."""
    errors: list[str] = []
    warnings: list[str] = []

    def val(name: str) -> str:
        return (env.get(name) or "").strip()

    # 1. presence
    for var in REQUIRED:
        if not val(var.name):
            errors.append(f"{var.name} is missing or empty — {var.why}")
    for var in RECOMMENDED:
        if not val(var.name):
            warnings.append(f"{var.name} is unset — {var.why}")

    # 2. unfilled placeholders
    for var in ALL_VARS:
        v = val(var.name)
        for ph in PLACEHOLDERS:
            if ph in v:
                errors.append(
                    f"{var.name} still contains the placeholder {ph} — "
                    + ("run `make env-init`" if ph == "__GENERATE_ME__"
                       else "fill it in; see docs/RDS_SECURITY.md")
                )

    # 3. auth posture (mirrors gateway/auth.validate_auth_config)
    app_env = val("APP_ENV").lower() or "development"
    auth_on = val("AUTH_ENABLED").lower() in {"1", "true", "yes", "on"}
    prod_like = app_env not in {"development", "dev", "local", "test"}

    if prod_like and not auth_on:
        errors.append(
            f"APP_ENV={app_env} requires AUTH_ENABLED=true — refusing an "
            "unauthenticated gateway outside local development"
        )
    if auth_on:
        secret = val("AUTH_JWT_SECRET")
        if secret == DEFAULT_JWT_SECRET:
            errors.append(
                "AUTH_JWT_SECRET is still the well-known default while AUTH_ENABLED=true. "
                "Generate one: openssl rand -hex 32  (or run `make env-init`)"
            )
        elif secret and len(secret) < 32:
            warnings.append(
                f"AUTH_JWT_SECRET is short ({len(secret)} chars); use at least 32 "
                "(openssl rand -hex 32)"
            )
    if prod_like and val("AUTH_DEV_TOKENS").lower() in {"1", "true", "yes", "on"}:
        errors.append(
            f"APP_ENV={app_env} requires AUTH_DEV_TOKENS=false — the password-less "
            "token seam may not be live outside development"
        )
    if not auth_on:
        warnings.append(
            "AUTH_ENABLED=false: every endpoint is unauthenticated. PII is still "
            "masked (gateway/pii.py fails closed), but this is not a demo posture."
        )

    # 4. dashboard/gateway auth agreement — a silent 401 storm otherwise
    vite_auth = val("VITE_AUTH_ENABLED").lower()
    if vite_auth and (vite_auth in {"1", "true", "yes", "on"}) != auth_on:
        warnings.append(
            f"VITE_AUTH_ENABLED={vite_auth} does not match AUTH_ENABLED={auth_on} — "
            "the dashboard will either send no token to an enforcing gateway (401 "
            "everywhere) or show a pointless login gate"
        )

    # 5. DSNs must actually be DSNs, and must not point at a local sandbox by
    #    accident in a production-like environment.
    for name in ("POSTGRES_DSN", "RFID_POSTGRES_DSN"):
        v = val(name)
        if v and not v.startswith("postgres"):
            errors.append(f"{name} does not look like a Postgres DSN: {v[:32]}...")
        if prod_like and re.search(r"@(postgres|localhost|127\.0\.0\.1):", v):
            errors.append(f"{name} points at a LOCAL database in APP_ENV={app_env}")

    # 6. superuser warning (docs/RDS_SECURITY.md §3)
    if re.search(r"://postgres:", val("POSTGRES_DSN")):
        warnings.append(
            "POSTGRES_DSN connects as the `postgres` SUPERUSER. Use the "
            "least-privilege application role (jnpa_app) — docs/RDS_SECURITY.md §3"
        )

    return errors, warnings


# ------------------------------------------------------------------- --init
def init_env() -> int:
    if ENV_FILE.exists():
        print(f"ERROR: {ENV_FILE.name} already exists — refusing to overwrite.",
              file=sys.stderr)
        print("       Delete it first, or run `make env-check` to validate it.",
              file=sys.stderr)
        return 1
    if not ENV_EXAMPLE.is_file():
        print(f"ERROR: {ENV_EXAMPLE} not found", file=sys.stderr)
        return 1

    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # Each __GENERATE_ME__ gets its OWN value — reusing one secret across
    # AUTH_JWT_SECRET and PWA_PAIRING_SECRET would let a paired device forge
    # tokens for any role.
    generated = 0

    def _gen(_m: re.Match) -> str:
        nonlocal generated
        generated += 1
        return secrets.token_hex(32)

    text = re.sub(r"__GENERATE_ME__", _gen, text)
    ENV_FILE.write_text(text, encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)  # it will hold the RDS password
    except OSError:
        pass

    print(f">> created {ENV_FILE.name} ({generated} secret(s) generated, mode 600)")
    remaining = [
        f"   {i}: {ln.split('=', 1)[0]}"
        for i, ln in enumerate(ENV_FILE.read_text().splitlines(), 1)
        if any(ph in ln for ph in PLACEHOLDERS) and not ln.lstrip().startswith("#")
    ]
    if remaining:
        print(">> STILL REQUIRED — fill these by hand (see docs/RDS_SECURITY.md):")
        for line in remaining:
            print(line)
    print(">> then: make env-check && make migrate && make up")
    return 0


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true",
                    help="create .env.local from the example and generate secrets")
    ap.add_argument("--quiet", action="store_true", help="print only failures")
    args = ap.parse_args(argv)

    if args.init:
        return init_env()

    if not ENV_FILE.is_file():
        print(f"ERROR: {ENV_FILE.name} not found. Run `make env-init`.", file=sys.stderr)
        return 1

    errors, warnings = validate(env_source())

    if warnings and not args.quiet:
        print(f"-- {len(warnings)} warning(s) --")
        for w in warnings:
            print(f"   WARN  {w}")
    if errors:
        print(f"\n-- {len(errors)} error(s) — the stack will not run --", file=sys.stderr)
        for e in errors:
            print(f"   FAIL  {e}", file=sys.stderr)
        print("\nFix the above, then re-run `make env-check`.", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"\nOK: .env.local satisfies all {len(REQUIRED)} required variables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
