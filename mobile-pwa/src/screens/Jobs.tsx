/**
 * Driver Jobs — the UC-III job surface for the driver PWA.
 *
 * The driver sees only their own assigned container jobs (the gateway scopes the
 * list from the device binding on the token) and drives the lifecycle from here:
 *   Accept → Reached gate → Confirm pickup / drop → Complete trip.
 *
 * Each action calls the same backend state machine the control room uses, so a
 * driver tap and an operator click produce identical audit history.
 */
import { useCallback, useEffect, useState } from "react";

import { api } from "../lib/api";
import type { DriverJob, DriverJobStatus } from "../lib/api";
import { Card, Chip, Empty, Row } from "../components/ui";

const STATUS_LABEL: Record<DriverJobStatus, string> = {
  ASSIGNED: "New",
  ACCEPTED: "Accepted",
  AT_GATE: "At gate",
  IN_YARD: "In yard",
  PICKED_UP: "Picked up",
  DROPPED: "Dropped",
  COMPLETED: "Completed",
  CANCELLED: "Cancelled",
};

function isDrop(job: DriverJob): boolean {
  return job.move_type === "EXPORT_DROP" || job.move_type === "EMPTY_DROP";
}

/** The single next action for a job — the driver never has to choose. */
function nextAction(job: DriverJob): { key: string; label: string } | null {
  switch (job.status) {
    case "ASSIGNED":
      return { key: "accept", label: "Accept job" };
    case "ACCEPTED":
      return { key: "gate", label: "Reached gate" };
    case "AT_GATE":
    case "IN_YARD":
      return isDrop(job)
        ? { key: "drop", label: "Confirm drop" }
        : { key: "pickup", label: "Confirm pickup" };
    case "PICKED_UP":
    case "DROPPED":
      return { key: "complete", label: "Complete trip" };
    default:
      return null;
  }
}

export default function Jobs() {
  const [jobs, setJobs] = useState<DriverJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [yard, setYard] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    try {
      const res = await api.myJobs();
      setJobs(res.items);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 20_000);
    return () => clearInterval(t);
  }, [load]);

  async function run(job: DriverJob, action: string) {
    setBusy(job.id);
    setError(null);
    try {
      if (action === "accept") await api.jobAccept(job.id);
      else if (action === "gate") await api.jobGateArrival(job.id, job.gate ?? undefined);
      else if (action === "pickup") await api.jobPickup(job.id, yard[job.id] || undefined);
      else if (action === "drop") await api.jobDrop(job.id, yard[job.id] || undefined);
      else if (action === "complete") await api.jobComplete(job.id);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (loading) return <p className="muted">Loading your jobs…</p>;

  return (
    <div className="stack">
      <h2>My jobs</h2>

      {error && <p className="error">{error}</p>}

      {jobs.length === 0 && <Empty>No jobs assigned to your vehicle right now.</Empty>}

      {jobs.map((job) => {
        const action = nextAction(job);
        const needsYard = action?.key === "pickup" || action?.key === "drop";
        return (
          <Card key={job.id}>
            <div className="row between">
              <strong className="mono">
                {job.container_number || job.group_code || `Job #${job.id}`}
              </strong>
              <Chip
                status={
                  job.status === "COMPLETED"
                    ? "ok"
                    : job.status === "CANCELLED"
                      ? "down"
                      : "open"
                }
              >
                {STATUS_LABEL[job.status]}
              </Chip>
            </div>

            <Row k="Move" v={job.move_type.replace("_", " ").toLowerCase()} />
            <Row k="Terminal" v={job.terminal || "—"} />
            <Row k="Gate" v={job.gate || "—"} />
            <Row k="Vehicle" v={<span className="mono">{job.vehicle_no || job.vehicle_id}</span>} />
            {job.document_reference && (
              <Row
                k="Document"
                v={<span className="mono">{`${job.document_type} ${job.document_reference}`}</span>}
              />
            )}

            {needsYard && (
              <label className="field">
                <span>Yard location</span>
                <input
                  value={yard[job.id] || ""}
                  onChange={(e) => setYard((y) => ({ ...y, [job.id]: e.target.value }))}
                  placeholder="e.g. 2P08D.1"
                  inputMode="text"
                />
              </label>
            )}

            {action ? (
              <button
                type="button"
                className="btn primary"
                onClick={() => void run(job, action.key)}
                disabled={busy === job.id}
                data-testid={`job-${job.id}-${action.key}`}
              >
                {busy === job.id ? "Working…" : action.label}
              </button>
            ) : (
              <p className="muted">
                {job.status === "COMPLETED" ? "Trip complete." : "No action required."}
              </p>
            )}
          </Card>
        );
      })}
    </div>
  );
}
