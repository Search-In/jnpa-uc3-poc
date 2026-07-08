import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { TruckDevice } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { PageContainer, PageHeader, StatGrid, StatCard } from "@/components/ui/dtccc";
import { fmtEta } from "@/lib/utils";
import { Navigation, CheckCircle2, AlertCircle, Route, DoorOpen } from "lucide-react";

const GATES = ["G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT"];

// Trucks AT_GATE_QUEUE with ETA-to-gate and a re-routing recommendation. The
// recommendation picks the least-loaded alternative gate; "Push Re-route" forces
// it via POST /api/trucks/{id}/route (used in the TFC-3 scenario).
export default function DriverAdvisory() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const queued = useQuery({
    queryKey: ["trucks", "AT_GATE_QUEUE", "advisory"],
    queryFn: () => getAdapter().trucks("AT_GATE_QUEUE", 500),
    refetchInterval: 6000,
  });

  const devices = queued.data ?? [];

  // Queue depth per gate -> the recommendation steers toward the shortest queue.
  const depth = new Map<string, number>();
  for (const t of devices) if (t.gate_id) depth.set(t.gate_id, (depth.get(t.gate_id) ?? 0) + 1);
  const recommendFor = (current?: string | null) => {
    const ranked = GATES.filter((g) => g !== current).sort(
      (a, b) => (depth.get(a) ?? 0) - (depth.get(b) ?? 0),
    );
    return ranked[0];
  };
  const busiest = GATES.reduce(
    (a, b) => ((depth.get(b) ?? 0) > (depth.get(a) ?? 0) ? b : a),
    GATES[0],
  );

  return (
    <PageContainer>
      <PageHeader
        icon={Route}
        title={t("nav.advisory")}
        subtitle={`${t("advisory.subtitlePrefix")} AT_GATE_QUEUE · ${t("advisory.subtitleSuffix")}`}
        updatedAt={queued.dataUpdatedAt}
        isFetching={queued.isFetching && !queued.isLoading}
        onRefresh={() =>
          qc.invalidateQueries({ queryKey: ["trucks", "AT_GATE_QUEUE", "advisory"] })
        }
      />

      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-5">
          <StatCard
            icon={DoorOpen}
            label={t("advisory.queuedTrucks")}
            value={devices.length}
            tone={devices.length > 40 ? "warn" : "info"}
            loading={queued.isLoading}
          />
          {GATES.map((g) => (
            <StatCard
              key={g}
              label={g.replace("G-", "")}
              value={depth.get(g) ?? 0}
              tone={g === busiest && (depth.get(g) ?? 0) > 0 ? "warn" : "ok"}
              sub={g === busiest && (depth.get(g) ?? 0) > 0 ? "busiest" : "queue depth"}
              loading={queued.isLoading}
            />
          ))}
        </StatGrid>
      </div>

      <div className="px-4 py-3">
        {queued.isLoading ? (
          <Card className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
            <Spinner /> {t("advisory.loadingQueue")}
          </Card>
        ) : devices.length === 0 ? (
          <Card>
            <EmptyState>{t("advisory.emptyQueue")}</EmptyState>
          </Card>
        ) : (
          <Card data-guided-id="advisory-queue" className="overflow-hidden">
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-border bg-muted/60 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2">{t("advisory.colDevice")}</th>
                      <th className="px-4 py-2">{t("advisory.colPlate")}</th>
                      <th className="px-4 py-2">{t("advisory.colGate")}</th>
                      <th className="px-4 py-2">{t("advisory.colEta")}</th>
                      <th className="px-4 py-2">{t("advisory.colRemaining")}</th>
                      <th className="px-4 py-2">{t("advisory.colRecommend")}</th>
                      <th className="px-4 py-2 text-right">{t("advisory.colAction")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {devices.slice(0, 200).map((t) => (
                      <QueueRow
                        key={t.device_id}
                        truck={t}
                        recommend={recommendFor(t.gate_id)}
                        qc={qc}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </PageContainer>
  );
}

function QueueRow({
  truck,
  recommend,
  qc,
}: {
  truck: TruckDevice;
  recommend: string;
  qc: ReturnType<typeof useQueryClient>;
}) {
  const { t } = useTranslation();
  // The dropdown shows the suggested gate by default, but stays editable. Once
  // the operator picks a gate we keep their choice (`selected`) regardless of
  // how the auto-recommendation shifts as the queue rebalances.
  const [selected, setSelected] = useState<string | null>(null);
  const gate = selected ?? recommend;
  const [done, setDone] = useState(false);

  const reroute = useMutation({
    mutationFn: (gateId: string) =>
      getAdapter().reroute(truck.device_id, {
        gate_id: gateId,
        force_state: "EN_ROUTE_TO_PORT",
      }),
    onSuccess: async () => {
      setDone(true);
      // Refetch so the Gate column reflects the persisted change immediately.
      await qc.invalidateQueries({ queryKey: ["trucks"] });
    },
  });

  const onGateChange = (gateId: string) => {
    setSelected(gateId);
    setDone(false);
    reroute.mutate(gateId);
  };

  return (
    <tr className="border-b border-border/50 hover:bg-muted/40">
      <td className="px-4 py-2 font-mono text-xs">{truck.device_id}</td>
      <td className="px-4 py-2 font-mono text-xs">{truck.plate ?? "—"}</td>
      <td className="px-4 py-2">{truck.gate_id?.replace("G-", "") ?? "—"}</td>
      <td className="px-4 py-2 tabular-nums">{fmtEta(truck.eta_s)}</td>
      <td className="px-4 py-2 tabular-nums">{truck.remaining_km.toFixed(1)} km</td>
      <td className="px-4 py-2">
        <Select value={gate} onValueChange={onGateChange} disabled={reroute.isPending}>
          <SelectTrigger
            className="w-[140px]"
            data-guided-id="advisory-reroute"
            aria-label={t("advisory.selectGateAria", { device: truck.device_id })}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {GATES.map((g) => (
              <SelectItem key={g} value={g}>
                → {g.replace("G-", "")}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </td>
      <td className="px-4 py-2 text-right">
        {reroute.isPending ? (
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            <Spinner /> {t("advisory.saving")}
          </span>
        ) : reroute.isError ? (
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs text-severity-crit"
            onClick={() => reroute.mutate(gate)}
          >
            <AlertCircle className="h-3.5 w-3.5" /> {t("common.retry")}
          </button>
        ) : done ? (
          <span className="inline-flex items-center gap-1 text-xs text-severity-ok">
            <CheckCircle2 className="h-3.5 w-3.5" /> {t("advisory.gateUpdated")}
          </span>
        ) : (
          <Button
            size="sm"
            variant="outline"
            onClick={() => reroute.mutate(gate)}
            disabled={reroute.isPending}
          >
            <Navigation className="h-3.5 w-3.5" />
            {t("advisory.pushReroute")}
          </Button>
        )}
      </td>
    </tr>
  );
}
