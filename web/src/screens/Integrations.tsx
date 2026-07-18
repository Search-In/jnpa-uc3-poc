// External Integrations console — Features 12/13/14 (PDP · LDB · RMS-TAS adapters).
// Every adapter response carries an explicit provenance token — `source`
// ("LIVE"/"MOCK"/"DB") on data payloads or a health `mode` — so the operator can
// see, at a glance, whether an external dependency is wired to a real endpoint or
// running on labelled mock data. We render a prominent badge (LIVE green, MOCK
// amber, DB blue) on every card/table. Built entirely on existing api methods;
// loosely typed (any) since these adapters proxy heterogeneous vendor shapes.

import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Network,
  Car,
  Container,
  CalendarClock,
  Search,
  Sprout,
  Ticket,
  Route,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  PageContainer,
  PageHeader,
  StatusChip,
  SegmentedTabs,
  DataTable,
  type Column,
  type Tone,
} from "@/components/ui/dtccc";
import { fmtDateTimeIST } from "@/lib/utils";

type TabKey = "pdp" | "ldb" | "rms";

// --- Provenance badge --------------------------------------------------------
// Maps every provenance token to the mandated colour: LIVE→green, MOCK→amber,
// DB→blue. Health endpoints report `mode` (live|mock); data payloads report
// `source` (LIVE|MOCK|DB). We normalise both through here.

function provenanceTone(raw?: string | null): Tone {
  const v = (raw ?? "").toUpperCase();
  if (v === "LIVE") return "ok";
  if (v === "DB" || v === "CACHED" || v === "RDS") return "info";
  if (v === "MOCK" || v === "SIM" || v === "SYNTHETIC") return "warn";
  return "neutral";
}

function SourceBadge({ value }: { value?: string | null }) {
  const label = (value ?? "MOCK").toUpperCase();
  return <StatusChip label={label} tone={provenanceTone(label)} />;
}

/** Health-endpoint mode → same LIVE/MOCK badge vocabulary. */
function ModeBadge({ mode }: { mode?: string | null }) {
  const label = (mode ?? "mock").toLowerCase() === "live" ? "LIVE" : "MOCK";
  return <StatusChip label={label} tone={provenanceTone(label)} />;
}

export default function Integrations() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabKey>("pdp");

  // Health of each adapter (drives the top row of cards).
  const pdpHealthQ = useQuery({ queryKey: ["pdp-health"], queryFn: () => api.pdpHealth(), retry: false });
  const ldbHealthQ = useQuery({ queryKey: ["ldb-health"], queryFn: () => api.ldbHealth(), retry: false });
  const rmsHealthQ = useQuery({ queryKey: ["rms-health"], queryFn: () => api.rmsHealth(), retry: false });

  const updatedAt = Math.max(
    pdpHealthQ.dataUpdatedAt || 0,
    ldbHealthQ.dataUpdatedAt || 0,
    rmsHealthQ.dataUpdatedAt || 0,
  );
  const anyFetching = pdpHealthQ.isFetching || ldbHealthQ.isFetching || rmsHealthQ.isFetching;

  function refreshAll() {
    void qc.invalidateQueries({ queryKey: ["pdp-health"] });
    void qc.invalidateQueries({ queryKey: ["ldb-health"] });
    void qc.invalidateQueries({ queryKey: ["rms-health"] });
  }

  return (
    <PageContainer>
      <PageHeader
        icon={Network}
        title="External Integrations"
        subtitle="PDP · LDB · RMS-TAS adapters · LIVE / MOCK / DB provenance on every response"
        updatedAt={updatedAt}
        isFetching={anyFetching}
        onRefresh={refreshAll}
      />

      {/* Provenance legend */}
      <div className="flex flex-wrap items-center gap-2 px-4 pt-3 text-[11px] text-muted-foreground">
        <span className="font-medium">Provenance:</span>
        <StatusChip label="LIVE" tone="ok" /> real vendor endpoint
        <StatusChip label="MOCK" tone="warn" /> labelled mock data
        <StatusChip label="DB" tone="info" /> persisted / cached
      </div>

      {/* Adapter health cards */}
      <div className="grid grid-cols-1 gap-3 px-4 py-3 sm:grid-cols-3">
        <HealthCard
          icon={Car}
          name="PDP"
          detail="Parivahan Data Platform — vehicle · permit · traffic"
          query={pdpHealthQ}
        />
        <HealthCard
          icon={Container}
          name="LDB"
          detail="Logistics Data Bank — container tracking · movements"
          query={ldbHealthQ}
        />
        <HealthCard
          icon={CalendarClock}
          name="RMS-TAS"
          detail="Terminal Appointment System — gate slot booking"
          query={rmsHealthQ}
        />
      </div>

      {/* Tabs */}
      <div className="px-4 pb-4">
        <SegmentedTabs
          value={tab}
          onChange={setTab}
          className="mb-3"
          tabs={[
            { key: "pdp", label: "PDP", icon: Car },
            { key: "ldb", label: "LDB", icon: Container },
            { key: "rms", label: "RMS-TAS", icon: CalendarClock },
          ]}
        />

        {tab === "pdp" && <PdpTab />}
        {tab === "ldb" && <LdbTab />}
        {tab === "rms" && <RmsTab />}
      </div>
    </PageContainer>
  );
}

// --- Adapter health card -----------------------------------------------------

function HealthCard({
  icon: Icon,
  name,
  detail,
  query,
}: {
  icon: LucideIcon;
  name: string;
  detail: string;
  query: any;
}) {
  const h: any = query.data;
  const mode = h?.mode;
  const configured = h?.configured;
  const reachable = !query.isError && !!h;
  return (
    <Card className="space-y-2.5 p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Icon className="h-5 w-5" strokeWidth={2} />
          </span>
          <span className="text-sm font-semibold text-foreground">{name}</span>
        </div>
        {query.isLoading ? (
          <StatusChip label="…" tone="neutral" />
        ) : (
          <ModeBadge mode={mode} />
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">{detail}</p>
      <dl className="grid grid-cols-2 gap-x-2 gap-y-1 border-t border-border/60 pt-2 text-[11px] text-muted-foreground">
        <dt>System</dt>
        <dd className="text-right text-foreground">{h?.system ?? name}</dd>
        <dt>Reachable</dt>
        <dd className="text-right text-foreground">{reachable ? "Yes" : "No"}</dd>
        <dt>Configured</dt>
        <dd className="text-right text-foreground">
          {configured == null ? "—" : configured ? "Yes" : "No (mock)"}
        </dd>
      </dl>
    </Card>
  );
}

// --- PDP tab -----------------------------------------------------------------

function PdpTab() {
  const [plateInput, setPlateInput] = useState("");
  const [plate, setPlate] = useState("");

  const vehicleQ = useQuery({
    queryKey: ["pdp-vehicle", plate],
    queryFn: () => api.pdpVehicle(plate),
    enabled: !!plate,
    retry: false,
  });
  const trafficQ = useQuery({ queryKey: ["pdp-traffic"], queryFn: () => api.pdpTraffic(), retry: false });

  const vehicle: any = vehicleQ.data;
  const segments: any[] = trafficQ.data?.segments ?? [];

  const segColumns: Column<any>[] = useMemo(
    () => [
      { key: "segment", header: "Segment", className: "font-medium", render: (s) => s.name ?? s.segment ?? s.id ?? "—" },
      { key: "speed", header: "Speed", align: "right", render: (s) => (s.speed_kmph ?? s.speed ?? null) != null ? `${s.speed_kmph ?? s.speed} km/h` : "—" },
      {
        key: "congestion",
        header: "Congestion",
        render: (s) => {
          const lvl = String(s.congestion ?? s.level ?? s.status ?? "—");
          const tone: Tone = /high|severe|jam/i.test(lvl) ? "critical" : /med/i.test(lvl) ? "warn" : "ok";
          return <StatusChip label={lvl} tone={tone} />;
        },
      },
      { key: "updated", header: "Updated", className: "text-muted-foreground", render: (s) => (s.ts ? fmtDateTimeIST(s.ts) : "—") },
    ],
    [],
  );

  return (
    <div className="space-y-3">
      {/* Vehicle lookup */}
      <Card className="p-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
          <Car className="h-4 w-4 text-muted-foreground" /> Vehicle & Permit Lookup
        </div>
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setPlate(plateInput.trim().toUpperCase());
          }}
        >
          <input
            value={plateInput}
            onChange={(e) => setPlateInput(e.target.value)}
            placeholder="Enter plate e.g. MH04AB1234"
            className="h-9 w-full max-w-xs rounded-md border border-border bg-background px-3 text-[13px] uppercase outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
          />
          <button
            type="submit"
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Search className="h-3.5 w-3.5" /> Lookup
          </button>
        </form>

        {plate && (
          <div className="mt-3">
            {vehicleQ.isLoading ? (
              <p className="text-sm text-muted-foreground">Looking up {plate}…</p>
            ) : vehicleQ.isError ? (
              <p className="text-sm text-severity-critical">Lookup failed for {plate}.</p>
            ) : vehicle ? (
              <div className="rounded-md border border-border bg-background p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-mono text-sm font-semibold text-foreground">
                    {vehicle.plate ?? plate}
                  </span>
                  <SourceBadge value={vehicle.source} />
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[12px]">
                  <Field label="Owner" value={vehicle.owner} />
                  <Field label="Vehicle Class" value={vehicle.vehicle_class} />
                  <Field
                    label="Permit"
                    value={
                      vehicle.permit_valid == null ? (
                        "—"
                      ) : (
                        <StatusChip
                          label={vehicle.permit_valid ? "VALID" : "INVALID"}
                          tone={vehicle.permit_valid ? "ok" : "critical"}
                        />
                      )
                    }
                  />
                  <Field label="RC Status" value={vehicle.rc_status} />
                </dl>
              </div>
            ) : null}
          </div>
        )}
      </Card>

      {/* Traffic segments */}
      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Route className="h-4 w-4 text-muted-foreground" /> Traffic Segments
          </div>
          <SourceBadge value={trafficQ.data?.source} />
        </div>
        <DataTable
          columns={segColumns}
          rows={segments}
          rowKey={(s) => String(s.id ?? s.segment ?? s.name)}
          status={trafficQ}
          onRetry={() => trafficQ.refetch()}
          emptyLabel="No traffic segments reported."
          pageSize={10}
        />
      </Card>
    </div>
  );
}

// --- LDB tab -----------------------------------------------------------------

function LdbTab() {
  const [noInput, setNoInput] = useState("");
  const [containerNo, setContainerNo] = useState("");

  const containerQ = useQuery({
    queryKey: ["ldb-container", containerNo],
    queryFn: () => api.ldbContainer(containerNo),
    enabled: !!containerNo,
    retry: false,
  });
  const movementsQ = useQuery({
    queryKey: ["ldb-movements", containerNo],
    queryFn: () => api.ldbMovements(containerNo),
    enabled: !!containerNo,
    retry: false,
  });

  const tracking: any = containerQ.data?.tracking;
  const movements: any[] = movementsQ.data?.movements ?? [];
  // newest first
  const sortedMovements = useMemo(
    () => [...movements].sort((a, b) => new Date(b.ts ?? 0).getTime() - new Date(a.ts ?? 0).getTime()),
    [movements],
  );

  return (
    <div className="space-y-3">
      <Card className="p-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
          <Container className="h-4 w-4 text-muted-foreground" /> Container Tracking
        </div>
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setContainerNo(noInput.trim().toUpperCase());
          }}
        >
          <input
            value={noInput}
            onChange={(e) => setNoInput(e.target.value)}
            placeholder="Enter container no e.g. MSKU1234567"
            className="h-9 w-full max-w-xs rounded-md border border-border bg-background px-3 text-[13px] uppercase outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
          />
          <button
            type="submit"
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Search className="h-3.5 w-3.5" /> Track
          </button>
        </form>

        {containerNo && (
          <div className="mt-3">
            {containerQ.isLoading ? (
              <p className="text-sm text-muted-foreground">Tracking {containerNo}…</p>
            ) : containerQ.isError ? (
              <p className="text-sm text-severity-critical">Tracking failed for {containerNo}.</p>
            ) : tracking ? (
              <div className="rounded-md border border-border bg-background p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-mono text-sm font-semibold text-foreground">{containerNo}</span>
                  <SourceBadge value={containerQ.data?.source} />
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[12px]">
                  <Field label="Status" value={tracking.status} />
                  <Field label="Location" value={tracking.location} />
                  <Field label="Terminal" value={tracking.terminal} />
                  <Field label="Last Event" value={tracking.last_event ?? tracking.event} />
                  <Field label="Line" value={tracking.line ?? tracking.shipping_line} />
                  <Field label="ETA" value={tracking.eta ? fmtDateTimeIST(tracking.eta) : tracking.eta} />
                </dl>
              </div>
            ) : null}
          </div>
        )}
      </Card>

      {/* Movements timeline */}
      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Route className="h-4 w-4 text-muted-foreground" /> Movement History
            {movementsQ.data?.count != null && (
              <span className="rounded-full bg-muted px-1.5 text-[10px] font-bold tabular-nums text-muted-foreground">
                {movementsQ.data.count}
              </span>
            )}
          </div>
          <SourceBadge value={movementsQ.data?.source} />
        </div>
        {!containerNo ? (
          <p className="px-3 py-6 text-sm text-muted-foreground">
            Enter a container number above to load its movement timeline.
          </p>
        ) : movementsQ.isLoading ? (
          <p className="px-3 py-6 text-sm text-muted-foreground">Loading movements…</p>
        ) : movementsQ.isError ? (
          <p className="px-3 py-6 text-sm text-severity-critical">Failed to load movements.</p>
        ) : sortedMovements.length === 0 ? (
          <p className="px-3 py-6 text-sm text-muted-foreground">No movements recorded.</p>
        ) : (
          <ol className="divide-y divide-border/60">
            {sortedMovements.map((m, i) => (
              <li key={i} className="flex items-start gap-3 px-3 py-2.5">
                <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-primary" aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[13px] font-medium text-foreground">{m.event ?? "—"}</span>
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      {m.ts ? fmtDateTimeIST(m.ts) : "—"}
                    </span>
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                    <span>{m.location ?? "—"}</span>
                    {m.terminal && <span>· {m.terminal}</span>}
                    {m.mode && <StatusChip label={String(m.mode).toUpperCase()} tone={provenanceTone(m.mode)} />}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}

// --- RMS-TAS tab -------------------------------------------------------------

function RmsTab() {
  const qc = useQueryClient();
  const [gateInput, setGateInput] = useState("");
  const [gateId, setGateId] = useState("");

  const slotsQ = useQuery({
    queryKey: ["rms-slots", gateId],
    queryFn: () => api.rmsSlots(gateId ? { gate_id: gateId } : undefined),
    retry: false,
  });

  const seedM = useMutation({
    mutationFn: () => api.rmsSeed({ gate_id: gateId || gateInput.trim(), slots_per_day: 8 }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["rms-slots"] }),
  });
  const bookM = useMutation({
    mutationFn: (slotCode: string) => api.rmsBook({ slot_code: slotCode, vehicle_id: "TRK-000123" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["rms-slots"] }),
  });

  const slots: any[] = slotsQ.data?.slots ?? [];

  const columns: Column<any>[] = useMemo(
    () => [
      { key: "slot_code", header: "Slot", className: "font-mono", render: (s) => s.slot_code ?? "—" },
      { key: "gate", header: "Gate", render: (s) => s.gate_id ?? "—" },
      {
        key: "window",
        header: "Window",
        className: "text-muted-foreground",
        render: (s) =>
          s.window_start
            ? `${fmtDateTimeIST(s.window_start)} → ${s.window_end ? fmtDateTimeIST(s.window_end) : "—"}`
            : "—",
      },
      { key: "capacity", header: "Cap", align: "right", render: (s) => s.capacity ?? "—" },
      { key: "booked", header: "Booked", align: "right", render: (s) => s.booked ?? "—" },
      { key: "available", header: "Avail", align: "right", render: (s) => s.available ?? "—" },
      {
        key: "status",
        header: "Status",
        render: (s) => {
          const st = String(s.status ?? "—");
          const tone: Tone = /full|closed/i.test(st) ? "critical" : /open|available/i.test(st) ? "ok" : "neutral";
          return <StatusChip label={st} tone={tone} />;
        },
      },
      {
        key: "action",
        header: "",
        align: "right",
        render: (s) => {
          const available = (s.available ?? 0) > 0;
          return (
            <button
              type="button"
              disabled={!available || bookM.isPending}
              onClick={() => bookM.mutate(s.slot_code)}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[12px] font-medium text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Ticket className="h-3.5 w-3.5" /> Book
            </button>
          );
        },
      },
    ],
    [bookM],
  );

  return (
    <div className="space-y-3">
      <Card className="p-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
          <CalendarClock className="h-4 w-4 text-muted-foreground" /> Gate Appointment Slots
        </div>
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setGateId(gateInput.trim());
          }}
        >
          <input
            value={gateInput}
            onChange={(e) => setGateInput(e.target.value)}
            placeholder="Gate ID e.g. GATE-1"
            className="h-9 w-full max-w-xs rounded-md border border-border bg-background px-3 text-[13px] outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
          />
          <button
            type="submit"
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
          >
            <Search className="h-3.5 w-3.5" /> Load Slots
          </button>
          <button
            type="button"
            disabled={seedM.isPending || !(gateId || gateInput.trim())}
            onClick={() => seedM.mutate()}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] font-medium text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Sprout className="h-3.5 w-3.5" /> {seedM.isPending ? "Seeding…" : "Seed slots"}
          </button>
        </form>
        {seedM.isError && <p className="mt-2 text-[12px] text-severity-critical">Seed failed.</p>}
        {bookM.isError && <p className="mt-2 text-[12px] text-severity-critical">Booking failed.</p>}
        {bookM.isSuccess && (
          <p className="mt-2 text-[12px] text-ok">Slot booked for TRK-000123.</p>
        )}
      </Card>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <CalendarClock className="h-4 w-4 text-muted-foreground" /> Slots
            {slotsQ.data?.count != null && (
              <span className="rounded-full bg-muted px-1.5 text-[10px] font-bold tabular-nums text-muted-foreground">
                {slotsQ.data.count}
              </span>
            )}
          </div>
          <StatusChip label="MOCK" tone="warn" />
        </div>
        <DataTable
          columns={columns}
          rows={slots}
          rowKey={(s) => String(s.slot_code)}
          status={slotsQ}
          onRetry={() => slotsQ.refetch()}
          emptyLabel="No slots. Enter a gate ID and click “Seed slots” to generate a labelled mock day."
          pageSize={12}
        />
      </Card>
    </div>
  );
}

// --- shared field renderer ---------------------------------------------------

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium text-foreground">
        {value == null || value === "" ? "—" : value}
      </dd>
    </>
  );
}
