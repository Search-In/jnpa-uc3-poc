"""RDS hardening regression guards (docs/RDS_SECURITY.md).

The 2026-08-04 audit found the production RDS endpoint committed in cleartext in
24 files, every DSN using the `postgres` superuser, and the instance reachable
from the public internet. The network fix is an AWS console change these tests
cannot assert; what they CAN do is stop the repository half of the finding from
silently coming back:

  * no real ``*.rds.amazonaws.com`` endpoint anywhere in the tree,
  * no ``postgres`` superuser in any example/deploy DSN,
  * the deploy scripts REQUIRE an endpoint instead of defaulting to one.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Directories that are not source of truth (vendored deps, build output, VCS,
# and the developer's own untracked .env.local which legitimately holds real
# values and is gitignored).
_SKIP_DIRS = {
    ".git", ".venv", ".venv-tmp", "node_modules", "__pycache__", ".pytest_cache",
    "dist", "build", ".mypy_cache", ".ruff_cache", "htmlcov", "evidence",
}
_SKIP_FILES = {".env.local", ".env.migration-test", ".env", ".env.prod"}

_TEXT_SUFFIXES = {
    ".py", ".md", ".yml", ".yaml", ".sh", ".patch", ".example", ".json",
    ".ts", ".tsx", ".sql", ".toml", ".cfg", ".ini", ".txt", "",
}

# A concrete AWS RDS endpoint, e.g. database-1.abc123.ap-south-1.rds.amazonaws.com
_RDS_ENDPOINT_RE = re.compile(r"[a-z0-9-]+\.[a-z0-9]{8,}\.[a-z0-9-]+\.rds\.amazonaws\.com", re.I)


def _tracked_text_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.name in _SKIP_FILES:
            continue
        if p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            yield p, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_no_real_rds_endpoint_is_committed():
    """A concrete RDS hostname must never reappear in the repository."""
    offenders = []
    for path, text in _tracked_text_files():
        for m in _RDS_ENDPOINT_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {m.group(0)}")
    assert not offenders, (
        "A real RDS endpoint is committed. Replace it with __RDS_HOST__ and read "
        "docs/RDS_SECURITY.md:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("rel", [
    ".env.local.example",
    ".env.aws.example",
    "deploy/.env.prod.example",
    "deploy/aws/rds.env.patch",
])
def test_example_dsns_use_placeholders_not_the_superuser(rel):
    """Example DSNs must template BOTH the host and the user, and the templated
    user must not be the `postgres` superuser (docs/RDS_SECURITY.md §3)."""
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    text = path.read_text()
    dsn_lines = [
        ln for ln in text.splitlines()
        if re.match(r"^\s*[A-Z0-9_]*POSTGRES_DSN\s*=", ln)
    ]
    assert dsn_lines, f"{rel} declares no POSTGRES_DSN to check"
    for ln in dsn_lines:
        assert "__RDS_HOST__" in ln, f"{rel}: DSN does not template the host: {ln}"
        assert "://postgres:" not in ln, (
            f"{rel}: DSN still uses the postgres SUPERUSER; use __RDS_USER__ "
            f"(least-privilege jnpa_app) — see docs/RDS_SECURITY.md §3:\n  {ln}"
        )


@pytest.mark.parametrize("rel", [
    "deploy/aws/use-rds.sh",
    "deploy/aws/verify-rds.sh",
    "deploy/aws/rds-preflight.sh",
])
def test_deploy_scripts_require_the_endpoint(rel):
    """These must abort without RDS_HOST rather than fall back to a baked-in one."""
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    text = path.read_text()
    assert re.search(r'RDS_HOST="\$\{RDS_HOST:\?', text), (
        f"{rel} must use ${{RDS_HOST:?...}} so a missing endpoint fails loudly "
        "instead of silently targeting a hardcoded host."
    )


def test_rds_security_doc_exists_and_covers_the_three_findings():
    doc = ROOT / "docs" / "RDS_SECURITY.md"
    assert doc.exists(), "docs/RDS_SECURITY.md is referenced by the env examples"
    text = doc.read_text().lower()
    for topic in ("security group", "least-privilege", "publicly-accessible",
                  "jnpa_app", "rotate"):
        assert topic in text, f"docs/RDS_SECURITY.md does not cover: {topic}"


def test_auth_is_enabled_by_default_in_the_local_example():
    """The demo profile must not ship an unauthenticated gateway.

    With AUTH_ENABLED=false there is no principal, so /api/drivers/master served
    31.8k real licences to anyone who could reach the port.
    """
    text = (ROOT / ".env.local.example").read_text()
    assert re.search(r"^AUTH_ENABLED=true\s*$", text, re.M), \
        ".env.local.example must ship AUTH_ENABLED=true"
    assert re.search(r"^VITE_AUTH_ENABLED=true\s*$", text, re.M), \
        "VITE_AUTH_ENABLED must match AUTH_ENABLED or the dashboard sends no token"
    assert not re.search(r"^AUTH_JWT_SECRET=jnpa-uc3-dev-secret-change-me\s*$", text, re.M), \
        "the example must not ship the well-known default JWT secret with auth on"


def test_pii_masking_defaults_are_declared_in_the_example():
    text = (ROOT / ".env.local.example").read_text()
    assert re.search(r"^PII_MASKING_ENABLED=true\s*$", text, re.M)
    assert re.search(r"^PII_UNMASK_ROLES=", text, re.M)
