// Reefer Availability (Feature 11) — powered reefer-slot availability board.
// RDS-backed via /api/reefer/*: an availability header (total / available /
// powered / occupied / fault + free% bar), a status-coloured slot grid, an
// allocate form and per-tile release. Mirrors ParkingManagement conventions
// (default export, react-query, api from @/lib/api, Tailwind, StatCard/StatGrid).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Snowflake,
  Container,
  CheckCircle2,
  Zap,
  ThermometerSnowflake,
  TriangleAlert,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  PageContainer,
  PageHeader,
  StatGrid,
  StatCard,
} from "@/components/ui/dtccc";

// Status → tile styling. AVAILABLE green, OCCUPIED blue, RESERVED amber, FAULT red.
function slotTone(status?: string): { border: string; bg: string; text: string; dot: string } {
  switch ((status ?? "").toUpperCase()) {
    case "AVAILABLE":
      return {
        border: "border-emerald-300",
        bg: "bg-emerald-50",
        text: "text-emerald-700",
        dot: "bg-emerald-500",
      };
    case "OCCUPIED":
      return {
        border: "border-blue-300",
        bg: "bg-blue-50",
        text: "text-blue-700",
        dot: "bg-blue-500",
      };
    case "RESERVED":
      return {
        border: "border-amber-300",
        bg: "bg-amber-50",
        text: "text-amber-700",
        dot: "bg-amber-500",
      };
    case "FAULT":
      return {
        border: "border-red-300",
        bg: "bg-red-50",
        text: "text-red-700",
        dot: "bg-red-500",
      };
    default:
      return {
        border: "border-border",
        bg: "bg-background",
        text: "text-muted-foreground",
        dot: "bg-muted-foreground",
      };
  }
}

function SlotTile({
  slot,
  onRelease,
  releasing,
}: {
  slot: any;
  onRelease: (slotCode: string) => void;
  releasing: boolean;
}) {
  const status = String(slot.status ?? "").toUpperCase();
  const tone = slotTone(status);
  const occupied = status === "OCCUPIED";
  const temp = slot.set_temperature;
  return (
    <div
      className={`relative flex flex-col gap-1 rounded-md border p-2 text-[11px] ${tone.border} ${tone.bg}`}
    >
      <div className="flex items-center justify-between gap-1">
        <span className="font-mono text-xs font-semibold text-foreground">{slot.slot_code}</span>
        {slot.powered ? (
          <span
            className="inline-flex items-center gap-0.5 text-amber-600"
            title="Powered slot"
          >
            <Zap className="h-3 w-3" fill="currentColor" />
          </span>
        ) : (
          <span className="text-[9px] text-muted-foreground" title="No power">
            no pwr
          </span>
        )}
      </div>
      <div className={`inline-flex items-center gap-1 font-medium ${tone.text}`}>
        <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
        <span>{status || "—"}</span>
      </div>
      {occupied && (
        <>
          <div className="truncate font-mono text-[10px] text-foreground" title={slot.container_number}>
            {slot.container_number ?? "—"}
          </div>
          <div className="flex items-center gap-1 text-[10px] text-muted-foreground tabular-nums">
            <ThermometerSnowflake className="h-3 w-3" />
            <span>
              set {temp != null ? `${temp}°C` : "—"}
              {slot.current_temperature != null ? ` · now ${slot.current_temperature}°C` : ""}
            </span>
          </div>
          <button
            type="button"
            disabled={releasing}
            onClick={() => onRelease(slot.slot_code)}
            className="mt-1 rounded border border-border bg-background px-2 py-0.5 text-[10px] font-medium text-foreground hover:bg-muted disabled:opacity-50"
          >
            {releasing ? "Releasing…" : "Release"}
          </button>
        </>
      )}
    </div>
  );
}

export default function ReeferAvailability() {
  const qc = useQueryClient();
  const [facilityFilter, setFacilityFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [containerNumber, setContainerNumber] = useState<string>("");
  const [setTemp, setSetTemp] = useState<string>("-18");

  const availQ = useQuery({
    queryKey: ["reefer-avail"],
    queryFn: () => api.reeferAvailability(),
    refetchInterval: 8000,
  });
  const slotsQ = useQuery({
    queryKey: ["reefer-slots", facilityFilter, statusFilter],
    queryFn: () =>
      api.reeferSlots({
        facility_id: facilityFilter || undefined,
        status: statusFilter || undefined,
      }),
  });

  const totals: any = availQ.data?.totals ?? {};
  const facilities: any[] = availQ.data?.facilities ?? [];
  const slots: any[] = slotsQ.data?.slots ?? [];

  const total = Number(totals.total ?? 0);
  const available = Number(totals.available ?? 0);
  const poweredAvailable = Number(totals.powered_available ?? 0);
  const occupied = Number(totals.occupied ?? 0);
  const fault = Number(totals.fault ?? 0);
  const freePct =
    totals.free_pct != null
      ? Number(totals.free_pct)
      : total > 0
        ? Math.round((available / total) * 100)
        : 0;

  const facilityOptions = useMemo(
    () => facilities.map((f) => String(f.facility_id)).filter(Boolean),
    [facilities],
  );

  function invalidateAll() {
    void qc.invalidateQueries({ queryKey: ["reefer-avail"] });
    void qc.invalidateQueries({ queryKey: ["reefer-slots"] });
  }

  const seedM = useMutation({
    mutationFn: () => api.reeferSeed(24),
    onSuccess: invalidateAll,
  });
  const allocateM = useMutation({
    mutationFn: (body: { container_number: string; set_temperature: number }) =>
      api.reeferAllocate(body),
    onSuccess: () => {
      setContainerNumber("");
      invalidateAll();
    },
  });
  const releaseM = useMutation({
    mutationFn: (slotCode: string) => api.reeferRelease({ slot_code: slotCode }),
    onSuccess: invalidateAll,
  });

  function submitAllocate(e: React.FormEvent) {
    e.preventDefault();
    const cn = containerNumber.trim();
    if (!cn) return;
    const t = Number(setTemp);
    allocateM.mutate({ container_number: cn, set_temperature: Number.isFinite(t) ? t : -18 });
  }

  return (
    <PageContainer>
      <PageHeader
        icon={Snowflake}
        title="Reefer Availability"
        subtitle="Powered reefer-slot capacity & allocation · RDS-backed"
        updatedAt={availQ.dataUpdatedAt}
        isFetching={availQ.isFetching && !availQ.isLoading}
        onRefresh={invalidateAll}
      />

      {/* Availability header */}
      <div className="px-4 pt-3">
        <StatGrid className="lg:grid-cols-5">
          <StatCard
            icon={Container}
            label="Total Slots"
            value={total}
            tone="info"
            loading={availQ.isLoading}
          />
          <StatCard
            icon={CheckCircle2}
            label="Available"
            value={available}
            tone="ok"
            loading={availQ.isLoading}
          />
          <StatCard
            icon={Zap}
            label="Powered Available"
            value={poweredAvailable}
            tone="info"
            loading={availQ.isLoading}
          />
          <StatCard
            icon={Snowflake}
            label="Occupied"
            value={occupied}
            tone="warn"
            loading={availQ.isLoading}
          />
          <StatCard
            icon={TriangleAlert}
            label="Fault"
            value={fault}
            tone={fault > 0 ? "critical" : "ok"}
            loading={availQ.isLoading}
          />
        </StatGrid>

        {/* Free % progress bar */}
        <Card className="mt-3 p-3">
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="font-medium text-foreground">Free capacity</span>
            <span className="tabular-nums text-muted-foreground">
              {freePct}% free · {available}/{total}
            </span>
          </div>
          <div className="h-2.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-emerald-500 transition-all"
              style={{ width: `${Math.min(100, Math.max(0, freePct))}%` }}
            />
          </div>
        </Card>
      </div>

      {/* Empty state → seed */}
      {total === 0 ? (
        <div className="px-4 py-6">
          <Card className="flex flex-col items-center justify-center gap-3 py-12 text-center">
            <Snowflake className="h-10 w-10 text-muted-foreground" />
            <div className="text-sm font-medium text-foreground">No reefer slots provisioned</div>
            <div className="max-w-sm text-xs text-muted-foreground">
              Seed a demo bank of powered reefer slots to populate the availability board.
            </div>
            <button
              type="button"
              disabled={seedM.isPending}
              onClick={() => seedM.mutate()}
              className="mt-1 inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              <Snowflake className="h-4 w-4" />
              {seedM.isPending ? "Seeding…" : "Seed reefer slots"}
            </button>
            {seedM.isError && (
              <div className="text-xs text-red-600">Seeding failed. Try again.</div>
            )}
          </Card>
        </div>
      ) : (
        <>
          {/* Allocate form */}
          <div className="px-4 pt-3">
            <Card className="p-3">
              <h2 className="mb-2 text-sm font-semibold text-foreground">Allocate reefer slot</h2>
              <form
                onSubmit={submitAllocate}
                className="flex flex-col gap-2 sm:flex-row sm:items-end"
              >
                <label className="flex flex-1 flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">Container number</span>
                  <input
                    value={containerNumber}
                    onChange={(e) => setContainerNumber(e.target.value.toUpperCase())}
                    placeholder="MSKU1234567"
                    className="rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground outline-none focus:border-primary"
                  />
                </label>
                <label className="flex w-full flex-col gap-1 text-xs sm:w-40">
                  <span className="text-muted-foreground">Set temperature (°C)</span>
                  <input
                    type="number"
                    value={setTemp}
                    onChange={(e) => setSetTemp(e.target.value)}
                    className="rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground tabular-nums outline-none focus:border-primary"
                  />
                </label>
                <button
                  type="submit"
                  disabled={allocateM.isPending || !containerNumber.trim()}
                  className="inline-flex items-center justify-center gap-1.5 rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
                >
                  <Zap className="h-4 w-4" />
                  {allocateM.isPending ? "Allocating…" : "Allocate"}
                </button>
              </form>
              {allocateM.isError && (
                <div className="mt-2 text-xs text-red-600">
                  Allocation failed — no free powered slot, or invalid container.
                </div>
              )}
              {allocateM.isSuccess && (
                <div className="mt-2 text-xs text-emerald-600">
                  Allocated to slot {(allocateM.data as any)?.slot_code ?? "—"}.
                </div>
              )}
            </Card>
          </div>

          {/* Slot grid */}
          <div className="px-4 py-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h2 className="mr-auto text-sm font-semibold text-foreground">
                Slot board
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  ({slotsQ.data?.count ?? slots.length})
                </span>
              </h2>
              <select
                value={facilityFilter}
                onChange={(e) => setFacilityFilter(e.target.value)}
                className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground"
              >
                <option value="">All facilities</option>
                {facilityOptions.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </select>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground"
              >
                <option value="">All statuses</option>
                <option value="AVAILABLE">Available</option>
                <option value="OCCUPIED">Occupied</option>
                <option value="RESERVED">Reserved</option>
                <option value="FAULT">Fault</option>
              </select>
            </div>

            <Card className="p-3">
              {slotsQ.isLoading ? (
                <div className="py-10 text-center text-sm text-muted-foreground">Loading slots…</div>
              ) : slots.length === 0 ? (
                <div className="py-10 text-center text-sm text-muted-foreground">
                  No slots match the current filter.
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
                  {slots.map((s) => (
                    <SlotTile
                      key={s.id ?? s.slot_code}
                      slot={s}
                      releasing={releaseM.isPending && releaseM.variables === s.slot_code}
                      onRelease={(code) => releaseM.mutate(code)}
                    />
                  ))}
                </div>
              )}
            </Card>
          </div>
        </>
      )}
    </PageContainer>
  );
}
