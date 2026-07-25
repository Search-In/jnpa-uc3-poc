-- 0203  Batched backfill for high-volume time-series tables.
-- Commits every batch; safe to re-run (resumes past max(ts)).
SET session_replication_role = replica;

DO $$
DECLARE lo timestamptz; hi timestamptz; cur timestamptz; step interval := interval '30 minutes';
BEGIN
  SELECT coalesce(max(ts), '-infinity') INTO lo FROM core.truck_telemetry;
  SELECT min(ts), max(ts) INTO cur, hi FROM jnpa.truck_telemetry;
  IF lo > '-infinity' THEN cur := lo;
  ELSE cur := cur - interval '1 microsecond';  -- include the min(ts) row itself
  END IF;
  WHILE cur <= hi LOOP
    INSERT INTO core.truck_telemetry
      SELECT * FROM jnpa.truck_telemetry WHERE ts > cur AND ts <= cur + step;
    cur := cur + step;
    COMMIT;
  END LOOP;
END $$;

DO $$
DECLARE lo timestamptz; hi timestamptz; cur timestamptz; step interval := interval '30 minutes';
BEGIN
  SELECT coalesce(max(ts), '-infinity') INTO lo FROM core.rfid_read;
  SELECT min(ts), max(ts) INTO cur, hi FROM jnpa.rfid_reads;
  IF lo > '-infinity' THEN cur := lo;
  ELSE cur := cur - interval '1 microsecond';  -- include the min(ts) row itself
  END IF;
  WHILE cur <= hi LOOP
    INSERT INTO core.rfid_read
      SELECT * FROM jnpa.rfid_reads WHERE ts > cur AND ts <= cur + step;
    cur := cur + step;
    COMMIT;
  END LOOP;
END $$;

RESET session_replication_role;
