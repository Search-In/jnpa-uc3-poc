import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type MapView from "@arcgis/core/views/MapView";
import { getAdapter } from "@/data";
import { useSocket } from "@/hooks/SocketContext";
import type { Alert, Gate, TrafficSnapshot } from "@/lib/types";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import { Card, CardContent } from "@/components/ui/card";
import { ThroughputChart } from "@/components/ThroughputChart";
import { KpiStrip } from "@/components/panels/KpiStrip";
import { CarbonTile } from "@/components/panels/CarbonTile";
import { EmptyContainerBoard } from "@/components/panels/EmptyContainerBoard";
import { ParkingBoard } from "@/components/panels/ParkingBoard";
import { AutoLeoPanel } from "@/components/panels/AutoLeoPanel";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/misc";
import { severityColour } from "@/lib/palette";
import { MAP_TOKENS, STATUS } from "@/lib/tokens";
import { fmtTimeIST, relativeAge } from "@/lib/utils";

export default function LiveOperations() {
  const { alerts: liveAlerts } = useSocket();
  const [view, setView] = useState<MapView | null>(null);
  const [selected, setSelected] = useState<Alert | null>(null);

  // All data now flows through the typed adapter (never the gateway directly).
  // Adapter methods return UNWRAPPED data (Gate[], TrafficSnapshot[], …).
  const corridorQ = useQuery({
    queryKey: ["corridor"],
    queryFn: () => getAdapter().corridor(),
    staleTime: Infinity,
  });
  const gatesQ = useQuery({ queryKey: ["gates"], queryFn: () => getAdapter().gates() });
  const snapsQ = useQuery({
    queryKey: ["snapshots"],
    queryFn: () => getAdapter().trafficSnapshots(),
  });
  const zonesQ = useQuery({ queryKey: ["zones"], queryFn: () => getAdapter().zones() });
  const trucksQ = useQuery({
    queryKey: ["trucks", "live-map"],
    queryFn: () => getAdapter().trucks(undefined, 500),
  });
  const queuedQ = useQuery({
    queryKey: ["trucks", "AT_GATE_QUEUE"],
    queryFn: () => getAdapter().trucks("AT_GATE_QUEUE", 500),
  });
  const parkingQ = useQuery({
    queryKey: ["parking-availability"],
    queryFn: () => getAdapter().parkingAvailability(),
  });
  // Seed the alert list from the adapter so the panel isn't empty before the first WS push.
  const alertsSeed = useQuery({
    queryKey: ["alerts-seed"],
    queryFn: () => getAdapter().alerts({ limit: 20 }),
  });
  // Prediction carries a decision_path → surfaced as a LIVE/SYNTHETIC badge.
  const predictQ = useQuery({
    queryKey: ["traffic-predict"],
    queryFn: () => getAdapter().trafficPredict(),
  });

  const gates: Gate[] = gatesQ.data ?? [];
  const snapshots: TrafficSnapshot[] = snapsQ.data ?? [];
  const seeded = alertsSeed.data ?? [];
  // Merge WS-live alerts with the adapter seed, de-duped by id, newest first.
  const merged = dedupe([...liveAlerts, ...seeded]).slice(0, 10);

  const queueByGate = new Map<string, number>();
  for (const t of queuedQ.data ?? []) {
    if (t.gate_id) queueByGate.set(t.gate_id, (queueByGate.get(t.gate_id) ?? 0) + 1);
  }

  function focusAlert(a: Alert) {
    setSelected(a);
    const lat = a.payload?.lat as number | undefined;
    const lon = a.payload?.lon as number | undefined;
    if (view && typeof lat === "number" && typeof lon === "number") {
      void view.goTo({ center: [lon, lat], zoom: 14 });
    }
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      {/* KPI strip from the adapter (label/value/target/Δ%/sparkline). */}
      <div className="border-b border-border p-3">
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Corridor KPIs
          </h2>
          <DecisionPathBadge path={predictQ.data?.decision_path} />
        </div>
        <KpiStrip />
      </div>

      {/* Gate throughput + queue tiles. */}
      <div className="grid grid-cols-2 gap-3 border-b border-border p-3 md:grid-cols-5">
        {gates.map((g) => (
          <Card key={g.id}>
            <CardContent className="flex flex-col gap-1 py-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">
                  {g.id.replace("G-", "")}
                </span>
                <Badge
                  colour={severityColour(g.utilisation && g.utilisation >= 1 ? "critical" : "ok")}
                >
                  {Math.round((g.utilisation ?? 0) * 100)}%
                </Badge>
              </div>
              <div className="text-xl font-semibold tabular-nums">
                {g.throughput_60min}
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  /{g.target_vph} vph
                </span>
              </div>
              <div className="text-[11px] text-muted-foreground">
                queue {queueByGate.get(g.id) ?? 0} · target {g.target_vph}/h
              </div>
            </CardContent>
          </Card>
        ))}
        <Card className="col-span-2 md:col-span-1">
          <CardContent className="flex h-full flex-col py-2">
            <span className="mb-1 text-[11px] font-medium text-muted-foreground">
              Throughput · trend
            </span>
            <div className="min-h-[64px] flex-1">
              <ThroughputChart />
            </div>
          </CardContent>
        </Card>
        {gatesQ.isLoading && (
          <div className="col-span-full flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> loading gate KPIs…
          </div>
        )}
      </div>

      {/* Map + alerts side panel */}
      <div className="flex min-h-[420px] flex-1">
        <div className="relative min-w-0 flex-1">
          <ArcgisMap
            corridor={corridorQ.data}
            gates={gates}
            zones={zonesQ.data}
            snapshots={snapshots}
            trucks={trucksQ.data}
            parkingFacilities={parkingQ.data}
            onViewReady={setView}
          />
          <MapLegend />
        </div>

        <aside className="flex w-80 shrink-0 flex-col border-l border-border bg-card/40">
          <div className="border-b border-border px-4 py-3">
            <h2 className="text-sm font-semibold">Active alerts</h2>
            <p className="text-[11px] text-muted-foreground">
              Top 10 · click to locate & view evidence
            </p>
          </div>
          <ul className="min-h-0 flex-1 overflow-y-auto">
            {merged.length === 0 && (
              <li className="p-4 text-sm text-muted-foreground">No active alerts.</li>
            )}
            {merged.map((a) => (
              <li key={a.id}>
                <button
                  onClick={() => focusAlert(a)}
                  className="flex w-full flex-col gap-1 border-b border-border/60 px-4 py-2.5 text-left hover:bg-muted"
                >
                  <div className="flex items-center justify-between gap-2">
                    <Badge colour={severityColour(a.severity)}>{a.kind}</Badge>
                    <span className="text-[10px] text-muted-foreground">{relativeAge(a.ts)}</span>
                  </div>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span className="font-mono">{a.plate ?? "—"}</span>
                    <span>{a.gate_id ?? a.payload?.zone_id ?? ""}</span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </aside>
      </div>

      {/* Appendix-C capability tiles (DTCCC view). */}
      <div className="grid grid-cols-1 gap-3 border-t border-border p-3 lg:grid-cols-3">
        <CarbonTile />
        <ParkingBoard />
        <EmptyContainerBoard />
      </div>
      <div className="grid grid-cols-1 gap-3 border-t border-border p-3 lg:grid-cols-2">
        <AutoLeoPanel />
      </div>

      <AlertEvidenceDialog alert={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function MapLegend() {
  const items = [
    { c: STATUS.ok, l: "free flow / on-target" },
    { c: STATUS.warning, l: "moderate" },
    { c: STATUS.critical, l: "congested / over-cap" },
    { c: MAP_TOKENS.truckFill, l: "trucks (1:50)" },
  ];
  return (
    <div className="absolute bottom-3 left-3 rounded-md border border-border bg-card/85 p-2 text-[11px] backdrop-blur">
      {items.map((i) => (
        <div key={i.l} className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: i.c }} />
          {i.l}
        </div>
      ))}
    </div>
  );
}

function AlertEvidenceDialog({ alert, onClose }: { alert: Alert | null; onClose: () => void }) {
  const evidence = alert?.payload?.evidence_url as string | undefined;
  const mp4 = alert?.payload?.evidence_mp4_url as string | undefined;
  const echallanId = alert?.payload?.echallan_id as string | undefined;
  const echallanPdf = alert?.payload?.echallan_pdf_url as string | undefined;
  return (
    <Dialog open={!!alert} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        {alert && (
          <>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Badge colour={severityColour(alert.severity)}>{alert.kind}</Badge>
                <span className="font-mono text-sm">{alert.plate ?? "—"}</span>
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-3 p-4">
              <div className="grid grid-cols-2 gap-2 text-xs">
                <Field k="Time (IST)" v={fmtTimeIST(alert.ts)} />
                <Field k="Severity" v={alert.severity} />
                <Field k="Gate" v={alert.gate_id ?? "—"} />
                <Field k="Zone" v={(alert.payload?.zone_id as string) ?? "—"} />
              </div>

              {/* TFC-2: play the last-10s evidence clip when present. */}
              {mp4 ? (
                <video
                  src={mp4}
                  controls
                  autoPlay
                  muted
                  loop
                  className="w-full rounded-md border border-border bg-black"
                  data-testid="evidence-video"
                >
                  Your browser does not support the video tag.
                </video>
              ) : evidence ? (
                <img
                  src={evidence}
                  alt="incident evidence from MinIO"
                  className="w-full rounded-md border border-border"
                  onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                />
              ) : (
                <div className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                  No photographic evidence attached.
                </div>
              )}

              {echallanId && (
                <div className="flex items-center justify-between rounded-md border border-severity-warning/50 bg-severity-warning/10 px-3 py-2 text-xs">
                  <span>
                    e-Challan <span className="font-mono font-semibold">{echallanId}</span>
                  </span>
                  {echallanPdf && (
                    <a
                      href={echallanPdf}
                      target="_blank"
                      rel="noreferrer"
                      className="text-severity-info hover:underline"
                    >
                      open PDF
                    </a>
                  )}
                </div>
              )}

              {alert.payload && (
                <pre className="max-h-40 overflow-auto rounded-md bg-muted p-2 text-[11px]">
                  {JSON.stringify(alert.payload, null, 2)}
                </pre>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{k}</div>
      <div>{v}</div>
    </div>
  );
}

function dedupe(alerts: Alert[]): Alert[] {
  const seen = new Set<string>();
  const out: Alert[] = [];
  for (const a of alerts) {
    const key = a.id || `${a.kind}-${a.ts}-${a.plate}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(a);
  }
  return out;
}
