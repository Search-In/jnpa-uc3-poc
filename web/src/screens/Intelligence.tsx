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
} from "lucide-react";
import { api } from "@/lib/api";
import { getAdapter } from "@/data";
import { ArcgisMap } from "@/components/map/ArcgisMap";
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
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { EmptyState, LoadingState, ErrorState } from "@/components/ui/misc";
import { fmtDateTimeIST, relativeAge } from "@/lib/utils";
import type { TruckDevice, VehicleIntel, DriverIntel } from "@/lib/types";

type Mode = "vehicle" | "driver";
type Row = Record<string, unknown>;

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
          ]}
        />
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
      </div>

      {mode === "vehicle" ? (
        <VehicleProfile plate={submitted} />
      ) : (
        <DriverProfile key={submitted} dl={submitted} />
      )}
    </PageContainer>
  );
}

// --- Vehicle 360 -------------------------------------------------------------

function VehicleProfile({ plate }: { plate: string }) {
  const { basemap } = useMapSettings();
  const enabled = !!plate;

  const viQ = useQuery({
    queryKey: ["vehicle-intel", plate],
    queryFn: () => api.vehicleIntel(plate),
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

  if (!plate) {
    return (
      <div className="p-6">
        <EmptyState>Search a vehicle number to open its full 360° intelligence profile.</EmptyState>
      </div>
    );
  }
  if (viQ.isLoading)
    return (
      <div className="p-6">
        <LoadingState label="Building 360° profile…" />
      </div>
    );
  if (viQ.isError)
    return (
      <div className="p-6">
        <ErrorState onRetry={() => viQ.refetch()} detail={(viQ.error as Error)?.message} />
      </div>
    );
  if (!vi)
    return (
      <div className="p-6">
        <EmptyState>No record found for {plate}.</EmptyState>
      </div>
    );

  const rc = (vi.rc ?? {}) as Row;
  const track = (vi.tracking ?? []) as Row[];
  const last = track[track.length - 1];
  const pseudoTruck: TruckDevice[] = last
    ? [
        {
          device_id: plate,
          plate,
          gate_id: null,
          state: "TRACKED",
          position: { lat: Number(last.lat), lon: Number(last.lon) },
          speed_kmh: Number(last.speed_kmh ?? 0),
          heading: 0,
          remaining_km: 0,
          eta_s: null,
          segment_id: null,
        },
      ]
    : [];

  return (
    <div className="space-y-3 p-4">
      {/* Summary cards */}
      <StatGrid className="lg:grid-cols-6">
        <StatCard
          icon={FileWarning}
          label="Violations"
          value={vi.violations.length}
          tone={vi.violations.length ? "warn" : "ok"}
        />
        <StatCard
          icon={ShieldAlert}
          label="Challans"
          value={vi.challans.length}
          tone={vi.challans.length ? "warn" : "ok"}
        />
        <StatCard
          icon={Bell}
          label="Alerts"
          value={vi.alerts.length}
          tone={vi.alerts.length ? "warn" : "ok"}
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

      {/* Profile + location */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <InfoCard title={`RC · ${vi.vehicle_number}`} icon={Car}>
          <KV label="Owner" value={rc.owner_name_masked ?? rc.owner_name} />
          <KV label="Vehicle class" value={rc.vehicle_class} />
          <KV label="Fuel" value={rc.fuel_type} />
          <KV label="Registered" value={rc.registration_date} />
          <KV label="Fitness upto" value={rc.fitness_valid_to} />
          <KV label="Insurance upto" value={rc.insurance_valid_to} />
          <KV label="RTO / State" value={`${rc.rto_code ?? "—"} / ${rc.state ?? "—"}`} />
          <KV label="Blacklist" value={rc.blacklist_status} />
        </InfoCard>

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
          {last ? (
            <div className="relative h-[220px]">
              <ArcgisMap
                basemap={basemap}
                corridor={corridorQ.data}
                trucks={pseudoTruck}
                center={[Number(last.lon), Number(last.lat)]}
                zoom={13}
              />
            </div>
          ) : (
            <div className="flex h-[220px] items-center justify-center text-sm text-muted-foreground">
              No tracking on record.
            </div>
          )}
        </Card>
      </div>

      {/* Record tabs */}
      <VehicleRecords vi={vi} customs={customs} parking={parking} geo={geo} ai={ai} track={track} />
    </div>
  );
}

type RecTab =
  | "timeline"
  | "violations"
  | "challans"
  | "alerts"
  | "customs"
  | "parking"
  | "geo"
  | "ai"
  | "tracking";

function VehicleRecords({
  vi,
  customs,
  parking,
  geo,
  ai,
  track,
}: {
  vi: VehicleIntel;
  customs: Row[];
  parking: Row[];
  geo: Row[];
  ai: Row[];
  track: Row[];
}) {
  const [tab, setTab] = useState<RecTab>("timeline");

  const timeline = useMemo(
    () => buildTimeline(vi, customs, parking, geo, ai),
    [vi, customs, parking, geo, ai],
  );

  return (
    <div>
      <SegmentedTabs
        value={tab}
        onChange={setTab}
        className="mb-3"
        tabs={[
          { key: "timeline", label: "Timeline", count: timeline.length },
          { key: "violations", label: "Violations", count: vi.violations.length },
          { key: "challans", label: "Challans", count: vi.challans.length },
          { key: "alerts", label: "Alerts", count: vi.alerts.length },
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
            rows={vi.violations}
            cols={[
              ["case_id", "Case"],
              ["status", "Status"],
              ["total_fine", "Fine"],
              ["first_detected_at", "Detected"],
            ]}
            empty="No violations on record."
            searchKeys={["case_id", "status"]}
          />
        )}
        {tab === "challans" && (
          <RecordsTable
            rows={vi.challans}
            cols={[
              ["challan_no", "Challan"],
              ["total_fine", "Fine"],
              ["status", "Status"],
              ["issued_at", "Issued"],
            ]}
            empty="No challans on record."
            searchKeys={["challan_no", "status"]}
          />
        )}
        {tab === "alerts" && (
          <RecordsTable
            rows={vi.alerts}
            cols={[
              ["kind", "Kind"],
              ["severity", "Severity"],
              ["ts", "When"],
            ]}
            empty="No alerts on record."
            searchKeys={["kind", "severity"]}
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
            empty="No tracking on record."
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

// --- Shared bits -------------------------------------------------------------

function InfoCard({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon: typeof Car;
  children: React.ReactNode;
}) {
  return (
    <Card className="p-0">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <Icon className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
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
  if (/CRITICAL|BLOCKED|VIOLATION|TAMPERED|REJECT|FAIL/.test(u)) return "critical";
  if (/WARN|PENDING|PROVISIONAL|ENTER|ELEVATED/.test(u)) return "warn";
  if (/OK|READY|ACTIVE|VERIFIED|EXIT|PAID|CLOSED/.test(u)) return "ok";
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
  vi: VehicleIntel,
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
  for (const v of vi.violations as Row[])
    push(
      v.first_detected_at ?? v.created_at,
      "Violation",
      `Violation ${v.case_id ?? ""} · ${v.status ?? ""}`,
      "warn",
    );
  for (const c of vi.challans as Row[])
    push(c.issued_at, "Challan", `Challan ${c.challan_no ?? ""} · ₹${c.total_fine ?? ""}`, "warn");
  for (const a of vi.alerts as Row[])
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
