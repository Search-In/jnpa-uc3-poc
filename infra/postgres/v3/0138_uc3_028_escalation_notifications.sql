-- 0138 — UC3-028: the N/2N/3N escalation ladder and per-channel delivery log.
--
-- The enforcement console could file a case, hash its evidence and issue a
-- challan, but nothing recorded WHO was told, THROUGH WHICH CHANNEL, and WHETHER
-- IT ARRIVED. UI-114 makes that the deliverable: "first alert at N minutes,
-- escalation at 2N, enforcement notification at 3N", with recipients drawn from
-- the vehicle-owner data over SMS/Email/WhatsApp and "each delivery channel
-- recorded".
--
-- Two tables, because they answer two different questions:
--
--   core.violation_escalation    WHICH RUNG a case has reached, and when it is
--                                due to climb. One row per (case, rung), so the
--                                ladder is a ledger rather than a status column
--                                that forgets its own history. The unique key
--                                makes the ladder idempotent: re-running the
--                                evaluator cannot fire the same rung twice, which
--                                is what stops a stuck scheduler from spamming a
--                                transporter.
--
--   core.notification_delivery   ONE ROW PER CHANNEL PER RUNG, carrying the
--                                recipient actually resolved, the provider, and
--                                the delivery state. A single "notified: true"
--                                flag cannot express "SMS delivered, WhatsApp
--                                failed, email queued", which is precisely the
--                                situation an enforcement audit asks about.
--
-- Delivery state vocabulary is CHECK-constrained rather than free text, because
-- the difference between SENT (we handed it to a provider) and DELIVERED (the
-- provider confirmed) is the difference between a defensible audit and a claim.
-- UNAVAILABLE is a first-class state: no SMS provider is configured pre-award,
-- and recording "we could not send" is honest where recording SENT would not be.
--
-- Fully ADDITIVE and idempotent. No existing table is touched.
BEGIN;

-- ---------------------------------------------------------------- the ladder
CREATE TABLE IF NOT EXISTS core.violation_escalation (
    escalation_id  bigserial PRIMARY KEY,
    case_id        uuid NOT NULL,
    -- 1 = first alert at N, 2 = escalation at 2N, 3 = enforcement notice at 3N.
    rung           integer NOT NULL CHECK (rung IN (1, 2, 3)),
    rung_label     text NOT NULL,
    -- The configured N for the zone this case sits in, kept ON THE ROW so a
    -- later change to zone config cannot silently rewrite what was due when.
    n_minutes      integer NOT NULL CHECK (n_minutes > 0),
    due_after_min  integer NOT NULL CHECK (due_after_min > 0),
    zone_id        text,
    fired_at       timestamptz NOT NULL DEFAULT now(),
    -- Idempotency: a rung fires at most once per case, so a re-run of the
    -- evaluator is a no-op rather than a second message to the same person.
    UNIQUE (case_id, rung)
);

CREATE INDEX IF NOT EXISTS idx_violation_escalation_case
    ON core.violation_escalation (case_id, rung);

COMMENT ON TABLE core.violation_escalation IS
    'UI-114 escalation ladder: first alert at N minutes, escalation at 2N, '
    'enforcement notification at 3N. UNIQUE(case_id, rung) makes firing idempotent.';

-- ------------------------------------------------------- per-channel delivery
CREATE TABLE IF NOT EXISTS core.notification_delivery (
    delivery_id    bigserial PRIMARY KEY,
    case_id        uuid NOT NULL,
    escalation_id  bigint REFERENCES core.violation_escalation (escalation_id)
                          ON DELETE CASCADE,
    rung           integer,
    channel        text NOT NULL CHECK (channel IN ('SMS', 'EMAIL', 'WHATSAPP')),
    -- Who was told, and on what authority. recipient_source names the table the
    -- address came from, so an auditor can retrace it rather than trust it.
    recipient_role text NOT NULL CHECK (recipient_role IN
                        ('OWNER', 'TRANSPORTER', 'TRAFFIC_POLICE', 'DRIVER')),
    recipient      text,
    recipient_name text,
    recipient_source text,
    -- QUEUED  accepted for sending
    -- SENT    handed to a provider
    -- DELIVERED provider confirmed receipt
    -- FAILED  provider rejected or errored
    -- UNAVAILABLE no provider configured, or no address for this recipient
    status         text NOT NULL DEFAULT 'QUEUED'
                        CHECK (status IN ('QUEUED', 'SENT', 'DELIVERED',
                                          'FAILED', 'UNAVAILABLE')),
    provider       text,
    detail         text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notification_delivery_case
    ON core.notification_delivery (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_status
    ON core.notification_delivery (status, created_at DESC);

COMMENT ON TABLE core.notification_delivery IS
    'One row per CHANNEL per escalation rung. A single notified flag cannot say '
    '"SMS delivered, WhatsApp failed, email queued" — which is exactly what an '
    'enforcement audit asks. UNAVAILABLE records "could not send" honestly, '
    'where SENT would be a claim.';

-- --------------------------------------------------- field verification (EC-5)
-- When ANPR cannot read the plate there is no owner to notify, so the case goes
-- to a human instead of being dropped or guessed at.
CREATE TABLE IF NOT EXISTS core.field_verification_task (
    task_id       bigserial PRIMARY KEY,
    case_id       uuid NOT NULL UNIQUE,
    reason        text NOT NULL DEFAULT 'PLATE_UNREADABLE',
    assigned_to   text NOT NULL DEFAULT 'TRAFFIC_MARSHAL',
    evidence_url  text,
    evidence_sha256 text,
    zone_id       text,
    status        text NOT NULL DEFAULT 'OPEN'
                       CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'CANCELLED')),
    resolved_plate text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_field_verification_status
    ON core.field_verification_task (status, created_at DESC);

COMMENT ON TABLE core.field_verification_task IS
    'EC-5: an unreadable plate produces a task for a traffic marshal with the '
    'photo evidence attached. The alternative — guessing the plate — would '
    'notify the wrong owner.';

COMMIT;
