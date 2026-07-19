// Customs details drawer — a lightweight read-only "Details" experience for an
// ICEGATE capture row on the Customs & Gate page. Opening the drawer fetches the
// full customs document view of one container (GET /api/customs/containers/{cn},
// module 5) and renders it plus a workflow timeline. Data is fetched only while
// the drawer is open (React Query, enabled on containerNo). No new endpoint, no
// new table — it reuses the existing aggregate view.
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Ship, FileText, ScanLine, BadgeCheck, DoorOpen } from "lucide-react";
import { api } from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { StatusChip, type Tone } from "@/components/ui/dtccc";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { fmtDateTimeIST } from "@/lib/utils";
import type { CustomsContainerView } from "@/lib/types";

const NA_IMPORT = "N/A (Import Container)";

/** Value cell — dashes for empty so a field never renders blank. */
function val(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{k}</span>
      <span className="text-sm">{v}</span>
    </div>
  );
}

type StageState = "done" | "pending" | "na";

interface Stage {
  key: string;
  label: string;
  icon: typeof Ship;
  state: StageState;
  detail: string;
}

/** Map the aggregate view onto the fixed customs workflow (import track).
 *  IGM Received -> RMS -> OOC -> LEO -> Gate Release. Export-only stages (LEO)
 *  are N/A for import containers; unavailable stages show "Pending". */
function buildTimeline(v: CustomsContainerView): Stage[] {
  const st = v.status;
  const isImport = v.import_export === "IMPORT" || v.import_export === "TRANSHIPMENT";
  const igmDone = Boolean(st?.declared_igm) || v.igm.length > 0;
  const rmsDone = Boolean(st?.rms_selected);
  const oocDone = Boolean(st?.ooc_cleared);
  const gateDone = v.workflow?.cleared_for_release ?? oocDone;
  return [
    {
      key: "IGM",
      label: "IGM Received",
      icon: Ship,
      state: igmDone ? "done" : "pending",
      detail: igmDone ? val(st?.igm_no ?? v.vessel?.igm_no) : "Pending",
    },
    {
      key: "RMS",
      label: "RMS",
      icon: ScanLine,
      state: rmsDone ? "done" : "pending",
      detail: rmsDone ? "Selected for scanning" : "Pending",
    },
    {
      key: "OOC",
      label: "OOC",
      icon: FileText,
      state: oocDone ? "done" : "pending",
      detail: oocDone ? val(v.ooc[0]?.out_of_charge_no) : "Pending",
    },
    {
      key: "LEO",
      label: "LEO",
      icon: BadgeCheck,
      state: isImport ? "na" : "pending",
      detail: isImport ? NA_IMPORT : "Pending",
    },
    {
      key: "GATE",
      label: "Gate Release",
      icon: DoorOpen,
      state: gateDone ? "done" : "pending",
      detail: gateDone ? "Cleared for release" : "Pending",
    },
  ];
}

const STAGE_TONE: Record<StageState, Tone> = {
  done: "ok",
  pending: "neutral",
  na: "info",
};

function Timeline({ stages }: { stages: Stage[] }) {
  return (
    <ol className="relative ml-2 border-l border-border">
      {stages.map((s) => {
        const Icon = s.icon;
        const done = s.state === "done";
        return (
          <li key={s.key} className="mb-4 ml-4 last:mb-0">
            <span
              className={
                "absolute -left-[9px] flex h-4 w-4 items-center justify-center rounded-full ring-4 ring-card " +
                (done ? "bg-emerald-500" : s.state === "na" ? "bg-sky-500/60" : "bg-muted")
              }
            >
              <Icon className="h-2.5 w-2.5 text-white" />
            </span>
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">{s.label}</span>
              <StatusChip
                label={s.state === "done" ? "Done" : s.state === "na" ? "N/A" : "Pending"}
                tone={STAGE_TONE[s.state]}
              />
            </div>
            <p className="text-xs text-muted-foreground">{s.detail}</p>
          </li>
        );
      })}
    </ol>
  );
}

function Body({ view }: { view: CustomsContainerView }) {
  const isImport = view.import_export === "IMPORT" || view.import_export === "TRANSHIPMENT";
  const st = view.status;
  const vessel = view.vessel;
  const ooc = view.ooc[0];
  const smtp = view.smtp[0];
  // Shipping Bill / LEO are export (SB-keyed) documents — not applicable to an
  // import container. Show the explicit N/A note rather than a blank field.
  const shippingBill = isImport ? NA_IMPORT : "—";
  const leoStatus = isImport ? NA_IMPORT : "Pending";
  const oocStatus = st?.ooc_cleared ? "Cleared (Out-of-Charge)" : "Pending";
  const rmsStatus = st?.rms_selected ? "Selected for scanning" : "Not selected";
  const smtpDetail = smtp ? `${val(smtp.smtp_no)} · Bond ${val(smtp.bond_no)}` : "N/A";
  const lastEvent = view.last_event;

  return (
    <div className="space-y-5 p-4">
      {/* Header chips */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-lg font-semibold">{view.container_no}</span>
        <StatusChip label={view.import_export ?? "UNKNOWN"} tone={isImport ? "info" : "neutral"} />
      </div>

      {/* Workflow timeline */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Customs Workflow
        </h3>
        <Timeline stages={buildTimeline(view)} />
      </section>

      {/* Customs fields */}
      <section className="grid grid-cols-2 gap-x-4 gap-y-3">
        <KV k="IGM Number" v={val(st?.igm_no ?? vessel?.igm_no)} />
        <KV k="Vessel" v={val(vessel?.vessel_code)} />
        <KV k="Voyage" v={val(vessel?.voyage_no)} />
        <KV k="Line Number" v={val(view.igm[0]?.line_no)} />
        <KV k="Bill of Entry" v={val(ooc?.bill_of_entry_no)} />
        <KV k="Shipping Bill" v={shippingBill} />
        <KV k="LEO Status" v={leoStatus} />
        <KV k="OOC Status" v={oocStatus} />
        <KV k="RMS Status" v={rmsStatus} />
        <KV k="SMTP Details" v={smtpDetail} />
        <KV k="Customs Message ID" v={val(view.message_id)} />
        <KV k="Import / Export" v={val(view.import_export)} />
        <KV k="IGM Date" v={val(vessel?.igm_date)} />
        <KV k="ETA" v={vessel?.expected_arrival ? fmtDateTimeIST(vessel.expected_arrival) : "—"} />
        <KV k="Entry Inward" v={vessel?.entry_inward ? fmtDateTimeIST(vessel.entry_inward) : "—"} />
        <KV
          k="Last Customs Event"
          v={
            lastEvent
              ? `${lastEvent.event} · ${fmtDateTimeIST(lastEvent.created_at)}`
              : "Pending"
          }
        />
      </section>
    </div>
  );
}

export default function CustomsDetailsDrawer({
  containerNo,
  onClose,
}: {
  containerNo: string | null;
  onClose: () => void;
}) {
  const open = containerNo !== null;
  const q = useQuery({
    queryKey: ["customs-container", containerNo],
    queryFn: () => api.customsContainer(containerNo as string),
    enabled: open, // fetch only while the drawer is open
  });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent side="right" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle>Customs Details · {containerNo ?? ""}</DialogTitle>
        </DialogHeader>
        {q.isLoading ? (
          <div className="flex items-center justify-center p-10">
            <Spinner />
          </div>
        ) : q.isError ? (
          <div className="p-6">
            <EmptyState>No customs documents found for this container.</EmptyState>
          </div>
        ) : q.data ? (
          <Body view={q.data} />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
