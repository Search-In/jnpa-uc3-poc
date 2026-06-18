import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalciteSegmentedControl,
  CalciteSegmentedControlItem,
  CalciteButton,
  CalciteChip,
  CalciteNotice,
  CalciteBlock,
} from "@esri/calcite-components-react";
import { getAdapter, DATA_MODE } from "@/data";
import { useSocket } from "@/hooks/SocketContext";
import type {
  FaultControlResult,
  FaultSeverity,
  FaultState,
  OperatorBanner,
} from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { KpiStrip } from "@/components/panels/KpiStrip";
import { DecisionPathBadge } from "@/components/DecisionPathBadge";
import { STATUS } from "@/lib/tokens";
import { SlidersHorizontal, Joystick } from "lucide-react";

// Demo Console (presenter control surface). Live control over the three
// fault-injection fallback chains (camera / Vahan / trucks), an operator banner
// that also updates from the live WS `operator_banner` frame, and a realism
// status panel (OCR accuracy, congestion F1, KPI deltas). Controls without a
// backend yet (feeds, demo clock, fleet size) are rendered as clearly-labelled
// "(preview)" demo-local widgets — no fabricated backend calls.

// The three chains. Severity colour comes from the response, not from us; we
// only render. Rungs are read from getFaults() so the control mirrors the
// backend's declared ladder exactly.
const CHAINS: { domain: "camera" | "vahan" | "trucks"; label: string; blurb: string }[] = [
  { domain: "camera", label: "Camera", blurb: "LIVE → CACHED → SYNTHETIC frame fallback" },
  { domain: "vahan", label: "Vahan", blurb: "LIVE_PRIMARY → LIVE_FALLBACK → CACHED → PROVISIONAL RC lookup" },
  { domain: "trucks", label: "Trucks", blurb: "PRIMARY → SECONDARY → TERTIARY telemetry source" },
];

/** GREEN/AMBER/RED severity → CB-safe token (tokens.ts only). */
function severityColour(sev?: FaultSeverity | null): string {
  if (sev === "RED") return STATUS.critical;
  if (sev === "AMBER") return STATUS.warning;
  if (sev === "GREEN") return STATUS.ok;
  return STATUS.unknown;
}

export default function DemoConsole() {
  const qc = useQueryClient();
  const { operatorBanner: wsBanner } = useSocket();

  // Latest banner echoed by a force/clear mutation (so a single client reacts
  // instantly without waiting for the WS round-trip).
  const [localBanner, setLocalBanner] = useState<OperatorBanner | null>(null);

  // ---- Fault-injection state (PRIMARY — full backend support) ----------
  const faultsQ = useQuery<FaultState>({
    queryKey: ["faults"],
    queryFn: () => getAdapter().getFaults(),
    refetchInterval: 3000,
  });

  const force = useMutation({
    mutationFn: ({ domain, rung }: { domain: string; rung: string }) =>
      getAdapter().forceFault(domain, rung),
    onSuccess: (res: FaultControlResult) => {
      setLocalBanner(res.banner);
      void qc.invalidateQueries({ queryKey: ["faults"] });
    },
  });

  const clear = useMutation({
    mutationFn: (domain?: string) => getAdapter().clearFault(domain),
    onSuccess: (res: FaultControlResult) => {
      setLocalBanner(res.banner);
      void qc.invalidateQueries({ queryKey: ["faults"] });
    },
  });

  // The banner shown is the freshest of (live WS push) vs (last mutation echo).
  // WS wins when present so a fault forced by another presenter also lights up.
  const banner: OperatorBanner | null = wsBanner ?? localBanner ?? null;
  const bannerActive = banner?.active ?? false;

  // ---- Realism probes --------------------------------------------------
  const ocrQ = useQuery({ queryKey: ["ocr-eval"], queryFn: () => getAdapter().ocrEval() });
  const f1Q = useQuery({ queryKey: ["congestion-metrics"], queryFn: () => getAdapter().congestionMetrics() });

  const faults = faultsQ.data;
  const anyForced = useMemo(() => {
    if (!faults) return false;
    return Object.values(faults.domains).some((d) => d.forced_rung != null);
  }, [faults]);

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="flex items-center justify-between border-b border-border p-4">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="h-5 w-5 text-primary" />
          <div>
            <h1 className="text-lg font-semibold">Demo Console</h1>
            <p className="text-sm text-muted-foreground">
              Force fallback rungs, watch realism status, reset to clean baseline.
            </p>
          </div>
        </div>
        <CalciteButton
          appearance="outline"
          kind="neutral"
          iconStart="reset"
          scale="s"
          disabled={(!anyForced || clear.isPending) || undefined}
          onClick={() => clear.mutate(undefined)}
        >
          {clear.isPending && clear.variables === undefined ? "Resetting…" : "Reset all faults"}
        </CalciteButton>
      </div>

      {/* ---- Operator banner ---- */}
      <div className="px-4 pt-4">
        <CalciteNotice
          open={bannerActive || undefined}
          kind={banner?.severity === "RED" ? "danger" : "warning"}
          icon="exclamation-mark-triangle"
          scale="m"
        >
          <div slot="title">Operator banner — fault injection active</div>
          <div slot="message">
            {banner && banner.domains.length > 0
              ? `Forced chains: ${banner.domains.join(", ")} · severity ${banner.severity ?? "—"}`
              : "No faults forced."}
          </div>
        </CalciteNotice>
      </div>

      {/* ---- Fault-injection chains ---- */}
      <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-3">
        {CHAINS.map((chain) => {
          const state = faults?.domains[chain.domain];
          const rungs = faults?.rungs[chain.domain] ?? [];
          const forced = state?.forced_rung ?? null;
          const sev = state?.severity ?? null;
          return (
            <Card key={chain.domain} className={forced ? "border-primary" : ""}>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>{chain.label}</CardTitle>
                <CalciteChip
                  scale="s"
                  kind={sev === "RED" ? "brand" : "neutral"}
                  style={{ color: severityColour(sev) }}
                >
                  {sev ?? "GREEN"}
                </CalciteChip>
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="text-xs text-muted-foreground">{chain.blurb}</p>

                {faultsQ.isLoading ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Spinner /> loading rungs…
                  </div>
                ) : (
                  <CalciteSegmentedControl
                    scale="s"
                    width="full"
                    onCalciteSegmentedControlChange={(e) => {
                      const value = (e.target as HTMLCalciteSegmentedControlElement).value as string;
                      if (value && value !== forced) force.mutate({ domain: chain.domain, rung: value });
                    }}
                  >
                    {rungs.map((rung) => (
                      <CalciteSegmentedControlItem
                        key={rung}
                        value={rung}
                        checked={rung === forced || undefined}
                      >
                        {rung}
                      </CalciteSegmentedControlItem>
                    ))}
                  </CalciteSegmentedControl>
                )}

                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-muted-foreground">
                    Forced: <span className="font-medium text-foreground">{forced ?? "none"}</span>
                  </span>
                  <CalciteButton
                    appearance="outline"
                    kind="neutral"
                    scale="s"
                    disabled={(!forced || clear.isPending) || undefined}
                    onClick={() => clear.mutate(chain.domain)}
                  >
                    Clear
                  </CalciteButton>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* ---- Realism status panel ---- */}
      <div className="grid grid-cols-1 gap-3 px-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>ANPR / OCR accuracy</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {ocrQ.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner /> probing…
              </div>
            ) : ocrQ.data ? (
              <div className="flex items-baseline gap-2">
                <span className="text-3xl font-semibold tabular-nums" style={{ color: STATUS.ok }}>
                  {(ocrQ.data.clear_accuracy * 100).toFixed(1)}%
                </span>
                <span className="text-xs text-muted-foreground">OCR accuracy · CLEAR condition</span>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Eval endpoint not exposed — target is{" "}
                <span className="font-medium text-foreground">≥95% in CLEAR</span> per the per-condition spec.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Congestion forecaster F1</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {f1Q.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner /> probing…
              </div>
            ) : f1Q.data ? (
              <div className="flex items-baseline gap-2">
                <span className="text-3xl font-semibold tabular-nums" style={{ color: STATUS.ok }}>
                  {f1Q.data.f1.toFixed(2)}
                </span>
                <span className="text-xs text-muted-foreground">F1 · NH-348 build-up forecast</span>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Metrics endpoint not exposed — the forecast remains{" "}
                <span className="font-medium text-foreground">advisory</span> only.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* ---- KPI strip deltas (reuse existing component + adapter) ---- */}
      <div className="px-4 pt-4">
        <h2 className="mb-2 text-sm font-semibold">KPI strip — deltas vs baseline</h2>
        <KpiStrip />
      </div>

      {/* ---- Mode badge + decision-path legend ---- */}
      <div className="px-4 pt-4">
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>Mode & decision-path legend</CardTitle>
            <CalciteChip scale="s" kind={DATA_MODE === "live" ? "brand" : "neutral"}>
              {DATA_MODE.toUpperCase()} MODE
            </CalciteChip>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3">
            {["LIVE", "CACHED", "SYNTHETIC", "PROVISIONAL"].map((p) => (
              <DecisionPathBadge key={p} path={p} />
            ))}
            <span className="text-[11px] text-muted-foreground">
              Provenance badges follow the same CB-safe palette as the chains above.
            </span>
          </CardContent>
        </Card>
      </div>

      {/* ---- Preview controls (no backend yet — demo-local only) ---- */}
      <div className="p-4">
        <CalciteBlock open heading="Presenter controls (preview)" description="Not wired to a backend yet — demo-local only.">
          <div className="flex items-center gap-2 px-1 pb-2">
            <Joystick className="h-4 w-4 text-muted-foreground" aria-hidden />
            <span className="text-[11px] text-muted-foreground">
              These adjust local state only; no fault or scenario is posted.
            </span>
          </div>
          <PreviewControls />
        </CalciteBlock>
      </div>
    </div>
  );
}

// Demo-local-only controls. Each renders a "(preview)" chip and mutates local
// state ONLY — there is intentionally no adapter call here (no fabricated
// backend). They demonstrate the intended presenter surface without claiming an
// effect they don't have.
function PreviewControls() {
  const [feeds, setFeeds] = useState(true);
  const [clockX, setClockX] = useState(1);
  const [fleet, setFleet] = useState(25000);

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium">Feeds</span>
          <CalciteChip scale="s" kind="neutral">(preview)</CalciteChip>
        </div>
        <CalciteButton
          appearance={feeds ? "solid" : "outline"}
          kind="neutral"
          scale="s"
          iconStart={feeds ? "pause" : "play"}
          onClick={() => setFeeds((v) => !v)}
        >
          {feeds ? "Stop feeds (demo-local)" : "Start feeds (demo-local)"}
        </CalciteButton>
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium">Demo clock</span>
          <CalciteChip scale="s" kind="neutral">(preview)</CalciteChip>
        </div>
        <input
          type="range"
          min={1}
          max={60}
          step={1}
          value={clockX}
          onChange={(e) => setClockX(Number(e.target.value))}
          className="w-full"
          aria-label="Demo clock speed multiplier"
        />
        <span className="text-[11px] text-muted-foreground tabular-nums">{clockX}× speed (demo-local)</span>
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium">Fleet size</span>
          <CalciteChip scale="s" kind="neutral">(preview)</CalciteChip>
        </div>
        <input
          type="range"
          min={20000}
          max={30000}
          step={1000}
          value={fleet}
          onChange={(e) => setFleet(Number(e.target.value))}
          className="w-full"
          aria-label="Fleet size"
        />
        <span className="text-[11px] text-muted-foreground tabular-nums">
          {fleet.toLocaleString()} trucks (demo-local)
        </span>
      </div>
    </div>
  );
}
