-- Deterministic verification of the event-driven Appendix-C gate KPIs.
--
-- Inserts 8 fully-completed synthetic gate trips (clearly tagged SEED / TRK-SEED
-- so they are obviously test data) with realistic durations:
--   queue wait = 7 min, gate transaction = 3 min, turn-around inside = 88 min.
-- Then reads the KPI views back so you can confirm the aggregation is correct.
--
-- Run:  psql "postgresql://postgres:jnpa_pw@localhost:5433/postgres" -f scripts/verify_gate_kpi.sql
-- After confirming /api/kpi/strip, remove the seed rows with the DELETE at the end.

\echo '== inserting 8 seed gate trips =='
WITH trips AS (
    SELECT gs AS i,
           now() - (gs || ' minutes')::interval - interval '100 minutes' AS arrival
    FROM generate_series(0, 105, 15) gs
)
INSERT INTO jnpa.gate_events (ts, device_id, plate, gate_id, trip_id, event_type, lat, lon)
SELECT arrival,                         'TRK-SEED'||i, 'MH04SEED'||i, 'G-NSICT', 'SEED:'||i, 'GATE_ARRIVAL',   18.9489, 72.9492 FROM trips
UNION ALL
SELECT arrival + interval '7 minutes',  'TRK-SEED'||i, 'MH04SEED'||i, 'G-NSICT', 'SEED:'||i, 'GATE_TXN_START', 18.9489, 72.9492 FROM trips
UNION ALL
SELECT arrival + interval '10 minutes', 'TRK-SEED'||i, 'MH04SEED'||i, 'G-NSICT', 'SEED:'||i, 'GATE_IN',         18.9489, 72.9492 FROM trips
UNION ALL
SELECT arrival + interval '98 minutes', 'TRK-SEED'||i, 'MH04SEED'||i, 'G-NSICT', 'SEED:'||i, 'GATE_OUT',        18.9489, 72.9492 FROM trips;

\echo '== per-trip pivoted timeline (expect 4 timestamps per trip) =='
SELECT * FROM jnpa.kpi_gate_trip_timeline WHERE trip_id LIKE 'SEED:%' ORDER BY arrival_ts;

\echo '== KPI 1 Gate Queue Wait (expect ~7.0 min) =='
SELECT * FROM jnpa.kpi_gate_queue_wait;
\echo '== KPI 2 Gate Transaction Time (expect ~3.0 min) =='
SELECT * FROM jnpa.kpi_gate_txn_time;
\echo '== KPI 4 Turn-Around Inside Port (expect ~88.0 min) =='
SELECT * FROM jnpa.kpi_tat_inside_port;

-- Cleanup (uncomment to remove the seed rows):
-- DELETE FROM jnpa.gate_events WHERE trip_id LIKE 'SEED:%';
