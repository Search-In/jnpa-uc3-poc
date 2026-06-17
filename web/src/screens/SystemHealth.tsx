import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { CameraHealth, Decision, SourceHealth } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusDot, Spinner } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { IdentityPanel } from "@/components/panels/IdentityPanel";
import { AssumptionsPanel } from "@/components/AssumptionsPanel";
import { DecisionPathBadge, decisionPathColour } from "@/components/DecisionPathBadge";
import { sourceStateColour } from "@/lib/palette";
import { relativeAge, fmtDateTimeIST } from "@/lib/utils";

// Live status of every source: ANPR (per camera), Vahan, Sarathi, FastTag,
// Google/HERE/TomTom, RFID, Trucking App. Each shows its current decision-path
// state, last_ok, latency p95, as a coloured chip; click opens the log drawer
// (recent decisions filtered to that source's api).

// Logical source groups the bid spec calls out; we map gateway source rows onto
// them and always render the full set so missing sources read as "no data yet".
const EXPECTED: { label: string; match: (s: string) => boolean; api?: string }[] = [
  { label: "Vahan (RC)", match: (s) => s.startsWith("vahan"), api: "vahan" },
  { label: "Sarathi (DL)", match: (s) => s.includes("sarathi"), api: "vahan" },
  { label: "FASTag (NETC)", match: (s) => s.includes("fastag"), api: "vahan" },
  { label: "Traffic (Google/HERE/TomTom)", match: (s) => s.includes("congestion") || s.includes("traffic"), api: "traffic" },
  { label: "RFID readers", match: (s) => s.includes("rfid"), api: "anpr" },
  { label: "Trucking App", match: (s) => s.includes("truck"), api: "trucks" },
  { label: "ULIP relay", match: (s) => s.includes("ulip"), api: "trucks" },
  { label: "Anomaly engine", match: (s) => s.includes("anomaly") || s.includes("alert"), api: "alerts" },
];

export default function SystemHealth() {
  const sourcesQ = useQuery({ queryKey: ["sources"], queryFn: () => getAdapter().sources(), refetchInterval: 5000 });
  const camerasQ = useQuery({ queryKey: ["cameras"], queryFn: () => getAdapter().cameras(), refetchInterval: 5000 });
  const [drawer, setDrawer] = useState<{ title: string; api?: string; source?: string } | null>(null);

  const sources = sourcesQ.data ?? [];
  const cameras = camerasQ.data ?? [];

  const byLabel = useMemo(() => {
    return EXPECTED.map((e) => {
      const rows = sources.filter((s) => e.match(s.source.toLowerCase()));
      // pick the worst state to surface (DOWN > DEGRADED > LIVE).
      const worst = rows.sort((a, b) => rank(b.state) - rank(a.state))[0];
      return { ...e, row: worst, count: rows.length };
    });
  }, [sources]);

  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">System Health</h1>
        <AssumptionsPanel />
      </div>

      {sourcesQ.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> loading source health…
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
            {byLabel.map((e) => (
              <SourceChip
                key={e.label}
                label={e.label}
                row={e.row}
                onClick={() => setDrawer({ title: e.label, api: e.api, source: e.row?.source })}
              />
            ))}
          </div>

          <Card className="mt-5">
            <CardHeader>
              <CardTitle>ANPR cameras (per-camera feed level)</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
                {cameras.map((c) => (
                  <CameraChip key={c.camera_id} cam={c} onClick={() => setDrawer({ title: c.camera_id, api: "anpr" })} />
                ))}
              </div>
            </CardContent>
          </Card>

          <div className="mt-5 grid grid-cols-1 gap-3 lg:grid-cols-2">
            <IdentityPanel />
          </div>
        </>
      )}

      <LogDrawer drawer={drawer} onClose={() => setDrawer(null)} />
    </div>
  );
}

function rank(state?: string) {
  return state === "DOWN" ? 3 : state === "DEGRADED" ? 2 : state === "LIVE" ? 1 : 0;
}

function SourceChip({ label, row, onClick }: { label: string; row?: SourceHealth; onClick: () => void }) {
  const state = row?.state ?? "no data";
  const colour = sourceStateColour(row?.state);
  return (
    <button onClick={onClick} className="text-left">
      <Card className="transition-colors hover:border-primary/60">
        <CardContent className="space-y-2 py-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">{label}</span>
            <StatusDot colour={colour} pulse={row?.state === "LIVE"} />
          </div>
          <Badge colour={colour}>{state}</Badge>
          <dl className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
            <dt>last ok</dt>
            <dd className="text-right text-foreground">{relativeAge(row?.last_ok)}</dd>
            <dt>p95</dt>
            <dd className="text-right text-foreground tabular-nums">
              {row?.latency_p95_ms != null ? `${Math.round(row.latency_p95_ms)} ms` : "—"}
            </dd>
            <dt>path</dt>
            <dd className="text-right text-foreground">{row?.last_decision_path ?? "—"}</dd>
          </dl>
        </CardContent>
      </Card>
    </button>
  );
}

function CameraChip({ cam, onClick }: { cam: CameraHealth; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex items-center justify-between rounded-md border border-border bg-background px-2.5 py-2 text-left hover:border-primary/60"
    >
      <span className="truncate text-xs">{cam.camera_id.replace("CAM-", "")}</span>
      {/* decision_path shows the LIVE/CACHED/SYNTHETIC fallback on screen (Task 6). */}
      <DecisionPathBadge path={cam.decision_path} />
    </button>
  );
}

function LogDrawer({
  drawer,
  onClose,
}: {
  drawer: { title: string; api?: string; source?: string } | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["decisions", drawer?.api],
    queryFn: () => getAdapter().decisions(drawer?.api, 200),
    enabled: !!drawer,
    refetchInterval: drawer ? 4000 : false,
  });
  const rows: Decision[] = (q.data ?? []).filter(
    (d) => !drawer?.source || !d.key || d.key === drawer.source || d.api === drawer.api
  );

  return (
    <Dialog open={!!drawer} onOpenChange={(o) => !o && onClose()}>
      <DialogContent side="right">
        {drawer && (
          <>
            <DialogHeader>
              <DialogTitle>Decision log · {drawer.title}</DialogTitle>
            </DialogHeader>
            <div className="p-4">
              {q.isLoading ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Spinner /> loading decisions…
                </div>
              ) : rows.length === 0 ? (
                <p className="text-sm text-muted-foreground">No recent decisions for this source.</p>
              ) : (
                <ul className="space-y-1.5">
                  {rows.slice(0, 100).map((d, i) => (
                    <li key={i} className="rounded-md border border-border/60 bg-background px-3 py-2 text-xs">
                      <div className="flex items-center justify-between">
                        <Badge colour={decisionPathColour(d.decision_path)}>
                          {d.decision_path}
                        </Badge>
                        <span className="text-muted-foreground">{fmtDateTimeIST(d.ts)}</span>
                      </div>
                      <div className="mt-1 flex justify-between text-muted-foreground">
                        <span className="font-mono">{d.api}{d.key ? ` · ${d.key}` : ""}</span>
                        <span className="tabular-nums">
                          {d.latency_ms != null ? `${Math.round(d.latency_ms)} ms` : ""}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
