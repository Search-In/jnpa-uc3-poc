// UC3-040 — the Auto-LEO four-way join, as a PANEL.
//
// Tender UC3-R5: "Vehicle & container identification, e-seal data, Form 13,
// weighbridge data, ICEGATE data capturing for Auto-LEO process". One row per
// export container; leo_ready lights only when all four streams pass.
//
// This is a panel, not a screen. Gate & Customs already owns an "Auto-LEO" tab
// (and Live Operations already shows an Auto-LEO queue card), so a third
// standalone Auto-LEO screen would have given the operator two places to read
// the same reconciliation and no way to know which was authoritative. The panel
// upgrades the existing tab in place.
//
// Two things it refuses to blur:
//
//  * **MISSING is not MISMATCH.** Each stream shows its own state, because "the
//    weighbridge disagreed" and "the weighbridge never reported" are different
//    operational problems with different remedies. One red/green per row hides
//    that; the previous LEO table did exactly that.
//  * **Which half is real.** Form 13 values come from the customer's own
//    documents; the weighbridge, e-seal and ICEGATE readings are simulated
//    around them (gaps G8/G10). Every stream carries its provenance.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BadgeCheck,
  FileText,
  Flag,
  PackageCheck,
  Scale,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";

import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  DataTable,
  StatCard,
  StatGrid,
  StatusChip,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import type { AutoLeoRow, LeoSourceState } from "@/lib/types";

const REFRESH_MS = 15_000;

const STREAMS = [
  { key: "eseal", label: "e-Seal", icon: ShieldCheck },
  { key: "form13", label: "Form 13", icon: FileText },
  { key: "weighbridge", label: "Weighbridge", icon: Scale },
  { key: "icegate", label: "ICEGATE", icon: PackageCheck },
] as const;

const SOURCE_TONE: Record<LeoSourceState, Tone> = {
  MATCH: "ok",
  MISMATCH: "critical",
  MISSING: "warn",
};

/** The four stream chips for one row — the four-way join, made legible. */
function StreamChips({ row }: { row: AutoLeoRow }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {STREAMS.map(({ key, label, icon: Icon }) => {
        const state = row.sources?.[key] ?? "MISSING";
        const provenance = row.evidence?.[key]?.provenance;
        return (
          <span
            key={key}
            title={`${label}: ${state}${provenance ? ` (${provenance})` : ""}`}
            className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border bg-muted/40 px-1.5 py-0.5 text-[10px]"
          >
            <Icon className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
            <span className="font-medium">{label}</span>
            <StatusChip label={state} tone={SOURCE_TONE[state]} />
          </span>
        );
      })}
    </div>
  );
}

function RowDetail({ row }: { row: AutoLeoRow }) {
  const checks = row.checks as Record<string, unknown>;
  const declared = row.form13_document?.declared_wt_kg;
  const measured = checks.weighbridge_measured_wt_kg as number | null;
  const pct = checks.weight_discrepancy_pct as number | null;

  return (
    <div className="flex flex-col gap-2.5 rounded-lg border border-border bg-muted/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        {row.leo_ready ? (
          <StatusChip label="LEO READY" tone="ok" />
        ) : (
          <StatusChip label="BLOCKED" tone="critical" />
        )}
        {row.anchored_to_real_document && <StatusChip label="REAL FORM 13" tone="info" />}
        {row.customs_flags.map((f) => (
          <StatusChip key={f} label={f} tone="critical" />
        ))}
      </div>

      {row.form13_document && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
          <div className="min-w-0">
            <dt className="text-[10px] uppercase text-muted-foreground">Form 13 no</dt>
            <dd className="break-all font-mono text-[12px]">
              {row.form13_document.doc_ref ?? "—"}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-[10px] uppercase text-muted-foreground">Customs seal</dt>
            <dd className="break-all font-mono text-[12px]">
              {row.form13_document.custom_seal_no ?? "—"}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-[10px] uppercase text-muted-foreground">Declared VGM</dt>
            <dd className="text-[12px] tabular-nums">
              {declared !== null && declared !== undefined
                ? `${declared.toLocaleString("en-IN")} kg`
                : "—"}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-[10px] uppercase text-muted-foreground">Weighbridge</dt>
            <dd className="text-[12px] tabular-nums">
              {measured ? `${measured.toLocaleString("en-IN")} kg` : "not reported"}
              {pct !== null && pct !== undefined && (
                <span className="ml-1 text-[10px] text-muted-foreground">({pct}%)</span>
              )}
            </dd>
          </div>
        </dl>
      )}

      {row.weighbridge_reroute && (
        <p className="flex items-start gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] leading-snug text-amber-700 dark:text-amber-400">
          <TriangleAlert className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
          <span className="min-w-0">
            Weighbridge {row.weighbridge_reroute.failed_wb_id} failed (X4). Truck rerouted to{" "}
            {row.weighbridge_reroute.alternate_wb_id ?? "an alternate weighbridge"}; customs{" "}
            {row.weighbridge_reroute.customs_notified ? "notified" : "not yet notified"}.
          </span>
        </p>
      )}

      <div className="flex flex-wrap gap-1.5">
        {STREAMS.map(({ key, label }) => {
          const ev = row.evidence?.[key];
          if (!ev) return null;
          return (
            <span
              key={key}
              className="inline-flex shrink-0 items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground"
            >
              {label}:{" "}
              <span
                className={
                  ev.provenance === "REAL"
                    ? "font-semibold text-emerald-600 dark:text-emerald-400"
                    : "font-semibold text-amber-600 dark:text-amber-400"
                }
              >
                {ev.provenance}
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

export default function AutoLeoJoinPanel() {
  const [expanded, setExpanded] = useState<string | null>(null);

  const boardQ = useQuery({
    queryKey: ["auto-leo-board"],
    queryFn: () => api.autoLeoBoard(50),
    refetchInterval: REFRESH_MS,
  });

  const data = boardQ.data;
  const rows = data?.rows ?? [];

  const columns: Column<AutoLeoRow>[] = [
    {
      key: "container_no",
      header: "Container",
      render: (r) => <span className="font-mono text-[12px]">{r.container_no}</span>,
    },
    {
      key: "vehicle_plate",
      header: "Vehicle",
      render: (r) => <span className="font-mono text-[12px]">{r.vehicle_plate ?? "—"}</span>,
    },
    { key: "sources", header: "Four-way join", render: (r) => <StreamChips row={r} /> },
    {
      key: "leo_ready",
      header: "Auto-LEO",
      render: (r) =>
        r.leo_ready ? (
          <StatusChip label="READY" tone="ok" />
        ) : (
          <StatusChip label="BLOCKED" tone="critical" />
        ),
    },
    {
      key: "customs_flags",
      header: "Flags",
      render: (r) =>
        r.customs_flags.length ? (
          <div className="flex flex-wrap gap-1">
            {r.customs_flags.map((f) => (
              <StatusChip key={f} label={f} tone="critical" />
            ))}
          </div>
        ) : (
          <span className="text-muted-foreground">—</span>
        ),
    },
  ];

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <StatGrid>
        <StatCard
          icon={BadgeCheck}
          label="LEO ready"
          value={String(data?.summary.leo_ready ?? "—")}
          tone="ok"
        />
        <StatCard
          icon={Flag}
          label="Blocked"
          value={String(data?.summary.blocked ?? "—")}
          tone="critical"
        />
        <StatCard
          icon={FileText}
          label="On a real Form 13"
          value={String(data?.summary.anchored_to_real_document ?? "—")}
          sub="customer-supplied documents"
        />
        <StatCard
          icon={Scale}
          label="Weight tolerance"
          value={`${data?.weight_tolerance_pct ?? 2}%`}
          sub="over tolerance ⇒ WEIGHT_MISMATCH"
        />
      </StatGrid>

      {data && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2.5">
          <TriangleAlert
            className="mt-px h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400"
            aria-hidden
          />
          <p className="min-w-0 text-[11px] leading-snug text-muted-foreground">
            <span className="font-medium text-foreground">Assumption {data.assumption.ref}.</span>{" "}
            {data.assumption.text}
          </p>
        </div>
      )}

      <DataTable<AutoLeoRow>
        columns={columns}
        rows={rows}
        rowKey={(r) => r.container_no}
        status={boardQ}
        onRetry={() => void boardQ.refetch()}
        emptyLabel="No export containers with a Form 13 to reconcile."
        onRowClick={(r) => setExpanded(expanded === r.container_no ? null : r.container_no)}
        isRowActive={(r) => r.container_no === expanded}
      />

      {expanded && rows.find((r) => r.container_no === expanded) && (
        <RowDetail row={rows.find((r) => r.container_no === expanded)!} />
      )}

      {data && (
        <Card className="min-w-0 p-3">
          <h3 className="text-sm font-semibold">Flag legend</h3>
          <dl className="mt-2 flex flex-col gap-1.5">
            {data.flags.map((f) => (
              <div key={f.flag} className="min-w-0">
                <dt className="inline">
                  <StatusChip label={f.flag} tone="critical" />
                </dt>
                <dd className="ml-2 inline text-[11px] leading-snug text-muted-foreground">
                  {f.meaning}
                </dd>
              </div>
            ))}
          </dl>
        </Card>
      )}
    </div>
  );
}
