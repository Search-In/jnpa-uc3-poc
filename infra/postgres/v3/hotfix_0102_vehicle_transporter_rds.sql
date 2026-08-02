-- ============================================================================
-- hotfix_0102_vehicle_transporter_rds.sql
-- RDS jnpa_schema_v3 was found (2026-07-31) without 0102's extension columns
-- on core.transporter / core.vehicle (base 0101 shapes only) — breaking the
-- ported demo seeds and the Vehicle Management API columns. This applies the
-- transporter + vehicle sections of 0102 idempotently (IF NOT EXISTS guards),
-- mirroring 0102's DDL exactly. The driver/pdp sections of 0102 are ALSO
-- missing on RDS but rewrite a large table — apply them via 0102 itself in a
-- maintenance window (tracked in docs/REMEDIATION_PLAN.md Phase 3 / C4).
-- Run: psql "$DSN" -v ON_ERROR_STOP=1 -f infra/postgres/v3/hotfix_0102_vehicle_transporter_rds.sql
-- ============================================================================
BEGIN;

-- --------------------------------------------------------------- transporter
CREATE SEQUENCE IF NOT EXISTS core.transporter_id_seq;
CREATE SEQUENCE IF NOT EXISTS core.transporter_company_id_seq;

ALTER TABLE core.transporter
    ADD COLUMN IF NOT EXISTS id         bigint DEFAULT nextval('core.transporter_id_seq') NOT NULL,
    ADD COLUMN IF NOT EXISTS code       text,
    ADD COLUMN IF NOT EXISTS gstin      text,
    ADD COLUMN IF NOT EXISTS contact    text,
    ADD COLUMN IF NOT EXISTS status     text DEFAULT 'ACTIVE' NOT NULL,
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now() NOT NULL,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now() NOT NULL;

DO $$ BEGIN
    ALTER TABLE core.transporter ADD CONSTRAINT uq_transporter_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_transporter_code
    ON core.transporter (code) WHERE code IS NOT NULL;

ALTER TABLE core.transporter
    ALTER COLUMN company_id SET DEFAULT nextval('core.transporter_company_id_seq');

-- keep the sequence ahead of existing company_ids so API inserts don't collide
SELECT setval('core.transporter_company_id_seq',
              GREATEST(COALESCE((SELECT max(company_id) FROM core.transporter), 0), 1));

DO $$ BEGIN
    CREATE TRIGGER trg_transporter_updated_at BEFORE UPDATE ON core.transporter
        FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------------------- vehicle
ALTER TABLE core.vehicle
    ADD COLUMN IF NOT EXISTS id             uuid DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS vehicle_id     text,
    ADD COLUMN IF NOT EXISTS vehicle_type   text,
    ADD COLUMN IF NOT EXISTS chassis_number text,
    ADD COLUMN IF NOT EXISTS rfid_fastag_id text,
    ADD COLUMN IF NOT EXISTS status         text DEFAULT 'ACTIVE',
    ADD COLUMN IF NOT EXISTS created_by     text,
    ADD COLUMN IF NOT EXISTS created_at     timestamptz DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at     timestamptz DEFAULT now();

DO $$ BEGIN
    ALTER TABLE core.vehicle ADD CONSTRAINT uq_vehicle_ext_id UNIQUE (id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_vehicle_vehicle_id
    ON core.vehicle (vehicle_id) WHERE vehicle_id IS NOT NULL;

DO $$ BEGIN
    CREATE TRIGGER trg_vehicle_updated_at BEFORE UPDATE ON core.vehicle
        FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMIT;
