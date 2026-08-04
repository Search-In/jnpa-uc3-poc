# RDS Security — JNPA UC-III

**Status:** required reading before filling in any `POSTGRES_DSN`.
**Owner:** deployment engineer · **Last reviewed:** 2026-08-04

The UC-III application database (`jnpa_schema_v3`) is not an ordinary demo store.
It holds, in cleartext at rest:

| Table | Rows | Personal data |
|---|---:|---|
| `core.driver` | 31,846 | full name, driving licence number, date of birth |
| `core.pdp` | 367,078 | port-entry permit lineage per driver |
| `core.transporter` | 2,194 | contact person, email, mobile, postal address |
| `core.driver_identity` | — | licence number, mobile, masked Aadhaar, face template ref |
| `core.eir` / `core.gate_document` | — | driver licence transcribed off physical gate slips |

Under the DPDP Act this is personal data of identifiable individuals who are not
party to this PoC. Treat the database as production, not as demo scaffolding.

---

## 1. What went wrong (the finding this document closes)

The 2026-08-04 production-readiness audit established, by direct test:

1. The RDS endpoint hostname was committed in cleartext across 24 files —
   `.env*.example`, `deploy/`, `scripts/`, `docs/`, and `.github/workflows/`.
2. Every DSN used the **`postgres` superuser**.
3. The instance accepted a connection **from a developer laptop over the public
   internet**, with no VPN and no bastion.

Together those three facts reduce the entire PII corpus above to *one guessed or
leaked password*. Items 1 and 2 are fixed in the repository (see §5). **Item 3 is
an AWS console change and is not fixed by any commit** — it is the action item
below.

---

## 2. Network — restrict the security group

The instance must not be reachable from the internet.

```bash
# What is the instance's SG and is it public?
aws rds describe-db-instances \
  --db-instance-identifier <instance-id> \
  --query 'DBInstances[0].{public:PubliclyAccessible,sg:VpcSecurityGroups[].VpcSecurityGroupId}'

# Any rule with CidrIp 0.0.0.0/0 on 5432 is the finding.
aws ec2 describe-security-groups --group-ids <sg-id> \
  --query 'SecurityGroups[0].IpPermissions'
```

Remediate:

```bash
# 1. Drop the world-open ingress rule.
aws ec2 revoke-security-group-ingress \
  --group-id <sg-id> --protocol tcp --port 5432 --cidr 0.0.0.0/0

# 2. Allow ONLY the application security group (EC2 running the stack).
aws ec2 authorize-security-group-ingress \
  --group-id <sg-id> --protocol tcp --port 5432 \
  --source-group <app-sg-id>

# 3. Turn off public accessibility entirely.
aws rds modify-db-instance \
  --db-instance-identifier <instance-id> \
  --no-publicly-accessible --apply-immediately
```

**Operator access** (imports, `make migrate`, `make psql`) then goes through one
of: the EC2 host itself, a session-manager port-forward, or a temporary
CIDR-scoped rule for a single known IP that is revoked afterwards. Never a
standing `0.0.0.0/0`.

```bash
# Session-manager tunnel — no inbound rule needed at all.
aws ssm start-session --target <ec2-instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<rds-endpoint>"],"portNumber":["5432"],"localPortNumber":["5432"]}'
```

---

## 3. Least-privilege application role

The application must not connect as `postgres`. Create `jnpa_app`, which can read
and write rows but cannot drop schemas, create roles, or read other databases.

```sql
-- Run ONCE as the superuser, connected to jnpa_schema_v3.
CREATE ROLE jnpa_app LOGIN PASSWORD '<strong-random>';

REVOKE ALL ON DATABASE jnpa_schema_v3 FROM PUBLIC;
GRANT CONNECT ON DATABASE jnpa_schema_v3 TO jnpa_app;

GRANT USAGE ON SCHEMA core, mart, staging TO jnpa_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core     TO jnpa_app;
GRANT SELECT                                ON ALL TABLES IN SCHEMA mart    TO jnpa_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA staging  TO jnpa_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core, staging TO jnpa_app;

-- Objects created by LATER migrations inherit the same grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA core, staging
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO jnpa_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA mart
  GRANT SELECT ON TABLES TO jnpa_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA core, staging
  GRANT USAGE, SELECT ON SEQUENCES TO jnpa_app;

-- Explicitly deny DDL: migrations run as the superuser, the app never does.
REVOKE CREATE ON SCHEMA core, mart, staging FROM jnpa_app;
```

Then set `__RDS_USER__=jnpa_app` in every DSN. `make migrate` is the **only**
thing that should ever use the superuser, and only for the duration of a
migration run.

Verify the role cannot escalate:

```bash
psql "postgresql://jnpa_app:<pw>@<host>:5432/jnpa_schema_v3?sslmode=require" \
  -c "DROP TABLE core.driver;"          # expect: ERROR: permission denied
psql "postgresql://jnpa_app:<pw>@<host>:5432/jnpa_schema_v3?sslmode=require" \
  -c "SELECT count(*) FROM core.driver;" # expect: 31846
```

---

## 4. Credential handling

| Secret | Where it lives | Never |
|---|---|---|
| `RDS_PASSWORD` | GitHub Actions secret + `.env.local` (untracked) | in git, in a `.example`, in a docstring |
| `RDS_HOST` | GitHub Actions secret + `.env.local` | committed — see §1 |
| `RDS_USER` | GitHub Actions secret (default `jnpa_app`) | `postgres` |

Rotate the password after any exposure, including this audit:

```bash
aws rds modify-db-instance --db-instance-identifier <instance-id> \
  --master-user-password '<new-strong-password>' --apply-immediately
```

Then update the GitHub secret and every deployed `.env`, and re-run
`bash deploy/aws/verify-rds.sh`.

---

## 5. What the repository now enforces

These are committed and testable (`tests/test_rds_security.py`):

* **No RDS hostname anywhere in the tree.** All 24 occurrences are now
  `__RDS_HOST__`. A test greps the repo and fails if a real
  `*.rds.amazonaws.com` endpoint reappears.
* **No `postgres` superuser in any example DSN** — `.env.local.example`,
  `.env.aws.example`, `deploy/.env.prod.example` and `deploy/aws/rds.env.patch`
  all use `__RDS_USER__`.
* **Deploy scripts require the endpoint.** `use-rds.sh`, `verify-rds.sh` and
  `rds-preflight.sh` use `${RDS_HOST:?...}` — they abort rather than fall back to
  a baked-in host.
* **CI/CD reads the endpoint and user from repository secrets**
  (`.github/workflows/deploy.yml`), and aborts if `RDS_HOST` is unset.
* **`GRAFANA_PG_HOST` is required** in `docker-compose.yml` rather than
  defaulting to the literal endpoint.

## 6. Application-layer defence (defence in depth)

Network and role hardening protect the database. `gateway/pii.py` +
`shared/jnpa_shared/pii.py` protect the *API*: driver licences, dates of birth,
mobiles, emails and addresses are masked in every response unless the caller's
role is in `PII_UNMASK_ROLES` (default `DTCCC_ADMIN,CUSTOMS`), and masking fails
closed when there is no authenticated principal. See `docs/UC3_PRODUCTION_AUDIT.md`
and `tests/test_pii_masking.py`.

---

## 7. Pre-demo checklist

- [ ] §2 — SG has no `0.0.0.0/0` on 5432; `PubliclyAccessible=false`
- [ ] §3 — every deployed DSN uses `jnpa_app`, not `postgres`
- [ ] §4 — master password rotated after the 2026-08-04 audit exposure
- [ ] §4 — `RDS_HOST` / `RDS_USER` / `RDS_PASSWORD` set as GitHub secrets
- [ ] §5 — `pytest tests/test_rds_security.py` green
- [ ] §6 — `pytest tests/test_pii_masking.py` green
- [ ] `bash deploy/aws/verify-rds.sh` green from the EC2 host
