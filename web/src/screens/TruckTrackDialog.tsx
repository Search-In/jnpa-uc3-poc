// NLDS/LDB-style truck Port Events tracker — opened from Vehicle Management Fleet
// Actions → Track. Uses the data adapter (mock when gateway is down; live via
// gateway when VITE_DATA_MODE=live). All actions stay in-app — no external redirects.

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  MapPin,
  Ship,
  Truck,
  Download,
  ShieldCheck,
  ChevronDown,
  X,
} from "lucide-react";
import { getAdapter } from "@/data";
import type { LdbTruckTracking } from "@/lib/types";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/misc";
import { Card } from "@/components/ui/card";

type Props = {
  open: boolean;
  vehicleNumber: string | null;
  onClose: () => void;
};

type Panel = "none" | "compliance" | "map";

function downloadTrackingReport(tracking: LdbTruckTracking, plate: string) {
  const lines: string[] = [
    "JNPA UC-III — Truck Tracking Report",
    `Vehicle: ${tracking.truckNumber || plate}`,
    `Type: ${tracking.truckType || "—"}`,
    `Generated: ${new Date().toISOString()}`,
    "",
  ];
  if (tracking.alert) {
    lines.push(`Alert: ${tracking.alert}`, "");
  }
  lines.push("Port Events");
  lines.push("event,time,terminal,container,mode,lat,lon");
  for (const ev of tracking.events || []) {
    lines.push(
      [
        ev.eventName ?? "",
        ev.eventTimeLabel ?? String(ev.eventTime ?? ""),
        ev.locName ?? "",
        ev.containerNumber ?? "",
        ev.transportMode ?? "",
        ev.locLat ?? "",
        ev.locLong ?? "",
      ]
        .map((c) => `"${String(c).replace(/"/g, '""')}"`)
        .join(","),
    );
  }
  if (tracking.compliance) {
    const c = tracking.compliance;
    lines.push(
      "",
      "Compliance",
      `status,${c.status}`,
      `owner,${c.owner ?? ""}`,
      `class,${c.vehicleClass ?? ""}`,
      `fitness,${c.fitnessValidUpto ?? ""}`,
      `insurance,${c.insuranceValidUpto ?? ""}`,
      `puc,${c.pucValidUpto ?? ""}`,
      `chassis,${c.chassisNumber ?? ""}`,
      `engine,${c.engineNumber ?? ""}`,
    );
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `truck-track-${plate || "report"}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function osmEmbedUrl(lat: string, lon: string): string {
  const la = Number(lat);
  const lo = Number(lon);
  if (!Number.isFinite(la) || !Number.isFinite(lo)) return "";
  const d = 0.04;
  const bbox = `${lo - d},${la - d},${lo + d},${la + d}`;
  return `https://www.openstreetmap.org/export/embed.html?bbox=${encodeURIComponent(bbox)}&layer=mapnik&marker=${encodeURIComponent(`${la},${lo}`)}`;
}

export default function TruckTrackDialog({ open, vehicleNumber, onClose }: Props) {
  const { t } = useTranslation();
  const plate = (vehicleNumber || "").trim().toUpperCase();
  const [eventsOpen, setEventsOpen] = useState(true);
  const [panel, setPanel] = useState<Panel>("none");

  const q = useQuery({
    queryKey: ["ldb-truck", plate],
    queryFn: () => getAdapter().ldbTruck(plate),
    enabled: open && !!plate,
    retry: false,
  });

  const tracking = q.data?.tracking;
  const terminals = tracking?.terminals ?? [];
  const alert = tracking?.alert;
  const source = q.data?.source;
  const compliance = tracking?.compliance;

  const mapSrc = useMemo(() => {
    const lat = tracking?.latest?.locLat;
    const lon = tracking?.latest?.locLong;
    if (!lat || !lon) return "";
    return osmEmbedUrl(lat, lon);
  }, [tracking?.latest?.locLat, tracking?.latest?.locLong]);

  const togglePanel = (next: Panel) => setPanel((p) => (p === next ? "none" : next));

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          setPanel("none");
          onClose();
        }
      }}
    >
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Truck className="h-5 w-5 text-primary" />
            {t("vehicles.trackTitle", "Truck Tracking")}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4 px-1 pb-2">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="font-mono text-2xl font-bold tracking-wide text-primary">
                {plate || "—"}
              </div>
              <div className="mt-1 text-[11px] text-muted-foreground">
                {tracking?.truckType ? (
                  <span className="mr-2 rounded bg-muted px-1.5 py-0.5 font-semibold uppercase">
                    {tracking.truckType}
                  </span>
                ) : null}
                {source ? (
                  <span>
                    {t("vehicles.trackSource", "Source")}: {source}
                  </span>
                ) : null}
              </div>
              <button
                type="button"
                className={`mt-2 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-semibold hover:bg-muted ${
                  panel === "compliance"
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border bg-background"
                }`}
                onClick={() => togglePanel("compliance")}
                disabled={!tracking && !q.isLoading}
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                {t("vehicles.checkCompliance", "Check Vehicle Compliance")}
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-semibold hover:bg-muted ${
                  panel === "map"
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border"
                }`}
                onClick={() => togglePanel("map")}
                disabled={!tracking}
              >
                <MapPin className="h-3.5 w-3.5" />
                {t("vehicles.routeMap", "Route Map")}
              </button>
              <button
                type="button"
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-[11px] font-semibold hover:bg-muted disabled:opacity-50"
                onClick={() => tracking && downloadTrackingReport(tracking, plate)}
                disabled={!tracking}
              >
                <Download className="h-3.5 w-3.5" />
                {t("vehicles.downloadReport", "Download Report")}
              </button>
            </div>
          </div>

          {panel === "compliance" && (
            <Card className="border-border p-3">
              <div className="mb-2 flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-sm font-semibold">
                  <ShieldCheck className="h-4 w-4 text-primary" />
                  {t("vehicles.complianceTitle", "Vehicle Compliance")}
                </div>
                <button
                  type="button"
                  className="rounded p-1 text-muted-foreground hover:bg-muted"
                  onClick={() => setPanel("none")}
                  aria-label="Close"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
              {!compliance ? (
                <p className="text-sm text-muted-foreground">
                  {t(
                    "vehicles.complianceUnavailable",
                    "Compliance details are not available for this vehicle yet.",
                  )}
                </p>
              ) : (
                <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-[12px] sm:grid-cols-2">
                  <div>
                    <dt className="text-muted-foreground">Status</dt>
                    <dd
                      className={`font-semibold ${
                        compliance.status === "COMPLIANT"
                          ? "text-emerald-700"
                          : compliance.status === "NON_COMPLIANT"
                            ? "text-severity-critical"
                            : "text-foreground"
                      }`}
                    >
                      {compliance.status}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Owner</dt>
                    <dd className="font-medium">{compliance.owner || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Class</dt>
                    <dd className="font-medium">{compliance.vehicleClass || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Fitness valid upto</dt>
                    <dd className="font-medium">{compliance.fitnessValidUpto || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Insurance valid upto</dt>
                    <dd className="font-medium">{compliance.insuranceValidUpto || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">PUC valid upto</dt>
                    <dd className="font-medium">{compliance.pucValidUpto || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Chassis</dt>
                    <dd className="font-mono font-medium">{compliance.chassisNumber || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Engine</dt>
                    <dd className="font-mono font-medium">{compliance.engineNumber || "—"}</dd>
                  </div>
                  {compliance.notes ? (
                    <div className="sm:col-span-2">
                      <dt className="text-muted-foreground">Notes</dt>
                      <dd className="text-muted-foreground">{compliance.notes}</dd>
                    </div>
                  ) : null}
                </dl>
              )}
            </Card>
          )}

          {panel === "map" && (
            <Card className="overflow-hidden border-border">
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <div className="flex items-center gap-1.5 text-sm font-semibold">
                  <MapPin className="h-4 w-4 text-primary" />
                  {t("vehicles.routeMap", "Route Map")}
                </div>
                <button
                  type="button"
                  className="rounded p-1 text-muted-foreground hover:bg-muted"
                  onClick={() => setPanel("none")}
                  aria-label="Close"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
              {mapSrc ? (
                <iframe
                  title={t("vehicles.routeMap", "Route Map")}
                  src={mapSrc}
                  className="h-64 w-full border-0"
                  loading="lazy"
                  referrerPolicy="no-referrer-when-downgrade"
                />
              ) : (
                <p className="p-4 text-sm text-muted-foreground">
                  {t("vehicles.routeMapUnavailable", "No coordinates available for this truck.")}
                </p>
              )}
              {tracking?.latest ? (
                <div className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
                  <span className="font-semibold text-foreground">
                    {tracking.latest.locName || "—"}
                  </span>
                  {tracking.latest.locLat && tracking.latest.locLong ? (
                    <span className="ml-2 font-mono">
                      {tracking.latest.locLat}, {tracking.latest.locLong}
                    </span>
                  ) : null}
                </div>
              ) : null}
            </Card>
          )}

          <Card className="overflow-hidden border-border">
            <button
              type="button"
              onClick={() => setEventsOpen((v) => !v)}
              className="flex w-full items-center justify-between bg-[#1e4b8e] px-3 py-2 text-left text-sm font-semibold text-white"
            >
              <span>{t("vehicles.portEvents", "Port Events")}</span>
              <ChevronDown
                className={`h-4 w-4 transition-transform ${eventsOpen ? "rotate-180" : ""}`}
              />
            </button>

            {eventsOpen && (
              <div className="bg-background p-4">
                {!plate ? (
                  <p className="text-sm text-muted-foreground">
                    {t("vehicles.trackNoPlate", "This vehicle has no plate / vehicle number.")}
                  </p>
                ) : q.isLoading ? (
                  <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
                    <Spinner /> {t("vehicles.trackLoading", "Loading port events…")}
                  </div>
                ) : q.isError ? (
                  <p className="py-6 text-sm text-severity-critical">
                    {(q.error as Error)?.message ||
                      t("vehicles.trackFailed", "Failed to load truck tracking.")}
                  </p>
                ) : terminals.length === 0 ? (
                  <p className="py-6 text-sm text-muted-foreground">
                    {t("vehicles.trackEmpty", "No port events found for this truck.")}
                  </p>
                ) : (
                  <div className="relative space-y-6 pl-2">
                    <div className="absolute bottom-2 left-[11px] top-2 w-0.5 bg-orange-500/80" />

                    {terminals.map((term) => (
                      <div key={term.locName} className="relative grid grid-cols-1 gap-3 md:grid-cols-2">
                        <div className="relative rounded-md border border-border bg-card shadow-sm">
                          <div className="absolute -left-[7px] top-5 h-3.5 w-3.5 rounded-full border-2 border-white bg-[#1e4b8e] shadow" />
                          <div className="rounded-t-md bg-[#1e4b8e] px-3 py-1.5 text-[12px] font-semibold text-white">
                            {term.locName}
                          </div>
                          <ul className="divide-y divide-border/70">
                            {term.events.map((ev, idx) => {
                              const name = String(ev.eventName || "").toUpperCase();
                              const isOut = name.includes("OUT");
                              return (
                                <li key={idx} className="flex items-start gap-2.5 px-3 py-2.5">
                                  <span className="mt-0.5 text-[#1e4b8e]">
                                    {isOut ? (
                                      <Truck className="h-4 w-4" />
                                    ) : (
                                      <Ship className="h-4 w-4" />
                                    )}
                                  </span>
                                  <div className="min-w-0 flex-1">
                                    <div className="text-[12px] font-bold uppercase tracking-wide text-foreground">
                                      {ev.eventName || "—"}
                                    </div>
                                    <div className="font-mono text-[11px] font-semibold text-red-600">
                                      {ev.eventTimeLabel || "—"}
                                    </div>
                                  </div>
                                </li>
                              );
                            })}
                          </ul>
                        </div>

                        <div className="md:pl-4">
                          {term.events[0]?.dateMarker ? (
                            <div className="mb-2 text-[11px] font-semibold text-muted-foreground">
                              {term.events[0].dateMarker}
                            </div>
                          ) : null}
                          {alert ? (
                            <div className="relative rounded-md border border-orange-300 bg-orange-500 px-3 py-3 text-[12px] font-semibold leading-snug text-white shadow-sm">
                              <AlertTriangle className="absolute right-2 top-2 h-4 w-4 text-white/90" />
                              <div className="pr-5">{alert}</div>
                            </div>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </Card>
        </div>
      </DialogContent>
    </Dialog>
  );
}
