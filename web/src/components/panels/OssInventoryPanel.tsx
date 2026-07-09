import { useQuery } from "@tanstack/react-query";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { Spinner } from "@/components/ui/misc";

// OSS Inventory panel (UC-3 audit P2 Task 9). Lists the open-source stack with
// purpose + license. Reads the backend single-source (GET /api/oss-inventory)
// and falls back to a bundled mirror so it renders offline / in the mock build.

interface OssItem {
  name: string;
  purpose: string;
  license: string;
  where?: string;
}

// Bundled mirror of gateway/routers/meta.py OSS_INVENTORY — offline fallback.
const FALLBACK: OssItem[] = [
  {
    name: "YOLOv8 (Ultralytics)",
    purpose: "ANPR licence-plate detection",
    license: "AGPL-3.0",
    where: "ai/anpr",
  },
  {
    name: "PaddleOCR (PP-OCRv4)",
    purpose: "Plate text recognition (OCR)",
    license: "Apache-2.0",
    where: "ai/anpr",
  },
  {
    name: "ByteTrack",
    purpose: "Multi-object tracking (anomaly)",
    license: "MIT",
    where: "ai/anomaly",
  },
  {
    name: "GraphSAGE + LSTM",
    purpose: "Congestion-onset forecasting",
    license: "MIT (project code)",
    where: "ai/congestion",
  },
  {
    name: "ArcGIS Maps SDK for JS",
    purpose: "Corridor / geofence maps",
    license: "Esri EULA",
    where: "web",
  },
  {
    name: "Apache Kafka",
    purpose: "Event backbone (CloudEvents)",
    license: "Apache-2.0",
    where: "infra",
  },
  {
    name: "FastAPI",
    purpose: "Gateway + microservice framework",
    license: "MIT",
    where: "gateway",
  },
  {
    name: "TimescaleDB / PostgreSQL",
    purpose: "Time-series + relational store",
    license: "Apache-2.0 / PostgreSQL",
    where: "infra",
  },
  {
    name: "Redis",
    purpose: "Frame bus + prediction cache",
    license: "BSD-3 / RSALv2",
    where: "infra",
  },
  {
    name: "Eclipse Mosquitto (MQTT)",
    purpose: "Telemetry ingest",
    license: "EPL-2.0 / EDL",
    where: "infra",
  },
  { name: "React + Vite", purpose: "Dashboard + driver PWA", license: "MIT", where: "web" },
  {
    name: "Prometheus + Grafana",
    purpose: "Metrics + observability",
    license: "Apache-2.0 / AGPL-3.0",
    where: "infra",
  },
];

async function fetchInventory(): Promise<OssItem[]> {
  try {
    const res = await fetch("/api/oss-inventory");
    if (res.ok) {
      const data = (await res.json()) as { components?: OssItem[] };
      if (Array.isArray(data.components) && data.components.length) return data.components;
    }
  } catch {
    /* offline / mock — fall through */
  }
  return FALLBACK;
}

export function OssInventoryPanel() {
  const q = useQuery({ queryKey: ["oss-inventory"], queryFn: fetchInventory, staleTime: Infinity });
  const items = q.data ?? FALLBACK;

  return (
    <CollapsibleCard
      id="oss-inventory"
      title="Open-Source Inventory"
      subtitle="The OSS stack — purpose & license"
      bodyClassName="space-y-2"
    >
      {q.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> loading
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] border-collapse text-[12px]">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 pr-3 font-medium">Component</th>
                <th className="py-1 pr-3 font-medium">Purpose</th>
                <th className="py-1 pr-3 font-medium">License</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.name} className="border-t border-border align-top">
                  <td className="py-1.5 pr-3 font-medium">
                    {it.name}
                    {it.where && (
                      <span className="ml-1 text-[10px] text-muted-foreground">· {it.where}</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-muted-foreground">{it.purpose}</td>
                  <td className="py-1.5 pr-3">
                    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">
                      {it.license}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </CollapsibleCard>
  );
}

export default OssInventoryPanel;
