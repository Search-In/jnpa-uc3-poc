-- 0142 — index the identity keys the availability queries correlate on.
--
-- 0140 made a driver's occupancy a constraint. This makes the OTHER half of the
-- rule cheap: availability now matches an open job on the resource's PHYSICAL
-- identity (registration / licence) as well as its surrogate id, because neither
-- master is unique on the human identifier — core.vehicle is unique on
-- vehicle_id, core.driver_identity on driver_id — so one truck can hold two
-- master rows and one person several driver records, and a job raised against
-- one of them was reading as "free" through the other.
--
-- The correlated NOT EXISTS in gateway.fleet / gateway.enrollment normalises
-- both sides (services.container_job.service.SQL_NORMALISE: strip non-alphanumerics,
-- upper-case), so a plain index on the raw column cannot serve it. These are the
-- matching expression indexes, restricted to open jobs — the only rows the
-- predicate ever looks at.
--
-- Additive: no column, table or row changes. Rollback is
--   DROP INDEX IF EXISTS core.idx_job_open_vehicle_no_norm;
--   DROP INDEX IF EXISTS core.idx_job_open_driver_licence_norm;
CREATE INDEX IF NOT EXISTS idx_job_open_vehicle_no_norm
    ON core.container_job_assignment
       (upper(regexp_replace(coalesce(vehicle_no, ''), '[^A-Za-z0-9]', '', 'g')))
    WHERE status NOT IN ('COMPLETED', 'CANCELLED');

CREATE INDEX IF NOT EXISTS idx_job_open_driver_licence_norm
    ON core.container_job_assignment
       (upper(regexp_replace(coalesce(driver_licence, ''), '[^A-Za-z0-9]', '', 'g')))
    WHERE status NOT IN ('COMPLETED', 'CANCELLED');
