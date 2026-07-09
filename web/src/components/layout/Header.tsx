import { RotateCcw, Wifi, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusDot } from "@/components/ui/misc";
import { useScenario, SCENARIO_LABELS, type ScenarioId } from "@/hooks/ScenarioContext";
import { useSocket } from "@/hooks/SocketContext";
import { activeBasemapProvider } from "@/lib/basemap";

const SCENARIO_COLOUR: Record<ScenarioId, string> = {
  none: "#009E73",
  "TFC-1": "#E69F00",
  "TFC-2": "#D55E00",
  "TFC-3": "#56B4E9",
  "MONSOON-FRIDAY": "#0072B2",
};

export function Header() {
  const { scenario, reset } = useScenario();
  const { status } = useSocket();
  const connected = status === "open";
  const provider = activeBasemapProvider();

  return (
    <header className="flex items-center justify-between gap-4 border-b border-border bg-card/40 px-5 py-3">
      <div className="flex items-center gap-3">
        <span className="text-sm font-semibold">Active scenario</span>
        <Badge colour={SCENARIO_COLOUR[scenario]} className="text-sm">
          {SCENARIO_LABELS[scenario]}
        </Badge>
      </div>

      <div className="flex items-center gap-4">
        <span
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          title={`Basemap provider: ${
            {
              mapbox: "Mapbox Satellite",
              esri: "Esri World Imagery (Satellite)",
              carto: "Carto Positron",
              bhuvan: "Bhuvan (ISRO) WMS",
            }[provider]
          }`}
        >
          map: {provider}
        </span>
        <span className="flex items-center gap-1.5 text-xs" role="status" aria-live="polite">
          <StatusDot colour={connected ? "#009E73" : "#D55E00"} pulse={connected} />
          {connected ? (
            <>
              <Wifi className="h-3.5 w-3.5" aria-hidden /> Live
            </>
          ) : (
            <>
              <WifiOff className="h-3.5 w-3.5" aria-hidden /> {status}
            </>
          )}
        </span>
        <Button variant="outline" size="sm" onClick={reset} disabled={scenario === "none"}>
          <RotateCcw className="h-3.5 w-3.5" aria-hidden />
          Reset to baseline
        </Button>
      </div>
    </header>
  );
}
