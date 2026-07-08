-- ===========================================================================
-- P0 demo scenario timelines — TFC-2 and TFC-3
-- ---------------------------------------------------------------------------
-- Adds two more recorded demo timelines so the What-If Console "Demo scenarios"
-- list covers all three scenario types (TFC-1 is seeded by seed_demo_p0.sql).
-- These are read-only recorded runs (handle_id prefix `demo-`, status
-- 'completed', detail.source='DEMO', sim:true). They surface through the RDS
-- read paths (GET /api/scenarios/handles, timeline fallback) and NEVER touch
-- the live scenario-runner flow.
--
-- Idempotent (WHERE NOT EXISTS). Apply:
--   docker exec -i jnpa-postgres psql -U postgres -d postgres < scripts/seed_demo_scenarios.sql
-- ===========================================================================

-- TFC-2 : wrong-way track → anomaly → e-Challan -----------------------------
INSERT INTO jnpa.scenario_handles (handle_id, name, status, params, trace_id, started_at, ended_at)
SELECT 'demo-tfc2-0001', 'tfc2', 'completed',
       '{"source":"DEMO","sim":true,"camera_id":"C-KARAL-EXIT"}'::jsonb,
       'demo-trace-0002', now() - interval '20 min', now() - interval '15 min'
WHERE NOT EXISTS (SELECT 1 FROM jnpa.scenario_handles WHERE handle_id = 'demo-tfc2-0001');

INSERT INTO jnpa.scenario_steps (handle_id, step_no, ts, title, status, trigger, detail)
SELECT 'demo-tfc2-0001', s.step_no, now() - interval '20 min' + (s.step_no || ' min')::interval,
       s.title, s.status, s.trigger, '{"source":"DEMO","sim":true}'::jsonb
FROM (VALUES
    (1, 'Wrong-way track injected (Karal Phata)', 'info',     'inject'),
    (2, 'Anomaly detector fired',                 'degraded', 'anomaly'),
    (3, 'ANPR plate resolved via Vahan',          'ok',       'anpr'),
    (4, 'Evidence snapshot stored (MinIO)',       'ok',       'evidence'),
    (5, 'e-Challan issued',                       'ok',       'echallan')
) AS s(step_no, title, status, trigger)
WHERE NOT EXISTS (SELECT 1 FROM jnpa.scenario_steps WHERE handle_id = 'demo-tfc2-0001');

-- TFC-3 : DPD release spike → demand surge → gate-slot reissue ---------------
INSERT INTO jnpa.scenario_handles (handle_id, name, status, params, trace_id, started_at, ended_at)
SELECT 'demo-tfc3-0001', 'tfc3', 'completed',
       '{"source":"DEMO","sim":true,"dpd_release_spike":2.5}'::jsonb,
       'demo-trace-0003', now() - interval '12 min', now() - interval '6 min'
WHERE NOT EXISTS (SELECT 1 FROM jnpa.scenario_handles WHERE handle_id = 'demo-tfc3-0001');

INSERT INTO jnpa.scenario_steps (handle_id, step_no, ts, title, status, trigger, detail)
SELECT 'demo-tfc3-0001', s.step_no, now() - interval '12 min' + (s.step_no || ' min')::interval,
       s.title, s.status, s.trigger, s.detail::jsonb
FROM (VALUES
    (1, 'UC-II DPD release spike 2.5x',         'info',     'dpd',      '{"source":"DEMO","sim":true}'),
    (2, 'Corridor demand surge detected',        'degraded', 'demand',   '{"source":"DEMO","sim":true}'),
    (3, 'Forecaster predicts build-up',          'info',     'forecast', '{"source":"DEMO","sim":true}'),
    (4, 'Gate-slot reissue advised',             'ok',       'reissue',  '{"source":"DEMO","sim":true}'),
    (5, 'Cross-twin handoff UC-II -> UC-III',    'ok',       'handoff',  '{"source":"DEMO","sim":true,"arrow":{"from":"UC-II","to":"UC-III"}}')
) AS s(step_no, title, status, trigger, detail)
WHERE NOT EXISTS (SELECT 1 FROM jnpa.scenario_steps WHERE handle_id = 'demo-tfc3-0001');

-- Summary -------------------------------------------------------------------
SELECT h.handle_id, h.name, h.status,
       (SELECT count(*) FROM jnpa.scenario_steps s WHERE s.handle_id = h.handle_id) AS steps
FROM jnpa.scenario_handles h
WHERE h.handle_id IN ('demo-tfc1-0001','demo-tfc2-0001','demo-tfc3-0001')
ORDER BY h.handle_id;

-- Rollback:
--   DELETE FROM jnpa.scenario_steps   WHERE handle_id IN ('demo-tfc2-0001','demo-tfc3-0001');
--   DELETE FROM jnpa.scenario_handles WHERE handle_id IN ('demo-tfc2-0001','demo-tfc3-0001');
