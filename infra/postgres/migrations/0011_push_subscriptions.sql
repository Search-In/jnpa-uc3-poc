-- 0011_push_subscriptions.sql
-- Persist driver push registrations (WebPush + Firebase FCM device tokens).
--
-- Before this migration the WebPush subscription lived ONLY in an in-memory dict
-- on the gateway (gateway/routers/push.py::SUBSCRIPTIONS) and was lost on every
-- restart. This table gives both transports a durable, per-device home and adds
-- a first-class FCM token column — additive only, no existing table is altered.
--
-- Keyed on device_id, the same key used by jnpa.device_bindings (the OTP pairing
-- table) and by the WebPush subscribe flow, so it aligns with the existing
-- session model without a schema change elsewhere.
--
-- NOTE: the gateway also self-provisions this DDL at runtime (push.py::_ensure),
-- mirroring the otp.py pattern, so it is present even when migrations are not
-- run against an already-initialised database.

CREATE SCHEMA IF NOT EXISTS jnpa;

CREATE TABLE IF NOT EXISTS jnpa.push_subscriptions (
    device_id    text PRIMARY KEY,
    driver_id    text,
    vehicle_id   text,
    -- WebPush leg (nullable when only FCM is registered)
    webpush      jsonb,
    -- Firebase Cloud Messaging leg (nullable when only WebPush is registered)
    fcm_token    text,
    platform     text NOT NULL DEFAULT 'web',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_push_subs_fcm
    ON jnpa.push_subscriptions (fcm_token)
    WHERE fcm_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_push_subs_driver
    ON jnpa.push_subscriptions (driver_id);
