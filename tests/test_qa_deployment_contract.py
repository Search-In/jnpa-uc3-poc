"""The QA stack must never collide with production.

THE RISK THIS PREVENTS
    QA and production share an RDS instance, and the QA overlay is a thin diff
    over the production compose files. Docker treats container names, host ports
    and image tags as GLOBAL, and docker-compose.yml hard-codes all three
    (`container_name: jnpa-*`, `ports: "8000:8000"`, `image: jnpa/gateway:0.1.0`).
    An overlay that forgets any one of them does not fail loudly — it takes
    production's name, steals its port, or silently replaces the image tag
    production's next `up` will use. The database is the same story: one copied
    DSN line and QA writes to jnpa_schema_v3.

    QA now deploys to its OWN EC2 instance, which removes the host-port half of
    that risk on today's topology but none of the rest — the two stacks still
    share RDS, the image registry on any shared builder, and this repository.
    The name/tag/database assertions therefore stay exactly as strict.

WHAT IS ASSERTED
    * .env.qa.example points every DSN at RDS/jnpa_qa and never at the production
      database, and carries no credential.
    * docker-compose.qa.yml renames every container, retags every built image,
      and resets every host port except the QA entry points (gateway 18000, web
      80/443 on the dedicated instance).
    * QA's nginx terminates TLS for the QA hostname only, never production's, and
      the HTTPS redirect targets the port QA is actually served on.
    * The production files carry no QA-specific value (i.e. they were not edited).
    * .gitignore hides the filled-in .env.qa but keeps the template trackable.

    Static contract checks — no docker, no network.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

BASE_COMPOSE = ROOT / "docker-compose.yml"
AWS_COMPOSE = ROOT / "docker-compose.aws.yml"
QA_COMPOSE = ROOT / "docker-compose.qa.yml"
QA_ENV = ROOT / ".env.qa.example"
AWS_ENV = ROOT / ".env.aws.example"

QA_DB = "jnpa_qa"
PROD_DB = "jnpa_schema_v3"
QA_RDS_HOST = "__RDS_HOST__"  # templated on purpose — see tests/test_rds_security.py
# QA runs on a dedicated instance: web owns 80/443, the gateway keeps 18000 for
# the debug curls in QA_DEPLOYMENT.md §7.
QA_HOST_PORTS = {"18000", "80", "443"}
QA_PROJECT = "jnpa-uc3-poc-qa"
PROD_PROJECT = "jnpa-uc3-poc"
QA_HOSTNAME = "qa.searchintech.in"
PROD_HOSTNAME = "traffic-three.searchintech.in"
PLACEHOLDER = "<SET_ON_EC2_ONLY>"

DSN_VARS = (
    "POSTGRES_DSN",
    "RFID_POSTGRES_DSN",
    "TRUCK_POSTGRES_DSN",
    "CONGESTION_POSTGRES_DSN",
    "ANOMALY_POSTGRES_DSN",
)


# --------------------------------------------------------------------------- #
# A tiny indent-based compose reader. Deliberately dependency-free (the sibling
# contract test is too): the CI python job installs only the project's own
# packages, so a PyYAML import here would be a new install-order dependency.
# It also has to survive the `!reset` / `!override` tags, which a plain
# yaml.safe_load rejects outright.
# --------------------------------------------------------------------------- #
def read_services(path: pathlib.Path) -> dict[str, dict]:
    """{service: {container_name, image, ports: [...], ports_tag: str|None}}."""
    services: dict[str, dict] = {}
    svc: str | None = None
    in_services = False
    collecting_ports = False

    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if not in_services:
            continue
        if line.strip().startswith("#") or not line.strip():
            continue

        m = re.match(r"^  ([A-Za-z0-9._-]+):\s*$", line)
        if m:
            svc = m.group(1)
            services[svc] = {"container_name": None, "image": None, "ports": [], "ports_tag": None}
            collecting_ports = False
            continue
        if svc is None:
            continue

        if collecting_ports:
            item = re.match(r"^      - \"?([0-9]+):", line)
            if item:
                services[svc]["ports"].append(item.group(1))
                continue
            collecting_ports = False

        m = re.match(r"^    ports:\s*(\S+)?", line)
        if m:
            tag = m.group(1) or ""
            services[svc]["ports_tag"] = tag.split()[0] if tag else None
            collecting_ports = not tag.startswith("!reset")
            continue
        m = re.match(r"^    container_name:\s*(\S+)", line)
        if m:
            services[svc]["container_name"] = m.group(1)
            continue
        m = re.match(r"^    image:\s*(\S+)", line)
        if m:
            services[svc]["image"] = m.group(1)
            continue

    return services


def env_values(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


BASE = read_services(BASE_COMPOSE)
QA = read_services(QA_COMPOSE)


# --------------------------------------------------------------------------- #
# Guard the guard
# --------------------------------------------------------------------------- #
def test_the_compose_reader_still_works():
    """If the parser stops matching, every assertion below passes vacuously."""
    assert len(BASE) >= 25, f"only parsed {len(BASE)} base services — reader broken?"
    assert BASE["gateway"]["container_name"] == "jnpa-gateway"
    assert BASE["gateway"]["ports"] == ["8000"]
    assert BASE["web"]["image"] == "jnpa/web:0.1.0"
    assert len(QA) >= 25, f"only parsed {len(QA)} QA services — reader broken?"


# --------------------------------------------------------------------------- #
# .env.qa.example
# --------------------------------------------------------------------------- #
def test_qa_env_example_exists_and_is_tracked_as_a_template():
    assert QA_ENV.exists(), ".env.qa.example is the QA template — it must be committed"


@pytest.mark.parametrize("var", DSN_VARS)
def test_every_qa_dsn_points_at_the_qa_database(var):
    dsn = env_values(QA_ENV).get(var)
    assert dsn, f"{var} is required by docker-compose.yml (${{{var}:?}}) but is missing"
    assert f"/{QA_DB}?" in dsn, f"{var} must target /{QA_DB}: {dsn}"
    assert QA_RDS_HOST in dsn, (
        f"{var} must TEMPLATE the endpoint as {QA_RDS_HOST}: docs/RDS_SECURITY.md\n"
        f"forbids committing a real one, and it is supplied on EC2 via $RDS_HOST: {dsn}"
    )
    assert PROD_DB not in dsn, f"{var} points at the PRODUCTION database: {dsn}"


def test_no_qa_setting_can_reach_the_production_database():
    """Comments may NAME the production database (the header explains the
    difference); no assignment may point at it."""
    for line in QA_ENV.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert PROD_DB not in line, f"{QA_ENV.name} assigns a production-database value: {line}"


def test_qa_env_ssl_dialects_are_not_normalised():
    """asyncpg wants `ssl=`, libpq wants `sslmode=` — mixing them breaks the boot."""
    vals = env_values(QA_ENV)
    assert vals["POSTGRES_DSN"].startswith("postgresql+asyncpg://")
    assert vals["POSTGRES_DSN"].endswith("?ssl=require")
    for var in DSN_VARS[1:]:
        assert vals[var].startswith("postgresql://")
        assert vals[var].endswith("?sslmode=require")


def test_qa_env_carries_no_credentials():
    """Every secret must be a placeholder — this file is committed."""
    secretish = re.compile(r"(PASSWORD|SECRET|_KEY|TOKEN)$")
    for var, val in env_values(QA_ENV).items():
        if not secretish.search(var):
            continue
        assert val in ("", PLACEHOLDER) or val.startswith("http"), (
            f"{var} in {QA_ENV.name} looks like a real credential ({val!r}). "
            f"Committed templates may only contain '' or {PLACEHOLDER}."
        )


def test_qa_env_declares_the_qa_app_env_with_a_safe_auth_posture():
    """APP_ENV=qa is production-like in gateway/auth.py, so the auth guard applies."""
    vals = env_values(QA_ENV)
    assert vals.get("APP_ENV") == "qa"
    assert vals.get("AUTH_ENABLED") == "true", "a non-dev APP_ENV refuses to start without auth"
    assert vals.get("AUTH_DEV_TOKENS") == "false", "the dev-token seam may not be live in QA"


@pytest.mark.parametrize("var", DSN_VARS)
def test_qa_dsns_use_the_least_privilege_role(var):
    """docs/RDS_SECURITY.md §3: the app must not connect as the postgres superuser."""
    dsn = env_values(QA_ENV)[var]
    assert "://postgres:" not in dsn, f"{var} uses the postgres SUPERUSER: {dsn}"
    assert "://jnpa_app:" in dsn, f"{var} must connect as jnpa_app: {dsn}"


def test_qa_grafana_datasource_follows_the_qa_database():
    vals = env_values(QA_ENV)
    assert vals.get("GRAFANA_PG_DB") == QA_DB
    assert vals.get("GRAFANA_PG_HOST", "").startswith(QA_RDS_HOST)
    assert vals.get("GRAFANA_PG_USER") == "jnpa_app"


def test_qa_env_covers_every_variable_the_aws_env_declares():
    """The QA template is the AWS template with QA values — a variable dropped
    from it is a `${VAR:?}` abort at deploy time."""
    missing = set(env_values(AWS_ENV)) - set(env_values(QA_ENV))
    assert not missing, f"{QA_ENV.name} is missing variables present in {AWS_ENV.name}: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# docker-compose.qa.yml
# --------------------------------------------------------------------------- #
def test_qa_overlay_declares_its_own_compose_project():
    body = QA_COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"^name:\s*(\S+)\s*$", body, re.M)
    assert m, "docker-compose.qa.yml must declare a top-level `name:`"
    assert m.group(1) == QA_PROJECT, (
        f"expected project {QA_PROJECT}, got {m.group(1)}"
    )
    assert m.group(1) != PROD_PROJECT, (
        "a shared project name would share networks and named volumes with production"
    )


def test_every_production_container_name_is_overridden():
    for svc, spec in BASE.items():
        if not spec["container_name"]:
            continue
        qa_name = QA.get(svc, {}).get("container_name")
        assert qa_name, (
            f"service '{svc}' hard-codes container_name={spec['container_name']} in "
            f"docker-compose.yml but docker-compose.qa.yml does not rename it — "
            f"`up` would collide with the running production container."
        )
        assert qa_name.startswith("jnpa-qa-"), f"{svc}: QA container name {qa_name} must start with jnpa-qa-"
        assert qa_name != spec["container_name"]


def test_every_built_image_tag_is_overridden():
    for svc, spec in BASE.items():
        img = spec["image"]
        if not img or not img.startswith("jnpa/"):
            continue  # third-party images are pulled, never rebuilt
        qa_img = QA.get(svc, {}).get("image")
        assert qa_img, (
            f"service '{svc}' builds into {img}; docker-compose.qa.yml must retag it "
            f"or a QA build would replace the image production's next `up` uses."
        )
        assert qa_img.endswith(":qa"), f"{svc}: QA image {qa_img} must be tagged :qa"
        assert qa_img != img


def test_qa_publishes_only_the_two_qa_entry_points():
    """Resolved across the FULL documented chain:
    docker-compose.yml -> docker-compose.aws.yml -> docker-compose.qa.yml."""
    aws = read_services(AWS_COMPOSE)
    published: dict[str, list[str]] = {}
    for svc, base_spec in BASE.items():
        ports = base_spec["ports"]
        for overlay in (aws, QA):
            spec = overlay.get(svc, {})
            if spec.get("ports_tag") is not None:
                ports = spec["ports"]  # !reset / !override REPLACE
        if ports:
            published[svc] = ports

    assert set(published) == {"gateway", "web"}, (
        f"only the QA gateway and web may publish a host port; got {published}"
    )
    assert published["gateway"] == ["18000"]
    # QA runs on a DEDICATED EC2 instance, so `web` owns the host's 80/443 and
    # the public URL needs no port suffix. If QA is ever co-located with a
    # production stack again, these move to 18080/18443 — and the redirect in
    # web/nginx/default.qa.conf has to grow an explicit :18443 with them.
    assert published["web"] == ["80", "443"]


def test_qa_port_overrides_replace_rather_than_merge():
    """A plain `ports:` in an overlay APPENDS to the base list, so the QA gateway
    would publish 8000 as well as 18000. `!override` / `!reset` replace it."""
    for svc in ("gateway", "web"):
        tag = QA[svc]["ports_tag"]
        assert tag in ("!override", "!reset"), (
            f"{svc}: `ports:` in docker-compose.qa.yml must carry !override (or "
            f"!reset), else compose MERGES it with the production publish; got {tag!r}"
        )


def test_qa_publishes_no_internal_service_port():
    """QA and production no longer share a box, so overlapping on 80/443 is the
    intended design and not a collision. What still must not happen is QA
    publishing an INTERNAL service to the host: Kafka, MinIO, Redis, Grafana,
    Prometheus, the EIR-OCR sidecar and friends carry no auth of their own and
    are only safe because they stay on the private bridge. A lost `!reset []`
    exposes one straight to the internet on a public EC2 instance."""
    prod_ports = {p for spec in BASE.values() for p in spec["ports"]}
    aws = read_services(AWS_COMPOSE)
    for svc, spec in aws.items():
        if spec["ports_tag"] == "!reset":
            prod_ports -= set(BASE.get(svc, {}).get("ports", []))
        prod_ports |= set(spec["ports"])
    # Everything production publishes EXCEPT the two web entry points QA now
    # legitimately owns on its own instance.
    internal = prod_ports - {"80", "443"}
    assert not (QA_HOST_PORTS & internal), (
        f"QA publishes internal service port(s) {sorted(QA_HOST_PORTS & internal)} to "
        f"the host; these must stay on the private bridge (`ports: !reset []`)"
    )


def test_the_qa_overlay_is_always_applied_last():
    """docker-compose.aws.yml publishes web on production's 80/443 and mounts
    /etc/letsencrypt. The QA overlay neutralises both with `!override`, which
    only replaces values from files applied BEFORE it — so a chain that puts
    docker-compose.qa.yml earlier would silently hand QA production's ports."""
    for path in (ROOT / "deploy" / "qa" / "manage.sh", ROOT / "deploy" / "qa" / "verify-qa.sh"):
        body = path.read_text(encoding="utf-8")
        invocations = [ln for ln in body.splitlines()
                       if not ln.lstrip().startswith("#") and "-f docker-compose" in ln]
        assert invocations, f"{path.name} no longer invokes compose with -f flags"
        for ln in invocations:
            assert "docker-compose.qa.yml" in ln, (
                f"{path.name} invokes compose without the QA overlay: {ln.strip()}"
            )
            if "docker-compose.aws.yml" in ln:
                assert ln.index("docker-compose.qa.yml") > ln.index("docker-compose.aws.yml"), (
                    f"{path.name} applies the QA overlay BEFORE the AWS one, so QA would "
                    f"inherit production's 80/443 publish: {ln.strip()}"
                )


def test_qa_web_mounts_its_own_config_over_the_baked_production_one():
    """QA now terminates TLS itself, so it DOES mount /etc/letsencrypt — but for
    its own certificate. What must not survive is the baked production nginx
    config, which references traffic-three's certificate; a name this box does
    not hold, so nginx would refuse to start."""
    aws_web = AWS_COMPOSE.read_text(encoding="utf-8")
    assert "/etc/letsencrypt" in aws_web, "guard stale: the AWS overlay no longer mounts certs"
    qa_body = QA_COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^    volumes: !override\s*$", qa_body, re.M), (
        "docker-compose.qa.yml must use `volumes: !override` on web, so the QA "
        "mount list REPLACES the AWS overlay's rather than merging with it"
    )
    active = [ln for ln in qa_body.splitlines() if not ln.lstrip().startswith("#")]
    mounts = [ln.strip().lstrip("- ") for ln in active if ":/etc/nginx/conf.d/default.conf" in ln]
    assert mounts, "docker-compose.qa.yml must mount the QA nginx config over the baked one"
    assert mounts[0].startswith("./web/nginx/default.qa.conf:"), (
        f"the QA nginx config must be bind-mounted by a path RELATIVE to the compose "
        f"project dir, so the checkout can live anywhere. An absolute path silently "
        f"creates an empty DIRECTORY at the mount point on a box that checked out "
        f"elsewhere, and nginx then starts with no server block at all. Got: {mounts[0]}"
    )


def test_the_qa_scripts_scope_every_command_to_the_qa_project():
    """Without -p, compose falls back to the base file's `name:` and would drive
    the production project."""
    for path in (ROOT / "deploy" / "qa" / "manage.sh", ROOT / "deploy" / "qa" / "verify-qa.sh"):
        body = path.read_text(encoding="utf-8")
        assert "-p " in body and QA_PROJECT in body, (
            f"{path.name} must scope compose to -p {QA_PROJECT}"
        )
        assert ".env.qa" in body, f"{path.name} must default to the .env.qa env file"


def test_qa_overlay_hard_codes_no_database():
    """The DSN must come from .env.qa only — the same rule production follows."""
    body = QA_COMPOSE.read_text(encoding="utf-8")
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "POSTGRES_DSN" not in line, f"docker-compose.qa.yml must not set a DSN: {line.strip()}"


# --------------------------------------------------------------------------- #
# Production must be untouched
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path", [BASE_COMPOSE, AWS_COMPOSE, AWS_ENV, ROOT / "web" / "nginx" / "default.conf"]
)
def test_production_files_carry_no_qa_value(path):
    body = path.read_text(encoding="utf-8")
    for needle in (QA_DB, "jnpa-qa", "18000", "18080"):
        assert needle not in body, (
            f"{path.name} contains the QA value {needle!r} — production configuration "
            f"must not be modified to add QA."
        )


def test_production_env_still_targets_the_production_database():
    vals = env_values(AWS_ENV)
    for var in DSN_VARS:
        assert PROD_DB in vals[var], f"{AWS_ENV.name}:{var} no longer targets {PROD_DB}"


def test_production_nginx_still_terminates_tls():
    body = (ROOT / "web" / "nginx" / "default.conf").read_text(encoding="utf-8")
    assert "listen 443 ssl" in body and "ssl_certificate" in body


# --------------------------------------------------------------------------- #
# QA web + secrets hygiene
# --------------------------------------------------------------------------- #
def test_qa_nginx_terminates_tls_for_the_qa_hostname_only():
    conf = ROOT / "web" / "nginx" / "default.qa.conf"
    assert conf.exists(), "docker-compose.qa.yml mounts web/nginx/default.qa.conf"
    body = conf.read_text(encoding="utf-8")
    active = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    active_body = "\n".join(active)

    assert re.search(r"^\s*listen 80;", active_body, re.M), "QA nginx must listen on 80"
    assert re.search(r"^\s*listen 443 ssl;", active_body, re.M), "QA nginx must listen on 443 ssl"
    assert "set $gateway_upstream gateway:8000;" in body, (
        "QA nginx must proxy /api to `gateway`, resolved on the QA network (i.e. "
        "jnpa-qa-gateway). A hard-coded host/IP could reach production's gateway."
    )

    # Every server_name and every certificate path must be QA's. Referencing
    # production's certificate would both leak its name into QA and stop nginx
    # from starting on a box that has no such file.
    for ln in active:
        if "server_name" in ln or "ssl_certificate" in ln:
            assert QA_HOSTNAME in ln, (
                f"QA nginx line must reference {QA_HOSTNAME}, not another host: {ln.strip()}"
            )
        assert PROD_HOSTNAME not in ln, (
            f"QA nginx must never reference the production hostname: {ln.strip()}"
        )

    # `certbot renew --webroot` writes the challenge to the host's /var/www/certbot,
    # which docker-compose.qa.yml bind-mounts. Serving it from anywhere else means
    # renewal 404s and the certificate silently expires after 90 days.
    assert "root /var/www/certbot;" in active_body, (
        "the ACME challenge location must be rooted at the /var/www/certbot bind mount"
    )
    qa_compose = QA_COMPOSE.read_text(encoding="utf-8")
    assert "/var/www/certbot:/var/www/certbot" in qa_compose, (
        "docker-compose.qa.yml must mount the ACME webroot, or `certbot renew` cannot "
        "reach the challenge nginx is serving"
    )

    # The healthcheck target must not be behind the HTTPS redirect — BusyBox wget
    # would follow the 301 and fail the cert's hostname check against `localhost`.
    assert "location = /healthz" in active_body, (
        "QA nginx needs an unredirected /healthz on :80 for the compose healthcheck"
    )

    # The proxy blocks the dashboard/PWA depend on must all be present.
    for loc in ("/api/ws", "/api/", "/minio/", "/pwa/", "/poc3/"):
        assert f"location {loc}" in body, f"QA nginx is missing the {loc} block"


def test_qa_https_redirect_carries_the_port_it_is_actually_served_on():
    """`return 301 https://$host…` drops the port. That is correct ONLY while QA
    owns 443. If the web publish is moved back behind an offset (co-location),
    the redirect must name the port or every plain-HTTP hit dead-ends on a
    closed 443."""
    web_ports = QA["web"]["ports"]
    body = (ROOT / "web" / "nginx" / "default.qa.conf").read_text(encoding="utf-8")
    redirect = [ln for ln in body.splitlines()
                if not ln.lstrip().startswith("#") and "return 301 https://" in ln]
    assert len(redirect) == 1, f"expected exactly one HTTPS redirect, got {redirect}"
    if "443" in web_ports:
        assert "$host$request_uri" in redirect[0], redirect[0]
    else:
        tls_port = next((p for p in web_ports if p != "80"), None)
        assert tls_port and f"$host:{tls_port}$request_uri" in redirect[0], (
            f"web publishes {web_ports}, so the redirect must target the TLS port "
            f"explicitly: {redirect[0].strip()}"
        )


def test_the_filled_in_qa_env_is_git_ignored_but_the_template_is_not():
    lines = [ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert ".env.qa" in lines, ".env.qa holds the real RDS password and must be git-ignored"
    assert ".env.qa.example" not in lines
    assert ".env.qa*" not in lines, "that pattern would also ignore the .env.qa.example template"
