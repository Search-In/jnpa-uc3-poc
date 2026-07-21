// Berthing — vessel lifecycle Timeline dialog (module 7). Opens on a Vessel-List row
// click; reads /api/berthing/{id}/timeline (the call + its accrued lifecycle events).
// Renders the canonical EXPECTED → ARRIVED → BERTH_ASSIGNED → BERTHING_STARTED →
// CARGO_OPERATION → COMPLETED → DEPARTED ladder, marking each milestone reached (with
// its timestamp) vs pending.
import { useQuery } from "@tanstack/react-query";
import { Ship, CheckCircle2, Circle } from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { LoadingState } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { StatusChip, type Tone } from "@/components/ui/dtccc";

const LIFECYCLE = [
  "EXPECTED",
  "ARRIVED",
  "BERTH_ASSIGNED",
  "BERTHING_STARTED",
  "CARGO_OPERATION",
  "COMPLETED",
  "DEPARTED",
];

const LABEL: Record<string, string> = {
  EXPECTED: "Expected",
  ARRIVED: "Arrived at anchorage",
  BERTH_ASSIGNED: "Berth allocated",
  BERTHING_STARTED: "Alongside / berthed",
  CARGO_OPERATION: "Cargo operation",
  COMPLETED: "Operation completed",
  DEPARTED: "Departed",
};

export function statusTone(s?: string): Tone {
  return s === "DEPARTED"
    ? "neutral"
    : s === "COMPLETED"
      ? "ok"
      : s === "CARGO_OPERATION" || s === "BERTHING_STARTED" || s === "BERTH_ASSIGNED"
        ? "info"
        : s === "ARRIVED"
          ? "warn"
          : "neutral";
}

function fmtTs(ts?: string | null): string {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return String(ts);
  }
}

export default function BerthingTimelineDialog({
  reportId,
  onClose,
}: {
  reportId: number | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["berthing-timeline", reportId],
    queryFn: () => api.berthingTimeline(reportId as number),
    enabled: !!reportId,
  });

  const data = q.data;
  const events: any[] = data?.events ?? [];
  const timeByType = new Map<string, string>();
  events.forEach((e) => timeByType.set(e.event_type, e.event_time));
  const reached = new Set(events.map((e) => e.event_type));
  const currentIdx = LIFECYCLE.indexOf(data?.status);

  return (
    <Dialog open={!!reportId} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-mono">
            <Ship size={16} /> {data?.vessel_name ?? "Vessel"}
          </DialogTitle>
        </DialogHeader>

        {q.isLoading ? (
          <div className="py-8">
            <LoadingState />
          </div>
        ) : q.isError ? (
          <div className="py-8 text-center text-sm text-destructive">Unable to load timeline.</div>
        ) : (
          <div className="flex flex-col gap-4">
            {/* Summary */}
            <div className="flex flex-wrap gap-2">
              <StatusChip label={data?.terminal} tone="info" />
              <StatusChip label={`Voyage ${data?.voyage_number}`} tone="neutral" />
              {data?.berth_number && <StatusChip label={`Berth ${data.berth_number}`} tone="neutral" />}
              {data?.shipping_line && <StatusChip label={data.shipping_line} tone="neutral" />}
              <StatusChip label={data?.status} tone={statusTone(data?.status)} />
            </div>

            {/* Lifecycle ladder */}
            <Card className="p-3">
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Vessel lifecycle
              </div>
              <ol className="flex flex-col gap-1.5">
                {LIFECYCLE.map((step, i) => {
                  const done = reached.has(step) || (currentIdx >= 0 && i <= currentIdx);
                  const t = timeByType.get(step);
                  return (
                    <li key={step} className="flex items-center gap-3 text-[13px]">
                      {done ? (
                        <CheckCircle2 size={16} className="text-ok" />
                      ) : (
                        <Circle size={16} className="text-muted-foreground/40" />
                      )}
                      <span className={done ? "font-medium text-foreground" : "text-muted-foreground"}>
                        {LABEL[step]}
                      </span>
                      <span className="ml-auto tabular-nums text-muted-foreground">{fmtTs(t)}</span>
                    </li>
                  );
                })}
              </ol>
            </Card>

            {/* Key timestamps */}
            <Card className="p-3 text-[13px]">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Reported times (IST)
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
                <Field label="ETA" value={fmtTs(data?.eta)} />
                <Field label="ATA" value={fmtTs(data?.ata)} />
                <Field label="Berthing" value={fmtTs(data?.berthing_time)} />
                <Field label="Ops start" value={fmtTs(data?.cargo_operation_start)} />
                <Field label="Ops end" value={fmtTs(data?.cargo_operation_end)} />
                <Field label="Departure" value={fmtTs(data?.departure_time)} />
              </dl>
            </Card>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, value }: { label: string; value?: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium text-foreground">{value || "—"}</dd>
    </>
  );
}
