"""Every hard-required compose variable must be satisfiable by a supported path.

THE BUG THIS PREVENTS
    docker-compose.yml declared `GRAFANA_PG_HOST: ${GRAFANA_PG_HOST:?...}` while
    nothing wrote that variable — not .env.aws.example, not the deploy
    workflow's ensure_key backfill, not a GitHub secret reference. The deploy
    reached the very last step and died with

        error while interpolating services.grafana.environment.GRAFANA_PG_HOST:
        required variable GRAFANA_PG_HOST is missing a value

    Compose resolves interpolation for EVERY service before starting ANY of
    them, so one unset monitoring variable aborted all 30 services. Nothing
    caught it before production because no test connected the compose
    requirement to the files that are supposed to satisfy it.

WHAT IS ASSERTED
    For each `${VAR:?}` in the compose files, at least one supported path
    provides it:
      * .env.aws.example        (manual EC2 deploy)
      * .env.local.example      (make env-init -> make up)
      * .github/workflows/deploy.yml   (ensure_key / set_key / sed backfill)

    This is a static contract check — no docker, no network.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

COMPOSE_FILES = ["docker-compose.yml", "docker-compose.aws.yml"]
AWS_EXAMPLE = ROOT / ".env.aws.example"
LOCAL_EXAMPLE = ROOT / ".env.local.example"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"

# `${VAR:?message}` — the "required, abort if unset" form.
_REQUIRED_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")
# `${VAR:-default}` and `${VAR}` are NOT required and are out of scope here.


def _strip_comments(text: str) -> str:
    """Drop whole-line YAML comments before scanning.

    The compose files DOCUMENT this very convention in prose
    ("the compose vars use the `${VAR:?…}` form"), so scanning raw text picks up
    a literal `VAR` that no environment will ever define. Only inline values can
    actually be interpolated, so comment lines are not interesting.
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def required_vars() -> dict[str, list[str]]:
    """{VAR: [files that require it]} across every compose file."""
    out: dict[str, list[str]] = {}
    for name in COMPOSE_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        body = _strip_comments(path.read_text(encoding="utf-8"))
        for var in set(_REQUIRED_RE.findall(body)):
            out.setdefault(var, []).append(name)
    return out


def declared_in_env_file(path: pathlib.Path, var: str) -> bool:
    """True when `path` assigns `var` on a non-comment line."""
    if not path.exists():
        return False
    return bool(re.search(rf"^{re.escape(var)}=", path.read_text(encoding="utf-8"), re.M))


def provided_by_deploy(var: str) -> bool:
    """True when the deploy workflow writes `var` into .env on the EC2 host.

    Covers the three mechanisms actually used:
      ensure_key VAR ...   (append when missing)
      set_key    VAR ...   (rewrite every run)
      VAR=...              (a literal assignment appended to .env)
      sed -i "s/.../" .env with the var name (placeholder substitution)
    """
    if not DEPLOY_WORKFLOW.exists():
        return False
    text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    patterns = [
        rf"ensure_key\s+{re.escape(var)}\b",
        rf"set_key\s+{re.escape(var)}\b",
        rf"^\s*{re.escape(var)}=",
        rf'echo\s+"{re.escape(var)}=',
    ]
    return any(re.search(p, text, re.M) for p in patterns)


def test_the_scan_finds_the_known_required_vars():
    """Guard the guard: if the regex stops matching, everything below passes
    vacuously and the check silently dies."""
    found = required_vars()
    assert len(found) >= 8, f"only found {len(found)} required vars — regex broken?"
    for expected in ("POSTGRES_DSN", "GRAFANA_PG_HOST", "MINIO_ACCESS_KEY"):
        assert expected in found, f"{expected} not detected as required"


@pytest.mark.parametrize("var", sorted(required_vars()))
def test_required_compose_var_is_satisfiable(var):
    """A `${VAR:?}` with no supplier is a guaranteed deploy-time abort."""
    sources = []
    if declared_in_env_file(AWS_EXAMPLE, var):
        sources.append(".env.aws.example")
    if declared_in_env_file(LOCAL_EXAMPLE, var):
        sources.append(".env.local.example")
    if provided_by_deploy(var):
        sources.append("deploy.yml")

    assert sources, (
        f"{var} is REQUIRED by {', '.join(required_vars()[var])} (${{{var}:?}}) but no "
        f"supported path provides it.\n"
        f"  Compose interpolates every service before starting any of them, so this "
        f"aborts the WHOLE stack.\n"
        f"  Fix by adding it to one or more of:\n"
        f"    .env.aws.example      (manual EC2 deploy)\n"
        f"    .env.local.example    (make env-init / make up)\n"
        f"    .github/workflows/deploy.yml  (ensure_key / set_key)"
    )


def test_grafana_datasource_is_fully_specified():
    """Regression: the exact failure. Every var the Grafana datasource
    provisioning file interpolates must resolve."""
    ds = ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "datasources.yml"
    if not ds.exists():
        pytest.skip("grafana datasource provisioning not present")

    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", ds.read_text(encoding="utf-8")))
    assert "GRAFANA_PG_HOST" in used

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for var in sorted(used):
        # Each one must be passed into the grafana container by compose,
        # otherwise Grafana renders an empty datasource and fails at query time
        # rather than at boot — a much quieter failure.
        assert re.search(rf"^\s*{re.escape(var)}:", compose, re.M), (
            f"{var} is interpolated by the Grafana datasource file but is not "
            f"set in the grafana service's environment in docker-compose.yml"
        )


def test_grafana_password_comes_from_postgres_password():
    """Documents a real trap: the datasource password is POSTGRES_PASSWORD.

    A `GRAFANA_PG_PASSWORD` secret/variable looks like it should work and is
    silently ignored. If this mapping ever changes, the examples and the deploy
    comments must change with it.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"^\s*GRAFANA_PG_PASSWORD:\s*(.+)$", compose, re.M)
    assert m, "grafana service no longer sets GRAFANA_PG_PASSWORD"
    assert "POSTGRES_PASSWORD" in m.group(1), (
        "GRAFANA_PG_PASSWORD no longer derives from POSTGRES_PASSWORD — update "
        ".env.aws.example / .env.local.example, which tell operators that a "
        "GRAFANA_PG_PASSWORD variable has no effect."
    )


def test_deploy_rewrites_rather_than_backfills_the_grafana_host():
    """`ensure_key` only appends when absent, so it would pin a stale endpoint
    after an RDS rotation. The host must be rewritten every run."""
    if not DEPLOY_WORKFLOW.exists():
        pytest.skip("deploy workflow not present")
    text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"set_key\s+GRAFANA_PG_HOST", text), (
        "GRAFANA_PG_HOST must be written with set_key (rewrite), not ensure_key "
        "(append-if-missing), so a rotated RDS endpoint propagates."
    )
    assert not re.search(r"ensure_key\s+GRAFANA_PG_HOST", text)
