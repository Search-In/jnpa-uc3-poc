import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Map as MlMap } from "maplibre-gl";
import { api } from "@/lib/api";
import { useSocket } from "@/hooks/SocketContext";
import type { Alert, Gate, TrafficSnapshot } from "@/lib/types";
import { LiveMap } from "@/components/map/LiveMap";
import { Card, CardContent } from "@/components/ui/card";
import { ThroughputChart } from "@/components/ThroughputChart";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/misc";
import { severityColour } from "@/lib/palette";
import { fmtTimeIST, relativeAge } from "@/lib/utils";

export default function LiveOperations() {
  const { alerts: liveAlerts, subscribe } = useSocket();
  const pushRef = useRef<((id: string, lon: number, lat: number) => void) | null>(null);
  const mapRef = useRef<MlMap | null>(null);
  const [selected, setSelected] = useState<Alert | null>(null);

  const corridorQ = useQuery({ queryKey: ["corridor"], queryFn: api.corridor, staleTime: Infinity });
  const gatesQ = useQuery({ queryKey: ["gates"], queryFn: api.gates });
  const snapsQ = useQuery({ queryKey: ["snapshots"], queryFn: api.trafficSnapshots });
  const zonesQ = useQuery({ queryKey: ["zones"], queryFn: api.zones });
  const queuedQ = useQuery({
    queryKey: ["trucks", "AT_GATE_QUEUE"],
    queryFn: () => api.trucks("AT_GATE_QUEUE", 500),
  });
  // Seed the alert list from REST so the panel isn't empty before the first WS push.
  const alertsSeed = useQuery({ queryKey: ["alerts-seed"], queryFn: () => api.alerts({ limit: 20 }) });

  // Feed WS truck positions into the map.
  useEffect(() => {
    const unsubscribe = subscribe((frame) => {
      if (frame.type === "truck_position" && pushRef.current) {
        const p = frame.payload;
        if (typeof p.lon === "number" && typeof p.lat === "number") {
          pushRef.current(p.device_id, p.lon, p.lat);
        }
      }
    });
    return () => {
      unsubscribe();
    };
  }, [subscribe]);

  const gates: Gate[] = gatesQ.data?.gates ?? [];
  const snapshots: TrafficSnapshot[] = snapsQ.data?.snapshots ?? [];
  const seeded = alertsSeed.data?.alerts ?? [];
  // Merge WS-live alerts with the REST seed, de-duped by id, newest first.
  const merged = dedupe([...liveAlerts, ...seeded]).slice(0, 10);

  const queueByGate = new Map<string, number>();
  for (const t of queuedQ.data?.devices ?? []) {
    if (t.gate_id) queueByGate.set(t.gate_id, (queueByGate.get(t.gate_id) ?? 0) + 1);
  }

  function focusAlert(a: Alert) {
    setSelected(a);
    const lat = a.payload?.lat as number | undefined;
    const lon = a.payload?.lon as number | undefined;
    if (mapRef.current && typeof lat === "number" && typeof lon === "number") {
      mapRef.current.flyTo({ center: [lon, lat], zoom: 14, duration: 800 });
    }
  }

  return (
    <div className="flex h-full flex-col">
      {/* KPI row */}
      <div className="grid grid-cols-2 gap-3 border-b border-border p-3 md:grid-cols-5">
        {gates.map((g) => (
          <Card key={g.id}>
            <CardContent className="flex flex-col gap-1 py-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">{g.id.replace("G-", "")}</span>
                <Badge colour={severityColour(g.utilisation && g.utilisation >= 1 ? "critical" : "ok")}>
                  {Math.round((g.utilisation ?? 0) * 100)}%
                </Badge>
              </div>
              <div className="text-xl font-semibold tabular-nums">
                {g.throughput_60min}
                <span className="ml-1 text-xs font-normal text-muted-foreground">/{g.target_vph} vph</span>
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
              Throughput · last 24 h
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
      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1">
          <LiveMap
            corridor={corridorQ.data}
            gates={gates}
            snapshots={snapshots}
            zones={zonesQ.data?.zones}
            onReady={(push, map) => {
              pushRef.current = push;
              mapRef.current = map;
            }}
          />
          <MapLegend />
        </div>

        <aside className="flex w-80 shrink-0 flex-col border-l border-border bg-card/40">
          <div className="border-b border-border px-4 py-3">
            <h2 className="text-sm font-semibold">Active alerts</h2>
            <p className="text-[11px] text-muted-foreground">Top 10 · click to locate & view evidence</p>
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

      <AlertEvidenceDialog alert={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function MapLegend() {
  const items = [
    { c: "#009E73", l: "free flow / on-target" },
    { c: "#E69F00", l: "moderate" },
    { c: "#D55E00", l: "congested / over-cap" },
    { c: "#56B4E9", l: "trucks (1:50)" },
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
              {evidence ? (
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
