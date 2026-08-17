// D-13 — Fleet View.  GAP-SCR-08.
//
// A transporter's own vehicles. It lives in the control-room app rather than the
// driver PWA because the audience is a fleet owner or dispatcher at a desk, and
// because a DRIVER token is bound to one vehicle and has no business
// enumerating a fleet — the gateway refuses it for that reason.
//
// The column that matters is PROVENANCE, and it describes the LINK, not the
// truck. `11-Transport Data` carries no vehicle-registration column anywhere
// (defect B1), so no plate can be resolved to a company through JNPA's own
// masters. Of the links we have, some were read off a Form 13 or PIN ticket and
// some were generated under assumption A-G6 so the flow could be demonstrated.
// A list that rendered them identically would present assumptions as records,
// so the split is shown above the table rather than left to be inferred by
// scrolling, and each row names the document or the assumption behind it.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Truck, FileCheck2, TriangleAlert, Building2 } from "lucide-react";

import {
  PageContainer, PageHeader, StatGrid, StatCard, StatusChip,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState, EmptyState } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { fmtDateTimeIST } from "@/lib/utils";
// `FleetVehicle` is already taken by the vehicle-registry model, which is a
// different thing entirely (a vehicle record, not a plate→company link).
import type { TransporterFleetVehicle } from "@/lib/types";

export default function FleetView() {
  // Control-room roles may inspect any company; a transporter account is scoped
  // server-side and this box is simply ignored for them.
  const [inspect, setInspect] = useState("");
  const [submitted, setSubmitted] = useState("");

  const q = useQuery({
    queryKey: ["fleet", submitted],
    queryFn: () => api.fleet(submitted ? Number(submitted) : undefined),
  });

  const byProv = q.data?.by_provenance ?? {};
  const evidenced = byProv.DOCUMENT_EVIDENCED ?? 0;
  const assumed = Object.entries(byProv)
    .filter(([k]) => k !== "DOCUMENT_EVIDENCED")
    .reduce((n, [, v]) => n + v, 0);

  return (
    <PageContainer>
      <PageHeader
        icon={Truck}
        title="Fleet"
        subtitle="Vehicles linked to a transporter, and how each link was established"
        updatedAt={q.dataUpdatedAt}
        isFetching={q.isFetching}
        onRefresh={() => q.refetch()}
      />

      <div className="flex flex-col gap-4 p-4">
        <Card className="p-4">
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              setSubmitted(inspect.trim());
            }}
          >
            <label className="text-xs">
              <div className="mb-1 text-muted-foreground">
                Transporter id (control room only)
              </div>
              <input
                value={inspect}
                onChange={(e) => setInspect(e.target.value)}
                placeholder="e.g. 840"
                inputMode="numeric"
                className="w-40 rounded border border-border bg-transparent px-2 py-1.5 text-sm"
              />
            </label>
            <button
              type="submit"
              className="rounded bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
            >
              Show fleet
            </button>
          </form>
        </Card>

        {q.isLoading && <LoadingState />}
        {q.isError && (
          <ErrorState onRetry={() => q.refetch()} detail="The fleet service did not answer." />
        )}

        {/* A refusal is an answer. Rendering it as text, rather than as an empty
            table, is what stops "you may not see this" reading as "there is
            nothing here". */}
        {q.data?.reason && (
          <Card className="border-l-4 p-3 text-sm" style={{ borderLeftColor: "var(--warn, #b45309)" }}>
            {q.data.reason}
          </Card>
        )}

        {q.data && q.data.count > 0 && (
          <>
            <StatGrid>
              <StatCard icon={Building2} label="Company" value={q.data.company ?? "—"} />
              <StatCard icon={Truck} label="Vehicles" value={q.data.count} tone="ok" />
              <StatCard icon={FileCheck2} label="Link on a document" value={evidenced}
                        tone={evidenced ? "ok" : "neutral"}
                        sub="a gate document names plate and company" />
              <StatCard icon={TriangleAlert} label="Link assumed" value={assumed}
                        tone={assumed ? "warn" : "neutral"}
                        sub="no registration column exists to resolve one" />
            </StatGrid>

            <Card>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted-foreground">
                      <th className="px-3 py-2">Vehicle</th>
                      <th className="px-3 py-2">Link established by</th>
                      <th className="px-3 py-2 text-right">Jobs</th>
                      <th className="px-3 py-2">Last gate document</th>
                      <th className="px-3 py-2 text-right">Company blacklist</th>
                    </tr>
                  </thead>
                  <tbody>
                    {q.data.vehicles.map((v: TransporterFleetVehicle) => {
                      const evid = v.provenance === "DOCUMENT_EVIDENCED";
                      return (
                        <tr key={v.vehicle_no} className="border-b border-border last:border-0">
                          <td className="px-3 py-2 font-mono">{v.vehicle_no}</td>
                          <td className="px-3 py-2">
                            <div className="flex flex-wrap items-center gap-2">
                              <StatusChip
                                label={evid ? "document" : "assumed"}
                                tone={evid ? "ok" : "warn"}
                              />
                              <span className="text-xs text-muted-foreground">
                                {evid ? v.source_ref : v.assumption_ref}
                              </span>
                            </div>
                          </td>
                          <td className="px-3 py-2 text-right">{v.jobs}</td>
                          <td className="px-3 py-2 text-xs">
                            {v.last_gate_document_ts ? (
                              fmtDateTimeIST(v.last_gate_document_ts)
                            ) : (
                              <span className="text-muted-foreground">none</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-right">
                            {v.company_blacklist_entries ? (
                              <StatusChip label={String(v.company_blacklist_entries)} tone="warn" />
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>

            <p className="text-xs text-muted-foreground">{q.data.note}</p>
          </>
        )}

        {q.data && q.data.count === 0 && !q.data.reason && (
          <EmptyState>No vehicle is linked to this transporter.</EmptyState>
        )}
      </div>
    </PageContainer>
  );
}
