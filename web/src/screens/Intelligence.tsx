// Vehicle & Driver Intelligence — enterprise 360° investigation dashboard.
// One entity (vehicle or driver) → its complete RDS-backed profile: RC/DL, FASTag,
// current location + route, violations, challans, alerts, customs / parking /
// geo-fence history, AI events and a merged timeline. Driven by the header Global
// Search (searchStore hand-off) or the on-page search. Every panel reuses existing
// endpoints (/api/vahan/*, /api/fastag/*, /api/gate-data/*, /api/parking/*,
// /api/geo/*, /api/ai/*) with UNCHANGED query keys — no backend changes.

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Car,
  IdCard,
  Search,
  ShieldAlert,
  FileWarning,
  Bell,
  MapPinned,
  SquareParking,
  CreditCard,
  ScanSearch,
  ScanFace,
  Repeat,
  Camera,
  BarChart3,
  History,
  Building2,
  ShieldCheck,
} from "lucide-react";
import { api } from "@/lib/api";
import { getAdapter } from "@/data";
import DoubleTrip from "@/screens/DoubleTrip";
import CameraAI from "@/screens/CameraAI";
import { ArcgisMap } from "@/components/map/ArcgisMap";
import {
  VehicleIdentityDialog,
  VehicleDetectionDialog,
} from "@/components/panels/VehicleIntelChecks";
import { useMapSettings } from "@/lib/mapSettings";
import { useGlobalSearch } from "@/lib/searchStore";
import { Card } from "@/components/ui/card";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
  SegmentedTabs,
  DataTable,
  StatusChip,
  Embedded,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { EmptyState, LoadingState, ErrorState, Spinner } from "@/components/ui/misc";
import { cn, fmtDateTimeIST, relativeAge } from "@/lib/utils";
import type { TruckDevice, DriverIntel } from "@/lib/types";

type Mode = "vehicle" | "driver" | "doubletrip" | "cameraai" | "driveranalytics";
type Row = Record<string, unknown>;

const SEARCH_MODES: Mode[] = ["vehicle", "driver"];

export default function Intelligence() {
  const [mode, setMode] = useState<Mode>("vehicle");
  const [term, setTerm] = useState("");
  const [submitted, setSubmitted] = useState<string>("");
  const [params] = useSearchParams();
  const gs = useGlobalSearch();

  // Hand-off from the header Global Search (store nonce) or a ?q= deep link.
  useEffect(() => {
    if (!gs.query) return;
    const m: Mode = gs.entity === "driver" ? "driver" : "vehicle";
    setMode(m);
    setTerm(gs.query);
    setSubmitted(m === "vehicle" ? gs.query.toUpperCase() : gs.query);
  }, [gs.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const q = params.get("q");
    if (q && !submitted) {
      setTerm(q);
      setSubmitted(mode === "vehicle" ? q.toUpperCase() : q);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function run() {
    const t = term.trim();
    if (!t) return;
    setSubmitted(mode === "vehicle" ? t.toUpperCase() : t);
  }

  return (
    <PageContainer>
      <PageHeader
        icon={ScanSearch}
        title="Vehicle & Driver Intelligence"
        subtitle="360° investigation · Vahan · Sarathi · FASTag · Customs · Geo — RDS-backed"
      />

      {/* Search bar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-card px-4 py-3">
        <SegmentedTabs
          value={mode}
          onChange={(m) => setMode(m)}
          tabs={[
            { key: "vehicle", label: "Vehicle", icon: Car },
            { key: "driver", label: "Driver", icon: IdCard },
            { key: "doubletrip", label: "Double Trip Analytics", icon: Repeat },
            { key: "cameraai", label: "Camera AI Statistics", icon: Camera },
            { key: "driveranalytics", label: "Driver Analytics", icon: BarChart3 },
          ]}
        />
        {SEARCH_MODES.includes(mode) && (
          <>
            <div className="relative min-w-0 flex-1 sm:max-w-md">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                value={term}
                onChange={(e) => setTerm(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && run()}
                placeholder={
                  mode === "vehicle" ? "Vehicle no / RC e.g. MH04AB1234" : "DL number or driver id"
                }
                className="h-9 w-full rounded-md border border-border bg-background pl-9 pr-3 text-[13px] outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
              />
            </div>
            <button
              onClick={run}
              className="inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90"
            >
              <Search className="h-4 w-4" /> Search
            </button>
          </>
        )}
      </div>

      {mode === "vehicle" ? (
        <VehicleProfile plate={submitted} />
      ) : mode === "driver" ? (
        <DriverProfile key={submitted} dl={submitted} />
      ) : mode === "doubletrip" ? (
        <Embedded>
          <DoubleTrip />
        </Embedded>
      ) : mode === "cameraai" ? (
        <Embedded>
          <CameraAI />
        </Embedded>
      ) : (
        <DriverAnalytics />
      )}
    </PageContainer>
  );
}

// --- Vehicle 360 -------------------------------------------------------------

function VehicleProfile({ plate }: { plate: string }) {
  const { basemap } = useMapSettings();
  const enabled = !!plate;
  // Vehicle Intelligence camera workflows (RC card header actions).
  const [identityOpen, setIdentityOpen] = useState(false);
  const [detectionOpen, setDetectionOpen] = useState(false);

  // ONE call for the whole profile. /api/vahan/vehicle-360 resolves the master
  // spine (vehicle -> assigned driver -> transport company) server-side and
  // embeds the vehicle-intel payload under `intel`, so the screen never pays for
  // a client-side lookup chain.
  const viQ = useQuery({
    queryKey: ["vehicle-360", plate],
    queryFn: () => api.vehicle360(plate),
    enabled,
  });
  const fbQ = useQuery({
    queryKey: ["fastag-balance", plate],
    queryFn: () => api.fastagBalance(plate),
    enabled,
    retry: false,
  });
  const ftQ = useQuery({
    queryKey: ["fastag-tx-history", plate],
    queryFn: () => api.fastagTransactionsHistory(plate, 100),
    enabled,
    retry: false,
  });
  const corridorQ = useQuery({
    queryKey: ["corridor"],
    queryFn: () => getAdapter().corridor(),
    staleTime: Infinity,
    enabled,
  });
  // Cross-domain history (existing endpoints; filtered to this plate client-side).
  const customsQ = useQuery({
    queryKey: ["customs-history"],
    queryFn: () => api.customsHistory(200),
    enabled,
  });
  const parkingQ = useQuery({
    queryKey: ["parking-hist"],
    queryFn: () => api.parkingHistory(200),
    enabled,
  });
  const geoQ = useQuery({
    queryKey: ["geo-events"],
    queryFn: () => api.geoEvents(undefined, 200),
    enabled,
  });
  const aiQ = useQuery({
    queryKey: ["ai-events"],
    queryFn: () => api.aiEvents(undefined, 200),
    enabled,
  });

  const vi = viQ.data;

  // Normalise the payload BEFORE anything reads it. The endpoint returns 200
  // with a lean body in several legitimate cases — an empty envelope when the
  // gateway has no DSN, and per-field defaults when one of the concurrent
  // lookups raises — so `.length` on a raw field can throw mid-render. With no
  // error boundary above this screen a render throw unmounts the tree and the
  // panel stays frozen on the last painted frame, i.e. "Building 360° profile…"
  // forever. Every field below is read through these guards instead.
  // Memoised so the normalised arrays keep a stable identity across renders —
  // they are the dependencies of the timeline memo further down.
  const { rc, track, violations, challans, alerts, verifications, lifecycle } = useMemo(
    () => ({
      rc: asRecord(vi?.intel?.rc),
      track: asArray(vi?.intel?.tracking),
      violations: asArray(vi?.intel?.violations),
      challans: asArray(vi?.intel?.challans),
      alerts: asArray(vi?.alerts),
      verifications: asArray(vi?.intel?.verification_history),
      lifecycle: asArray(vi?.timeline),
    }),
    [vi],
  );
  const vehicle = vi?.vehicle ?? null;
  const driver = vi?.driver ?? null;
  const licence = driver?.license ?? null;
  const transporter = vi?.transporter ?? null;
  const compliance = vi?.compliance ?? null;
  const vehicleNumber = String(
    vehicle?.number ?? vi?.plate ?? rc.plate ?? plate ?? "",
  ).toUpperCase();

  const matchPlate = (v: unknown) => String(v ?? "").toUpperCase() === plate;
  const customs = useMemo(
    () => (customsQ.data?.alerts ?? []).filter((a) => matchPlate(a.plate)) as unknown as Row[],
    [customsQ.data, plate],
  );
  const parking = useMemo(
    () =>
      (parkingQ.data?.transactions ?? []).filter((t) =>
        matchPlate(t.vehicle_id),
      ) as unknown as Row[],
    [parkingQ.data, plate],
  );
  const geo = useMemo(
    () => (geoQ.data?.events ?? []).filter((e) => matchPlate(e.vehicle_id)) as unknown as Row[],
    [geoQ.data, plate],
  );
  const ai = useMemo(
    () => (aiQ.data?.events ?? []).filter((e) => matchPlate(e.vehicle_id)) as unknown as Row[],
    [aiQ.data, plate],
  );

  // Does the response actually carry a profile? A 200 with `{}` is "nothing to
  // show", not a success — but it is also not an error, so it gets its own state.
  const hasProfile =
    !!vi &&
    (vi.found === true ||
      !!vehicle ||
      !!driver ||
      !!transporter ||
      Object.keys(rc).length > 0 ||
      track.length + violations.length + challans.length + alerts.length + verifications.length >
        0);

  if (!plate) {
    return (
      <div className="p-6">
        <EmptyState>Search a vehicle number to open its full 360° intelligence profile.</EmptyState>
      </div>
    );
  }
  // Exhaustive terminal states while there is no payload: error, paused (the
  // browser is offline — TanStack would otherwise sit in `pending` forever),
  // in-flight, and settled-but-empty. Once `vi` exists we always render, so the
  // spinner can never outlive the request.
  if (!vi) {
    if (viQ.isError)
      return (
        <div className="p-6">
          <ErrorState onRetry={() => void viQ.refetch()} detail={apiMessage(viQ.error)} />
        </div>
      );
    if (viQ.fetchStatus === "paused")
      return (
        <div className="p-6">
          <ErrorState
            onRetry={() => void viQ.refetch()}
            detail="Network unavailable — the request is paused."
          />
        </div>
      );
    if (viQ.isPending) return <Vehicle360Skeleton />;
  }
  if (!hasProfile)
    return (
      <div className="p-6">
        <EmptyState>No vehicle intelligence data available for {plate}.</EmptyState>
      </div>
    );

  // Telemetry arrives newest-first (ORDER BY ts DESC); the current position is
  // the newest fix with usable coordinates, not the last row of the array.
  const last = latestFix(track);
  const lat = finite(last?.lat);
  const lon = finite(last?.lon);
  const pseudoTruck: TruckDevice[] =
    lat != null && lon != null
      ? [
          {
            device_id: plate,
            plate,
            gate_id: null,
            state: "TRACKED",
            position: { lat, lon },
            speed_kmh: finite(last?.speed_kmh) ?? 0,
            heading: 0,
            remaining_km: 0,
            eta_s: null,
            segment_id: null,
          },
        ]
      : [];

  const blacklist = String(rc.blacklist_status ?? "").trim();

  return (
    <div className="space-y-3 p-4">
      {/* Vehicle header — the registration number is the primary identifier;
          every technical id stays secondary. */}
      <Card className="p-0">
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
          <Car className="h-5 w-5 text-primary" />
          <span className="font-mono text-xl font-semibold tracking-wide text-foreground">
            {vehicleNumber}
          </span>
          <StatusChip
            label={vehicle?.status ? String(vehicle.status) : "NOT IN MASTER"}
            tone={vehicle?.status ? statusTone(String(vehicle.status)) : "neutral"}
          />
          {blacklist ? (
            <StatusChip label={`Blacklist · ${blacklist}`} tone={blacklistTone(blacklist)} />
          ) : null}
          {rc.updated_at ? (
            <span className="ml-auto text-[11px] text-muted-foreground">
              RC updated {relativeAge(String(rc.updated_at))}
            </span>
          ) : null}
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-1 px-4 py-3 sm:grid-cols-3 lg:grid-cols-5">
          <Field label="Vehicle ID" value={vehicle?.id} mono />
          <Field label="Vehicle class" value={vehicle?.class ?? rc.vehicle_class} />
          <Field label="Fuel" value={vehicle?.fuel ?? rc.fuel_type} />
          <Field label="Vehicle type" value={vehicle?.type} />
          <Field
            label="Assignment"
            value={humanizeCode(vehicle?.assignment_status)}
            tone={assignmentTone(vehicle?.assignment_status)}
          />
        </div>
      </Card>

      {/* A refetch that fails over a profile we already hold must not blank the
          screen — surface it inline and keep the last good data on display. */}
      {viQ.isError ? (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px]"
        >
          <span className="font-medium">Refresh failed — showing the last loaded profile.</span>
          <span className="truncate text-muted-foreground">{apiMessage(viQ.error)}</span>
          <button
            type="button"
            onClick={() => void viQ.refetch()}
            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 font-medium hover:bg-muted"
          >
            Retry
          </button>
        </div>
      ) : null}

      {/* Summary cards */}
      <StatGrid className="lg:grid-cols-6">
        <StatCard
          icon={FileWarning}
          label="Violations"
          value={violations.length}
          tone={violations.length ? "warn" : "ok"}
        />
        <StatCard
          icon={ShieldAlert}
          label="Challans"
          value={challans.length}
          tone={challans.length ? "warn" : "ok"}
        />
        <StatCard
          icon={Bell}
          label="Alerts"
          value={alerts.length}
          tone={alerts.length ? "warn" : "ok"}
        />
        <StatCard
          icon={MapPinned}
          label="Geo-fence"
          value={geo.length}
          tone={geo.length ? "warn" : "ok"}
          loading={geoQ.isLoading}
        />
        <StatCard
          icon={SquareParking}
          label="Parking"
          value={parking.length}
          tone="info"
          loading={parkingQ.isLoading}
        />
        <StatCard
          icon={ShieldAlert}
          label="Customs"
          value={customs.length}
          tone={customs.length ? "critical" : "ok"}
          loading={customsQ.isLoading}
        />
      </StatGrid>

      {/* Who is driving it, on whose licence, for which company. */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <InfoCard title="Driver Information" icon={IdCard}>
          {driver ? (
            <>
              <div className="mb-2 flex items-center gap-3 sm:col-span-2">
                <DriverPhoto src={driver.photo} name={driver.name} />
                <div className="min-w-0">
                  <div className="truncate text-[15px] font-semibold text-foreground">
                    {driver.name || NA}
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                    <StatusChip
                      label={driver.status ? String(driver.status) : "STATUS UNKNOWN"}
                      tone={driver.status ? statusTone(String(driver.status)) : "neutral"}
                    />
                    {driver.enrollment_status ? (
                      <StatusChip
                        label={`Enrollment · ${driver.enrollment_status}`}
                        tone={statusTone(String(driver.enrollment_status))}
                      />
                    ) : null}
                  </div>
                </div>
              </div>
              <Field label="Driver ID" value={driver.id} mono />
              <Field label="Mobile" value={driver.mobile} />
              <Field label="Date of birth" value={fmtDateOrNA(driver.dob)} />
              <Field label="Enrolled" value={fmtDateOrNA(driver.enrolled_at)} />
            </>
          ) : (
            <div className="py-2 text-[13px] text-muted-foreground sm:col-span-2">
              No driver is currently assigned to this vehicle.
            </div>
          )}
        </InfoCard>

        <InfoCard title="License" icon={ScanSearch}>
          {licence && (licence.number || licence.in_master) ? (
            <>
              <Field label="License number" value={licence.number} mono />
              <Field label="License type" value={licence.type} />
              <Field label="Valid until" value={fmtDateOrNA(licence.valid_until)} />
              <Field
                label="License validity"
                value={humanizeCode(licence.validity?.status)}
                tone={validityTone(licence.validity?.status)}
              />
              <Field
                label="PDP status"
                value={humanizeCode(licence.pdp_status)}
                tone={validityTone(licence.pdp_status)}
              />
              <Field label="PDP number" value={licence.pdp_number} mono />
              <Field
                label="Verification"
                value={humanizeCode(licence.verification_status)}
                tone={
                  licence.verification_status ? statusTone(licence.verification_status) : undefined
                }
              />
              <Field label="Verified on" value={fmtDateOrNA(licence.verified_at)} />
            </>
          ) : (
            <div className="py-2 text-[13px] text-muted-foreground sm:col-span-2">
              {driver
                ? "This driver has no licence record in the driver master."
                : "No licence to show until a driver is assigned."}
            </div>
          )}
        </InfoCard>

        <InfoCard title="Transporter" icon={Building2}>
          {transporter ? (
            <>
              <div className="mb-1 sm:col-span-2">
                <div className="truncate text-[15px] font-semibold text-foreground">
                  {transporter.name || NA}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5">
                  <StatusChip
                    label={transporter.status ? String(transporter.status) : "STATUS UNKNOWN"}
                    tone={
                      transporter.blacklisted
                        ? "critical"
                        : transporter.status
                          ? statusTone(String(transporter.status))
                          : "neutral"
                    }
                  />
                  <span className="text-[11px] text-muted-foreground">
                    {TRANSPORTER_SOURCE[String(transporter.source)] ?? ""}
                  </span>
                </div>
              </div>
              <Field label="Transporter ID" value={transporter.id} mono />
              <Field label="Transporter code" value={transporter.code} mono />
              <Field label="GSTIN" value={transporter.gstin} mono />
              <Field label="Contact" value={transporter.contact} />
              {transporter.blacklisted ? (
                <Field
                  label="Blacklist reason"
                  value={transporter.blacklist_reason}
                  tone="critical"
                />
              ) : null}
            </>
          ) : (
            <div className="py-2 text-[13px] text-muted-foreground sm:col-span-2">
              This vehicle is not mapped to a transport company.
            </div>
          )}
        </InfoCard>
      </div>

      {/* Compliance & risk + lifecycle */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <InfoCard title="Compliance & Risk" icon={ShieldCheck}>
          <Field
            label="RC"
            value={humanizeCode(compliance?.rc?.status)}
            tone={compliance?.rc?.status === "ON_RECORD" ? "ok" : "neutral"}
          />
          <Field
            label="Insurance"
            value={validityLabel(compliance?.insurance)}
            tone={validityTone(compliance?.insurance?.status)}
          />
          <Field
            label="PUC"
            value={validityLabel(compliance?.puc)}
            tone={validityTone(compliance?.puc?.status)}
          />
          <Field
            label="Fitness"
            value={validityLabel(compliance?.fitness)}
            tone={validityTone(compliance?.fitness?.status)}
          />
          <Field
            label="Blacklist"
            value={humanizeCode(compliance?.blacklist?.status)}
            tone={
              compliance?.blacklist?.status === "BLACKLISTED"
                ? "critical"
                : compliance?.blacklist?.status === "CLEAR"
                  ? "ok"
                  : "neutral"
            }
          />
          <Field label="FASTag" value={humanizeCode(compliance?.fastag?.status)} />
          <Field
            label="Alerts"
            value={String(alerts.length)}
            tone={alerts.length ? "warn" : "ok"}
          />
          {compliance?.blacklist?.reason ? (
            <div className="pt-1 text-[12px] text-muted-foreground sm:col-span-2">
              {String(compliance.blacklist.reason)}
            </div>
          ) : null}
        </InfoCard>

        <SectionCard title="Operational Timeline" icon={History} count={lifecycle.length}>
          <LifecycleTimeline steps={lifecycle} />
        </SectionCard>

        <SectionCard title="Alerts" icon={Bell} count={alerts.length}>
          {alerts.length === 0 ? (
            <EmptyState>No alerts found.</EmptyState>
          ) : (
            <ul className="divide-y divide-border/50">
              {alerts.slice(0, 8).map((a, i) => (
                <li
                  key={String(a.id ?? i)}
                  className="flex items-center gap-2 px-3 py-2 text-[13px]"
                >
                  <StatusChip
                    label={isBlank(a.severity) ? NA : String(a.severity)}
                    tone={statusTone(String(a.severity ?? ""))}
                  />
                  <span className="truncate">{isBlank(a.kind) ? "Alert" : String(a.kind)}</span>
                  <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                    {isBlank(a.ts) ? NA : fmtDateTimeIST(String(a.ts))}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>
      </div>

      {/* RC + FASTag + location */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <InfoCard
          title={`RC Details · ${vehicleNumber}`}
          icon={Car}
          actions={
            <>
              <button
                type="button"
                onClick={() => setIdentityOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted"
              >
                <ScanFace className="h-3.5 w-3.5" /> Identity
              </button>
              <button
                type="button"
                onClick={() => setDetectionOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[11px] font-medium hover:bg-muted"
              >
                <ShieldAlert className="h-3.5 w-3.5" /> Detection
              </button>
            </>
          }
        >
          {/* Core RC fields always render (with an em-dash when the column is
              null); the registration/validity fields only appear when the RC row
              actually carries them — nothing here is invented. */}
          <KV label="Plate" value={rc.plate ?? vehicleNumber} />
          <KV label="Vehicle class" value={rc.vehicle_class} />
          <KV label="Fuel type" value={rc.fuel_type} />
          <KV label="Blacklist status" value={rc.blacklist_status} />
          {RC_OPTIONAL_FIELDS.map(([key, label, kind]) =>
            isBlank(rc[key]) ? null : (
              <KV key={key} label={label} value={formatValue(rc[key], kind)} />
            ),
          )}
          {Object.keys(rc).length === 0 ? (
            <div className="py-1 text-[13px] text-muted-foreground sm:col-span-2">
              No RC record on file for this vehicle.
            </div>
          ) : null}
        </InfoCard>

        <VehicleIdentityDialog
          vehicleNumber={vehicleNumber}
          open={identityOpen}
          onOpenChange={setIdentityOpen}
        />
        <VehicleDetectionDialog
          vehicleNumber={vehicleNumber}
          open={detectionOpen}
          onOpenChange={setDetectionOpen}
        />

        <InfoCard title="FASTag" icon={CreditCard}>
          {fbQ.isLoading ? (
            <LoadingState />
          ) : fbQ.isError || !fbQ.data ? (
            <div className="py-2 text-xs text-muted-foreground">No FASTag record for this RC.</div>
          ) : (
            <>
              <KV label="Tag status" value={fbQ.data.tag_status} />
              <KV
                label="Balance"
                value={
                  fbQ.data.available_balance != null ? `₹${fbQ.data.available_balance}` : undefined
                }
              />
              <KV label="Bank" value={fbQ.data.provider_name} />
              <KV
                label="Vehicle class"
                value={fbQ.data.vehicle_class_desc ?? fbQ.data.vehicle_class}
              />
              <KV label="Customer" value={fbQ.data.customer_name} />
              <KV
                label="Transactions"
                value={ftQ.data?.count ?? ftQ.data?.transactions?.length ?? 0}
              />
            </>
          )}
        </InfoCard>

        <Card className="overflow-hidden p-0">
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <h3 className="text-sm font-semibold text-foreground">Current Location & Route</h3>
            {last && (
              <span className="text-[11px] text-muted-foreground">
                {relativeAge(String(last.ts))}
              </span>
            )}
          </div>
          {lat != null && lon != null ? (
            <div className="relative h-[220px]">
              <ArcgisMap
                basemap={basemap}
                corridor={corridorQ.data}
                trucks={pseudoTruck}
                center={[lon, lat]}
                zoom={13}
              />
            </div>
          ) : (
            <div className="flex h-[220px] items-center justify-center text-sm text-muted-foreground">
              No tracking data available.
            </div>
          )}
        </Card>
      </div>

      {/* RC verification lineage — the audit trail behind the header status. */}
      <SectionCard title="Verification History" icon={History} count={verifications.length}>
        {verifications.length === 0 ? (
          <EmptyState>No verification history available.</EmptyState>
        ) : (
          <ul className="divide-y divide-border/50">
            {verifications.slice(0, 10).map((v, i) => (
              <li key={i} className="flex items-center gap-2 px-3 py-2 text-[13px]">
                <StatusChip
                  label={isBlank(v.verification_status) ? NA : String(v.verification_status)}
                  tone={statusTone(String(v.verification_status ?? ""))}
                />
                <span className="truncate text-muted-foreground">
                  {isBlank(v.source) ? NA : String(v.source)}
                </span>
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                  {isBlank(v.created_at) ? NA : fmtDateTimeIST(String(v.created_at))}
                </span>
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

      {/* Record tabs */}
      <VehicleRecords
        violations={violations}
        challans={challans}
        alerts={alerts}
        verifications={verifications}
        customs={customs}
        parking={parking}
        geo={geo}
        ai={ai}
        track={track}
      />
    </div>
  );
}

type RecTab =
  | "timeline"
  | "violations"
  | "challans"
  | "alerts"
  | "verifications"
  | "customs"
  | "parking"
  | "geo"
  | "ai"
  | "tracking";

function VehicleRecords({
  violations,
  challans,
  alerts,
  verifications,
  customs,
  parking,
  geo,
  ai,
  track,
}: {
  violations: Row[];
  challans: Row[];
  alerts: Row[];
  verifications: Row[];
  customs: Row[];
  parking: Row[];
  geo: Row[];
  ai: Row[];
  track: Row[];
}) {
  const [tab, setTab] = useState<RecTab>("timeline");

  const timeline = useMemo(
    () => buildTimeline(violations, challans, alerts, customs, parking, geo, ai),
    [violations, challans, alerts, customs, parking, geo, ai],
  );

  return (
    <div>
      <SegmentedTabs
        value={tab}
        onChange={setTab}
        className="mb-3"
        tabs={[
          { key: "timeline", label: "Timeline", count: timeline.length },
          { key: "violations", label: "Violations", count: violations.length },
          { key: "challans", label: "Challans", count: challans.length },
          { key: "alerts", label: "Alerts", count: alerts.length },
          { key: "verifications", label: "Verifications", count: verifications.length },
          { key: "customs", label: "Customs", count: customs.length },
          { key: "parking", label: "Parking", count: parking.length },
          { key: "geo", label: "Geo-fence", count: geo.length },
          { key: "ai", label: "AI Events", count: ai.length },
          { key: "tracking", label: "Tracking", count: track.length },
        ]}
      />
      <Card className="overflow-hidden">
        {tab === "timeline" && <Timeline events={timeline} />}
        {tab === "violations" && (
          <RecordsTable
            rows={violations}
            cols={[
              ["case_id", "Case"],
              ["status", "Status"],
              ["total_fine", "Fine"],
              ["first_detected_at", "Detected"],
            ]}
            empty="No violations found."
            searchKeys={["case_id", "status"]}
          />
        )}
        {tab === "challans" && (
          <RecordsTable
            rows={challans}
            cols={[
              ["challan_no", "Challan"],
              ["total_fine", "Fine"],
              ["status", "Status"],
              ["issued_at", "Issued"],
            ]}
            empty="No challans found."
            searchKeys={["challan_no", "status"]}
          />
        )}
        {tab === "alerts" && (
          <RecordsTable
            rows={alerts}
            cols={[
              ["kind", "Kind"],
              ["severity", "Severity"],
              ["ts", "When"],
            ]}
            empty="No alerts found."
            searchKeys={["kind", "severity"]}
          />
        )}
        {tab === "verifications" && (
          <RecordsTable
            rows={verifications}
            cols={[
              ["verification_status", "Status"],
              ["source", "Source"],
              ["created_at", "When"],
            ]}
            empty="No verification history available."
            searchKeys={["verification_status", "source"]}
          />
        )}
        {tab === "customs" && (
          <RecordsTable
            rows={customs}
            cols={[
              ["_flag", "Flag"],
              ["severity", "Severity"],
              ["_container", "Container"],
              ["ts", "Raised"],
            ]}
            empty="No customs history for this vehicle."
            searchKeys={["severity"]}
          />
        )}
        {tab === "parking" && (
          <RecordsTable
            rows={parking}
            cols={[
              ["facility_id", "Facility"],
              ["entry_time", "Entry"],
              ["exit_time", "Exit"],
              ["status", "Status"],
            ]}
            empty="No parking history for this vehicle."
            searchKeys={["facility_id", "status"]}
          />
        )}
        {tab === "geo" && (
          <RecordsTable
            rows={geo}
            cols={[
              ["event_type", "Event"],
              ["zone_id", "Zone"],
              ["violation_type", "Violation"],
              ["created_at", "When"],
            ]}
            empty="No geo-fence history for this vehicle."
            searchKeys={["event_type", "zone_id"]}
          />
        )}
        {tab === "ai" && (
          <RecordsTable
            rows={ai}
            cols={[
              ["event_type", "AI Event"],
              ["zone_id", "Zone"],
              ["created_at", "When"],
            ]}
            empty="No AI events for this vehicle."
            searchKeys={["event_type"]}
          />
        )}
        {tab === "tracking" && (
          <RecordsTable
            rows={track}
            cols={[
              ["ts", "Time"],
              ["lat", "Lat"],
              ["lon", "Lon"],
              ["speed_kmh", "Speed"],
            ]}
            empty="No tracking data available."
          />
        )}
      </Card>
    </div>
  );
}

// --- Driver profile ----------------------------------------------------------

function DriverProfile({ dl }: { dl: string }) {
  const enabled = !!dl;
  const diQ = useQuery({
    queryKey: ["driver-intel", dl],
    queryFn: () => api.driverIntel(dl),
    enabled,
  });
  const dlQ = useQuery({
    queryKey: ["dl-lookup", dl],
    queryFn: () => api.dlLookup(dl),
    enabled,
    retry: false,
  });

  if (!dl)
    return (
      <div className="p-6">
        <EmptyState>Search a DL number or driver id to see the driver profile.</EmptyState>
      </div>
    );
  if (diQ.isLoading)
    return (
      <div className="p-6">
        <LoadingState label="Building driver profile…" />
      </div>
    );
  if (diQ.isError)
    return (
      <div className="p-6">
        <ErrorState onRetry={() => diQ.refetch()} detail={(diQ.error as Error)?.message} />
      </div>
    );
  const di = diQ.data as DriverIntel | undefined;
  if (!di)
    return (
      <div className="p-6">
        <EmptyState>No driver found for {dl}.</EmptyState>
      </div>
    );

  const d = (di.driver ?? {}) as Row;
  const dlRec = (dlQ.data?.record ?? {}) as Row;

  return (
    <div className="space-y-3 p-4">
      <StatGrid className="lg:grid-cols-4">
        <StatCard
          icon={FileWarning}
          label="Violations"
          value={di.violations.length}
          tone={di.violations.length ? "warn" : "ok"}
        />
        <StatCard icon={IdCard} label="DL Lookups" value={di.dl_history.length} tone="info" />
        <StatCard icon={ScanSearch} label="Verifications" value={di.activity.length} tone="info" />
        <StatCard icon={Car} label="Vehicle" value={di.vehicle_no ?? "—"} tone="neutral" />
      </StatGrid>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <InfoCard title="Driver profile" icon={IdCard}>
          <KV label="Name" value={d.name} />
          <KV label="Licence" value={d.license_no} />
          <KV label="Status" value={d.status} />
          <KV label="Provider" value={d.provider} />
          <KV label="Vehicle" value={di.vehicle_no} />
          <KV label="Mobile" value={d.mobile} />
        </InfoCard>
        <InfoCard title="DL (Sarathi)" icon={ScanSearch}>
          {dlQ.isLoading ? (
            <LoadingState />
          ) : dlQ.isError || !dlQ.data ? (
            <div className="py-2 text-xs text-muted-foreground">No live DL record.</div>
          ) : (
            <>
              <KV label="DL" value={dlQ.data.dl} />
              <KV label="Status" value={dlQ.data.status} />
              <KV label="Decision path" value={dlQ.data.decision_path} />
              <KV label="Class" value={dlRec.cov ?? dlRec.vehicle_class} />
              <KV label="Valid upto" value={dlRec.valid_upto ?? dlRec.doe} />
            </>
          )}
        </InfoCard>
      </div>

      <DriverRecords di={di} />
    </div>
  );
}

function DriverRecords({ di }: { di: DriverIntel }) {
  const [tab, setTab] = useState<"dl" | "violations" | "activity">("dl");
  return (
    <div>
      <SegmentedTabs
        value={tab}
        onChange={setTab}
        className="mb-3"
        tabs={[
          { key: "dl", label: "DL Lookup History", count: di.dl_history.length },
          { key: "violations", label: "Vehicle Violations", count: di.violations.length },
          { key: "activity", label: "Verification Activity", count: di.activity.length },
        ]}
      />
      <Card className="overflow-hidden">
        {tab === "dl" && (
          <RecordsTable
            rows={di.dl_history}
            cols={[
              ["status", "Status"],
              ["source", "Source"],
              ["created_at", "When"],
            ]}
            empty="No DL lookups on record."
            searchKeys={["status"]}
          />
        )}
        {tab === "violations" && (
          <RecordsTable
            rows={di.violations}
            cols={[
              ["case_id", "Case"],
              ["status", "Status"],
              ["total_fine", "Fine"],
            ]}
            empty="No violations on record."
            searchKeys={["case_id", "status"]}
          />
        )}
        {tab === "activity" && (
          <RecordsTable
            rows={di.activity}
            cols={[
              ["decision", "Decision"],
              ["score", "Score"],
              ["ts", "When"],
            ]}
            empty="No verification activity."
            searchKeys={["decision"]}
          />
        )}
      </Card>
    </div>
  );
}

// --- Driver Analytics (lightweight, reuses existing intel endpoints) --------

function DriverAnalytics() {
  const vhQ = useQuery({
    queryKey: ["verification-history", 100],
    queryFn: () => api.verificationHistory(100),
  });
  const dhQ = useQuery({
    queryKey: ["dl-history", 100],
    queryFn: () => api.dlHistory(100),
  });

  const verifications = (vhQ.data?.history ?? []) as Row[];
  const dlLookups = (dhQ.data?.history ?? []) as Row[];

  const uniquePlates = useMemo(() => {
    const set = new Set<string>();
    for (const v of verifications) {
      const p = String(v.vehicle_number ?? "").trim();
      if (p) set.add(p.toUpperCase());
    }
    return set.size;
  }, [verifications]);

  if (vhQ.isLoading || dhQ.isLoading)
    return (
      <div className="p-6">
        <LoadingState label="Loading driver analytics…" />
      </div>
    );
  if (vhQ.isError || dhQ.isError)
    return (
      <div className="p-6">
        <ErrorState
          onRetry={() => {
            vhQ.refetch();
            dhQ.refetch();
          }}
          detail={((vhQ.error ?? dhQ.error) as Error)?.message}
        />
      </div>
    );

  return (
    <div className="space-y-3 p-4">
      <StatGrid className="lg:grid-cols-3">
        <StatCard
          icon={ScanSearch}
          label="Total Verifications"
          value={vhQ.data?.count ?? verifications.length}
          tone="info"
        />
        <StatCard icon={Car} label="Unique Plates" value={uniquePlates} tone="neutral" />
        <StatCard
          icon={IdCard}
          label="Total DL Lookups"
          value={dhQ.data?.count ?? dlLookups.length}
          tone="info"
        />
      </StatGrid>

      <Card className="overflow-hidden">
        <div className="border-b border-border px-3 py-2">
          <h3 className="text-sm font-semibold text-foreground">Recent Verifications</h3>
        </div>
        <RecordsTable
          rows={verifications}
          cols={[
            ["vehicle_number", "Plate"],
            ["verification_status", "Status"],
            ["source", "Source"],
            ["created_at", "When"],
          ]}
          empty="No verification activity on record."
          searchKeys={["vehicle_number", "verification_status", "source"]}
        />
      </Card>
    </div>
  );
}

// --- Vehicle 360 presentation bits -------------------------------------------

/** The single placeholder for "the source row has no value here". Used across
 *  every 360 card so a blank never reads as a zero or a clean status. */
const NA = "Not Available";

/** Labelled value in the DTCCC card grid. Stacked (label above value) because
 *  these cards mix long names with short codes. */
function Field({
  label,
  value,
  tone,
  mono,
}: {
  label: string;
  value: unknown;
  tone?: Tone;
  mono?: boolean;
}) {
  const blank = isBlank(value);
  return (
    <div className="border-b border-border/40 py-1.5">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div
        className={cn(
          "truncate text-[13px] font-medium",
          mono && !blank && "font-mono",
          blank ? "text-muted-foreground" : tone ? TONE_TEXT[tone] : "text-foreground",
        )}
        title={blank ? NA : String(value)}
      >
        {blank ? NA : String(value)}
      </div>
    </div>
  );
}

const TONE_TEXT: Record<Tone, string> = {
  ok: "text-emerald-600 dark:text-emerald-400",
  warn: "text-amber-600 dark:text-amber-400",
  critical: "text-red-600 dark:text-red-400",
  info: "text-sky-600 dark:text-sky-400",
  neutral: "text-foreground",
};

/** Enrolment photo with an initials fallback — a broken image URL must not leave
 *  a torn placeholder in the driver card. */
function DriverPhoto({ src, name }: { src?: string | null; name?: string | null }) {
  const [failed, setFailed] = useState(false);
  const initials = String(name ?? "")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("");
  if (src && !failed) {
    return (
      <img
        src={src}
        alt={name ? `Photo of ${name}` : "Driver photo"}
        onError={() => setFailed(true)}
        className="h-12 w-12 shrink-0 rounded-full border border-border object-cover"
      />
    );
  }
  return (
    <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-border bg-muted text-sm font-semibold text-muted-foreground">
      {initials || <IdCard className="h-5 w-5" />}
    </div>
  );
}

/** ASSIGNED_TO_JOB -> "Assigned to job". Backend codes are SCREAMING_SNAKE; the
 *  operator should never have to read one. */
function humanizeCode(v: unknown): string | undefined {
  if (isBlank(v)) return undefined;
  const s = String(v).replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function fmtDateOrNA(v: unknown): string | undefined {
  if (isBlank(v)) return undefined;
  return fmtDateIST(v);
}

/** "Valid · till 12/05/2027" — status and the date that justifies it together. */
function validityLabel(
  v?: { status?: string | null; valid_to?: string | null } | null,
): string | undefined {
  if (!v || isBlank(v.status) || v.status === "NOT_AVAILABLE") return undefined;
  const label = humanizeCode(v.status);
  return v.valid_to ? `${label} · till ${fmtDateIST(v.valid_to)}` : label;
}

function validityTone(status?: string | null): Tone | undefined {
  switch (String(status ?? "").toUpperCase()) {
    case "VALID":
    case "ACTIVE":
      return "ok";
    case "EXPIRING":
      return "warn";
    case "EXPIRED":
    case "CANCELLED":
      return "critical";
    default:
      return undefined;
  }
}

function assignmentTone(status?: string | null): Tone | undefined {
  const u = String(status ?? "").toUpperCase();
  if (!u || u === "UNASSIGNED") return undefined;
  if (u === "DRIVER_ASSIGNED") return "info";
  return "ok";
}

/** Where the transport company came from — the vehicle mapping is authoritative;
 *  the others are stated so an operator knows how firm the link is. */
const TRANSPORTER_SOURCE: Record<string, string> = {
  vehicle_mapping: "via vehicle mapping",
  driver_employer: "via driver's employer",
  driver_company: "via driver record",
};

/** Vehicle lifecycle: registered -> enrolled -> assigned -> gate -> cargo -> now. */
function LifecycleTimeline({ steps }: { steps: Row[] }) {
  if (steps.length === 0)
    return <EmptyState>No lifecycle events recorded for this vehicle.</EmptyState>;
  return (
    <ol className="relative space-y-0 p-3 pl-5">
      <span className="absolute left-[11px] top-5 bottom-5 w-px bg-border" aria-hidden />
      {steps.map((s, i) => {
        const current = s.stage === "CURRENT_STATUS";
        return (
          <li key={i} className="relative flex gap-3 pb-3 last:pb-0">
            <span
              className="absolute -left-[9px] mt-1 h-3 w-3 rounded-full ring-4 ring-card"
              style={{ backgroundColor: current ? toneColour("info") : toneColour("ok") }}
            />
            <div className="ml-3 min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-[13px] font-medium text-foreground">
                  {String(s.label ?? s.stage ?? "Event")}
                </span>
                {isBlank(s.ts) ? null : (
                  <span className="text-[11px] text-muted-foreground">
                    {fmtDateTimeIST(String(s.ts))}
                  </span>
                )}
              </div>
              {isBlank(s.detail) ? null : (
                <div className="truncate text-[12px] text-muted-foreground">{String(s.detail)}</div>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** Skeleton that mirrors the real layout, so the page does not reflow when the
 *  data lands. The label keeps the familiar "Building 360° profile…" wording. */
function Vehicle360Skeleton() {
  const bar = "animate-pulse rounded bg-muted";
  return (
    <div className="space-y-3 p-4" aria-busy="true" aria-live="polite">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> Building 360° profile…
      </div>
      <Card className="p-4">
        <div className={cn(bar, "h-6 w-48")} />
        <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="space-y-1.5">
              <div className={cn(bar, "h-2.5 w-16")} />
              <div className={cn(bar, "h-3.5 w-24")} />
            </div>
          ))}
        </div>
      </Card>
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Card key={i} className="space-y-2 p-4">
            <div className={cn(bar, "h-4 w-32")} />
            {Array.from({ length: 4 }).map((__, j) => (
              <div key={j} className={cn(bar, "h-3 w-full")} />
            ))}
          </Card>
        ))}
      </div>
    </div>
  );
}

// --- Shared bits -------------------------------------------------------------

/** Array-or-nothing. Every list field on the intel payload is optional server-side. */
function asArray(v: unknown): Row[] {
  return Array.isArray(v) ? (v as Row[]) : [];
}

/** Object-or-nothing (`rc` is null for vehicles with no RC row). */
function asRecord(v: unknown): Row {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Row) : {};
}

function isBlank(v: unknown): boolean {
  return v == null || v === "";
}

/** Number when the value really is one — `Number(null)` is 0, which would put a
 *  vehicle with no fix on the map at (0, 0). */
function finite(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) && v !== null && v !== "" ? n : null;
}

/** Newest telemetry row that carries usable coordinates. */
function latestFix(track: Row[]): Row | undefined {
  const fixes = track.filter((t) => finite(t.lat) != null && finite(t.lon) != null);
  if (fixes.length === 0) return undefined;
  return fixes.reduce((best, r) =>
    (Date.parse(String(r.ts)) || 0) > (Date.parse(String(best.ts)) || 0) ? r : best,
  );
}

/** Human-readable message off a thrown api error (falls back to a generic line). */
function apiMessage(err: unknown): string | undefined {
  const msg = err instanceof Error ? err.message : err ? String(err) : "";
  return msg || undefined;
}

function fmtDateIST(v: unknown): string {
  const d = new Date(String(v));
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
}

function formatValue(v: unknown, kind: FieldKind): string {
  if (kind === "date") return fmtDateIST(v);
  if (kind === "datetime") return fmtDateTimeIST(String(v));
  if (kind === "bool") return v ? "Yes" : "No";
  return String(v);
}

type FieldKind = "text" | "date" | "datetime" | "bool";

/** Registration / validity columns of core.vehicle_rc, rendered only when the
 *  row actually has them — never substituted with placeholder values. */
const RC_OPTIONAL_FIELDS: [string, string, FieldKind][] = [
  ["owner_name_masked", "Owner", "text"],
  ["rc_type", "RC type", "text"],
  ["registration_date", "Registration date", "date"],
  ["rto_code", "RTO", "text"],
  ["state", "State", "text"],
  ["fitness_valid_to", "Fitness valid to", "date"],
  ["insurance_valid_to", "Insurance valid to", "date"],
  ["puc_valid_to", "PUC valid to", "date"],
  ["fastag_status", "FASTag status", "text"],
  ["provisional", "Provisional RC", "bool"],
  ["provisional_until", "Provisional until", "datetime"],
  ["updated_at", "Last updated", "datetime"],
];

function blacklistTone(status: string): Tone {
  const u = status.toUpperCase();
  if (/CLEAR|NONE|NO|FALSE|CLEAN/.test(u)) return "ok";
  return "critical";
}

/** Card with a titled header and a free-form body (lists rather than KV pairs). */
function SectionCard({
  title,
  icon: Icon,
  count,
  children,
}: {
  title: string;
  icon: typeof Car;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <Card className="overflow-hidden p-0">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <Icon className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {count != null && (
          <span className="ml-auto rounded bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground">
            {count}
          </span>
        )}
      </div>
      {children}
    </Card>
  );
}

function InfoCard({
  title,
  icon: Icon,
  actions,
  children,
}: {
  title: string;
  icon: typeof Car;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card className="p-0">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <Icon className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {actions && <div className="ml-auto flex items-center gap-1.5">{actions}</div>}
      </div>
      <div className="grid gap-x-6 p-3 sm:grid-cols-2">{children}</div>
    </Card>
  );
}

function KV({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="flex justify-between gap-3 border-b border-border/40 py-1 text-[13px]">
      <span className="text-muted-foreground">{label}</span>
      <span className="truncate text-right font-medium">
        {value == null || value === "" ? "—" : String(value)}
      </span>
    </div>
  );
}

const DATE_KEY = /_at$|_time$|^ts$|^issued|_detected/;

function RecordsTable({
  rows,
  cols,
  empty,
  searchKeys,
}: {
  rows: Row[];
  cols: [string, string][];
  empty: string;
  searchKeys?: string[];
}) {
  const columns: Column<Row>[] = cols.map(([key, header]) => ({
    key,
    header,
    className:
      key === "_container" || key.includes("id") || key === "lat" || key === "lon"
        ? "font-mono"
        : undefined,
    render: (r) => {
      const raw =
        key === "_flag"
          ? (r as any).payload?.flag
          : key === "_container"
            ? (r as any).payload?.container_no
            : r[key];
      if (raw == null || raw === "") return "—";
      if (
        key === "severity" ||
        key === "status" ||
        key === "event_type" ||
        key === "violation_type"
      ) {
        return <StatusChip label={String(raw)} tone={statusTone(String(raw))} />;
      }
      if (DATE_KEY.test(key)) return fmtDateTimeIST(String(raw));
      if (key === "total_fine") return `₹${raw}`;
      return String(raw);
    },
  }));
  const keyed = useMemo(() => rows.map((r, i) => ({ ...r, __k: i })), [rows]);
  return (
    <DataTable
      columns={columns}
      rows={keyed}
      rowKey={(r) => String((r as any).__k)}
      emptyLabel={empty}
      search={
        searchKeys
          ? (r, q) =>
              searchKeys.some((k) =>
                String((r as any)[k] ?? "")
                  .toLowerCase()
                  .includes(q),
              )
          : undefined
      }
      searchPlaceholder="Search…"
      pageSize={10}
    />
  );
}

function statusTone(s: string): Tone {
  const u = s.toUpperCase();
  // Negative states are tested FIRST: "INACTIVE"/"SUSPENDED" contain no negation
  // marker the ok-branch could distinguish, and `ACTIVE` alone matches both.
  if (
    /CRITICAL|HIGH|BLOCKED|VIOLATION|TAMPERED|REJECT|FAIL|INACTIVE|SUSPEND|CANCEL|EXPIRED|BLACKLIST/.test(
      u,
    )
  )
    return "critical";
  if (/WARN|MEDIUM|PENDING|PROVISIONAL|ENTER|ELEVATED|EXPIRING|REENROLL/.test(u)) return "warn";
  if (/OK|READY|ACTIVE|VERIFIED|EXIT|PAID|CLOSED|CLEAR|ENROLLED|VALID/.test(u)) return "ok";
  return "neutral";
}

// --- Timeline ----------------------------------------------------------------

interface TLEvent {
  ts: number;
  iso: string;
  kind: string;
  label: string;
  tone: Tone;
}

function buildTimeline(
  violations: Row[],
  challans: Row[],
  alerts: Row[],
  customs: Row[],
  parking: Row[],
  geo: Row[],
  ai: Row[],
): TLEvent[] {
  const out: TLEvent[] = [];
  const push = (iso: unknown, kind: string, label: string, tone: Tone) => {
    const t = Date.parse(String(iso));
    if (!Number.isNaN(t)) out.push({ ts: t, iso: String(iso), kind, label, tone });
  };
  for (const v of violations)
    push(
      v.first_detected_at ?? v.created_at,
      "Violation",
      `Violation ${v.case_id ?? ""} · ${v.status ?? ""}`,
      "warn",
    );
  for (const c of challans)
    push(c.issued_at, "Challan", `Challan ${c.challan_no ?? ""} · ₹${c.total_fine ?? ""}`, "warn");
  for (const a of alerts)
    push(a.ts, "Alert", `${a.kind ?? "Alert"} · ${a.severity ?? ""}`, "critical");
  for (const g of geo)
    push(
      g.created_at,
      "Geo-fence",
      `${g.event_type ?? g.violation_type ?? "Geo"} · ${g.zone_id ?? ""}`,
      "info",
    );
  for (const p of parking) push(p.entry_time, "Parking", `Parked · ${p.facility_id ?? ""}`, "info");
  for (const c of customs)
    push(c.ts, "Customs", `Customs flag · ${(c as any).payload?.flag ?? ""}`, "critical");
  for (const e of ai) push(e.created_at, "AI", `${e.event_type ?? "AI event"}`, "warn");
  return out.sort((a, b) => b.ts - a.ts);
}

function Timeline({ events }: { events: TLEvent[] }) {
  if (events.length === 0) return <EmptyState>No timeline events for this vehicle.</EmptyState>;
  return (
    <ol className="relative space-y-0 p-4 pl-6">
      <span className="absolute left-[13px] top-4 bottom-4 w-px bg-border" aria-hidden />
      {events.slice(0, 60).map((e, i) => (
        <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
          <span
            className="absolute -left-[11px] mt-1 h-4 w-4 rounded-full ring-4 ring-card"
            style={{ backgroundColor: toneColour(e.tone) }}
          />
          <div className="ml-4 flex flex-1 flex-wrap items-center gap-x-2 gap-y-0.5">
            <StatusChip label={e.kind} tone={e.tone} />
            <span className="text-[13px] text-foreground">{e.label}</span>
            <span
              className="ml-auto text-[11px] text-muted-foreground"
              title={fmtDateTimeIST(e.iso)}
            >
              {relativeAge(e.iso)}
            </span>
          </div>
        </li>
      ))}
    </ol>
  );
}

function toneColour(t: Tone): string {
  return {
    info: "#56B4E9",
    ok: "#009E73",
    warn: "#E69F00",
    critical: "#D55E00",
    neutral: "#64748b",
  }[t];
}
