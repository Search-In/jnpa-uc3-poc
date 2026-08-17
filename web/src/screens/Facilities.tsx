// T-09 — Facilities & Utilities Directory.  GAP-SCR-03.
//
// There is no facilities master table, so this is composed from the five places
// the corpus actually names a place, and every row says which one. That matters
// here more than on most screens: a CFS named in a monthly dwell report and a
// rail siding read off a daily PDF are evidence of very different strength, and
// a directory that flattened them into one list would hide that.
//
// The screen also renders what is NOT here. Weighbridges are referenced by two
// ids in a reroute row and defined nowhere; driver amenities are absent
// entirely. Both are shown as stated absences, because the driver-side locator
// (D-10) and facilities list (D-11) are built on this endpoint and must say
// "not supplied" rather than draw an empty map that reads as a failed load.

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Building2, MapPin, Ship, Train, CircleParking, TriangleAlert } from "lucide-react";

import {
  PageContainer, PageHeader, StatGrid, StatCard, SearchInput, StatusChip, FilterSelect,
} from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { LoadingState, ErrorState, EmptyState } from "@/components/ui/misc";
import { api } from "@/lib/api";
import type { FacilityRow } from "@/lib/types";

const TYPE_ICON: Record<string, typeof Ship> = {
  TERMINAL: Ship,
  RAIL_SIDING: Train,
  CPP: CircleParking,
  CFS: Building2,
  ICD: Building2,
};

const TYPE_LABEL: Record<string, string> = {
  TERMINAL: "Container terminal",
  CFS: "Container freight station",
  ICD: "Inland container depot",
  RAIL_SIDING: "Rail siding",
  CPP: "Parking",
};

export default function Facilities() {
  const [term, setTerm] = useState("");
  const [type, setType] = useState("");

  const q = useQuery({
    queryKey: ["facilities"],
    queryFn: () => api.facilities(),
  });

  const rows = useMemo(() => {
    const all = q.data?.facilities ?? [];
    const needle = term.trim().toLowerCase();
    return all.filter(
      (f) =>
        (!type || f.type === type) &&
        (!needle ||
          f.name.toLowerCase().includes(needle) ||
          (f.operator ?? "").toLowerCase().includes(needle)),
    );
  }, [q.data, term, type]);

  const byType = q.data?.by_type ?? {};

  return (
    <PageContainer>
      <PageHeader
        icon={MapPin}
        title="Facilities & Utilities"
        subtitle="Every place the corpus names, and the file family that names it"
        updatedAt={q.dataUpdatedAt}
        isFetching={q.isFetching}
        onRefresh={() => q.refetch()}
      />

      <div className="flex flex-col gap-4 p-4">
        {q.isLoading && <LoadingState />}
        {q.isError && (
          <ErrorState onRetry={() => q.refetch()} detail="The facilities directory is unavailable." />
        )}

        {q.data && (
          <>
            <StatGrid>
              <StatCard icon={Ship} label="Terminals" value={byType.TERMINAL ?? 0} tone="ok" />
              <StatCard icon={Building2} label="CFS" value={byType.CFS ?? 0} tone="ok"
                        sub="named in the LDB dwell reports" />
              <StatCard icon={Building2} label="ICD" value={byType.ICD ?? 0} tone="ok" />
              <StatCard icon={Train} label="Rail sidings" value={byType.RAIL_SIDING ?? 0} tone="ok"
                        sub="from the ICD daily reports" />
              <StatCard icon={CircleParking} label="Parking" value={byType.CPP ?? 0} tone="ok" />
            </StatGrid>

            {/* Named absences. Rendered at the same weight as the data, for the
                same reason the Evidence Explorer draws its empty hops. */}
            {q.data.absent?.length > 0 && (
              <Card>
                <div className="flex items-center gap-2 border-b border-border px-4 py-2 text-sm font-semibold">
                  <TriangleAlert className="h-4 w-4" /> Not in the corpus
                </div>
                {q.data.absent.map((a) => (
                  <div key={a.type} className="border-b border-border px-4 py-3 last:border-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusChip label={a.type} tone="warn" />
                      <span className="text-sm">{a.why}</span>
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      Would need: {a.would_need}
                    </div>
                  </div>
                ))}
              </Card>
            )}

            <Card className="p-4">
              <div className="flex flex-wrap items-end gap-3">
                <SearchInput
                  value={term}
                  onChange={setTerm}
                  placeholder="Name or operator"
                  className="min-w-[16rem] flex-1"
                />
                <FilterSelect
                  value={type}
                  onChange={setType}
                  options={[
                    { value: "", label: `All types (${q.data.count})` },
                    ...Object.entries(byType).map(([k, n]) => ({
                      value: k,
                      label: `${TYPE_LABEL[k] ?? k} (${n})`,
                    })),
                  ]}
                />
              </div>
            </Card>

            <Card>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted-foreground">
                      <th className="px-3 py-2">Facility</th>
                      <th className="px-3 py-2">Type</th>
                      <th className="px-3 py-2">Operator</th>
                      <th className="px-3 py-2 text-right">Berths</th>
                      <th className="px-3 py-2 text-right">Capacity</th>
                      <th className="px-3 py-2 text-right">Dwell (h)</th>
                      <th className="px-3 py-2">Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((f: FacilityRow, i: number) => {
                      const Icon = TYPE_ICON[f.type] ?? Building2;
                      return (
                        <tr key={`${f.type}-${f.facility_id}-${i}`}
                            className="border-b border-border last:border-0">
                          <td className="px-3 py-2">
                            <div className="flex items-center gap-2">
                              <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                              <span>{f.name}</span>
                            </div>
                            {f.site_code && (
                              <div className="ml-5.5 text-xs text-muted-foreground">
                                <code>{f.site_code}</code>
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-2 text-xs">{TYPE_LABEL[f.type] ?? f.type}</td>
                          <td className="px-3 py-2 text-xs">
                            {f.operator ?? <span className="text-muted-foreground">—</span>}
                          </td>
                          <td className="px-3 py-2 text-right">{f.berth_count ?? "—"}</td>
                          <td className="px-3 py-2 text-right">{f.capacity ?? "—"}</td>
                          <td className="px-3 py-2 text-right">{f.dwell_hours ?? "—"}</td>
                          {/* The table AND the corpus family behind it — a CFS
                              from a monthly report is weaker evidence than a
                              terminal from the reference model, and the reader
                              should be able to see which they are looking at. */}
                          <td className="px-3 py-2 text-xs text-muted-foreground">
                            <code>{f.source_table}</code>
                            {f.source_files ? <div>{f.source_files}</div> : null}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {rows.length === 0 && <EmptyState>No facility matches those filters.</EmptyState>}
            </Card>

            <p className="text-xs text-muted-foreground">
              There is no facilities master table in the database. This directory is
              composed from {Object.keys(byType).length} sources at read time, so it
              follows the data rather than a list maintained by hand.
            </p>
          </>
        )}
      </div>
    </PageContainer>
  );
}
