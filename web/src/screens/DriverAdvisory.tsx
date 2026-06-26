import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { TruckDevice } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { fmtEta } from "@/lib/utils";
import { Navigation, CheckCircle2 } from "lucide-react";

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

  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">{t("nav.advisory")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("advisory.subtitlePrefix")} <span className="font-mono">AT_GATE_QUEUE</span> ·{" "}
            {t("advisory.subtitleSuffix")}
          </p>
        </div>
        <Badge colour="#56B4E9">{t("advisory.queuedCount", { count: devices.length })}</Badge>
      </div>

      {queued.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("advisory.loadingQueue")}
        </div>
      ) : devices.length === 0 ? (
        <Card>
          <EmptyState>{t("advisory.emptyQueue")}</EmptyState>
        </Card>
      ) : (
        <Card data-guided-id="advisory-queue">
          <CardHeader>
            <CardTitle>{t("advisory.queuedTrucks")}</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <table className="w-full text-sm">
              <thead className="border-b border-border text-left text-xs text-muted-foreground">
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
          </CardContent>
        </Card>
      )}
    </div>
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
  const [done, setDone] = useState(false);
  const reroute = useMutation({
    mutationFn: () =>
      getAdapter().reroute(truck.device_id, {
        gate_id: recommend,
        force_state: "EN_ROUTE_TO_PORT",
      }),
    onSuccess: () => {
      setDone(true);
      qc.invalidateQueries({ queryKey: ["trucks"] });
    },
  });

  return (
    <tr className="border-b border-border/50 hover:bg-muted/40">
      <td className="px-4 py-2 font-mono text-xs">{truck.device_id}</td>
      <td className="px-4 py-2 font-mono text-xs">{truck.plate ?? "—"}</td>
      <td className="px-4 py-2">{truck.gate_id?.replace("G-", "") ?? "—"}</td>
      <td className="px-4 py-2 tabular-nums">{fmtEta(truck.eta_s)}</td>
      <td className="px-4 py-2 tabular-nums">{truck.remaining_km.toFixed(1)} km</td>
      <td className="px-4 py-2">
        <Badge colour="#009E73">→ {recommend.replace("G-", "")}</Badge>
      </td>
      <td className="px-4 py-2 text-right">
        {done ? (
          <span className="inline-flex items-center gap-1 text-xs text-severity-ok">
            <CheckCircle2 className="h-3.5 w-3.5" /> {t("advisory.rerouted")}
          </span>
        ) : (
          <Button
            size="sm"
            variant="outline"
            data-guided-id="advisory-reroute"
            onClick={() => reroute.mutate()}
            disabled={reroute.isPending}
          >
            {reroute.isPending ? <Spinner /> : <Navigation className="h-3.5 w-3.5" />}
            {t("advisory.pushReroute")}
          </Button>
        )}
      </td>
    </tr>
  );
}
