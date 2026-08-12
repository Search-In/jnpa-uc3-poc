-- 0140 — one open container job per driver (the missing half of "one job, one crew").
--
-- 0113 gave core.container_job_assignment uq_job_open_vehicle and
-- uq_job_open_container, so a truck and a box can each hold at most one open
-- job. The DRIVER had no equivalent: validation checked nothing at all, and the
-- Assign-Job dropdown listed every ACTIVE enrolled driver, so the same person
-- could be dispatched on two trucks at once. One person cannot drive two trucks.
--
-- The predicate mirrors the application exactly:
--   * occupied  = status NOT IN ('COMPLETED','CANCELLED')
--                 (services.container_job.service TERMINAL — the same set
--                  uq_job_open_vehicle uses, and the same one
--                  gateway.enrollment.list_assignable_drivers excludes on)
--   * key       = driver_id, NULLs excluded (a job may legitimately carry no
--                 driver for the move types outside DRIVER_REQUIRED_MOVE_TYPES)
-- so the index and ContainerJobService.validate_assignment can never disagree.
-- The index is the real guard under concurrency; the pre-flight check exists to
-- return a friendly `driver_already_assigned` rather than a constraint violation
-- (ContainerJobRepository.create_job maps this index name to that error).
--
-- Additive: no column, table or row is changed. Rollback is
--   DROP INDEX IF EXISTS core.uq_job_open_driver;
-- which restores the previous behaviour exactly.
--
-- PRE-FLIGHT: this fails if a driver ALREADY holds two open jobs. Find them with
--   SELECT driver_id, count(*), array_agg(id)
--     FROM core.container_job_assignment
--    WHERE driver_id IS NOT NULL AND status NOT IN ('COMPLETED','CANCELLED')
--    GROUP BY driver_id HAVING count(*) > 1;
-- and cancel the surplus jobs (POST /api/jobs/{id}/cancel) before applying —
-- deliberately NOT auto-resolved here, because which job is the real one is an
-- operational decision, not a migration's to make.
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_open_driver
    ON core.container_job_assignment (driver_id)
    WHERE driver_id IS NOT NULL
      AND status NOT IN ('COMPLETED', 'CANCELLED');
