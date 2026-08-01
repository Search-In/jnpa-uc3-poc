// UC-3 Job Assignment — the CREATE step that opens a container lifecycle.
//
// The lifecycle console below this panel operates on jobs that already exist;
// this is where one is raised: pick a container, a truck and (optionally) a
// driver, dry-run the pre-conditions, then assign.
//
// Two deliberate behaviours:
//
//   * Driver is OPTIONAL. The backend only runs the PDP-permit chain when a
//     driver is supplied (services/container_job/service.py), so a truck can be
//     dispatched while a driver's permit is being renewed. Drivers whose permit
//     cannot clear are still listed, flagged, and selectable — the operator sees
//     the reason from Validate rather than an empty dropdown.
//   * The two master lists sit under a STRICTER RBAC policy than /api/jobs
//     itself (CUSTOMS + DTCCC_ADMIN vs CONTROL_ROOM + CUSTOMS), so a 403 on a
//     dropdown is a permission fact, not an outage, and is reported as such.
//
// Composes the DTCCC kit only (Card / Button / FilterSelect / SearchInput /
// StatusChip / LoadingState / ErrorState / EmptyState) with semantic theme
// tokens — no design system of its own, matching Uc3Lifecycle.tsx.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ShieldAlert, Truck, UserCheck, XCircle } from "lucide-react";

import { FilterSelect, StatusChip } from "@/components/ui/dtccc";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/misc";

import { api, apiError } from "../../lib/api";
import type { ContainerJob, JobAssignInput, JobCheck } from "../../lib/api";

// The move types the backend accepts (services/container_job/service.py MOVE_TYPES).
const MOVE_TYPES = [
  { value: "IMPORT_PICK", label: "Import pick" },
  { value: "EXPORT_DROP", label: "Export drop" },
  { value: "EMPTY_PICK", label: "Empty pick" },
  { value: "EMPTY_DROP", label: "Empty drop" },
] as const;

const NONE = "";

/** A check row rendered from either a success payload or a rejection. */
type CheckRow = { key: string; ok: boolean; label: string; detail: string };

function toRows(checks: JobCheck[]): CheckRow[] {
  return checks.map((c, i) => ({
    key: `${c.check}-${i}`,
    ok: c.ok,
    label: c.check.replace(/_/g, " "),
    detail: c.detail,
  }));
}

export default function JobAssignPanel({
  onAssigned,
  defaultContainer = "",
}: {
  /** Called with the new job id so the parent can select it and open the stepper. */
  onAssigned?: (jobId: number) => void;
  defaultContainer?: string;
}) {
  const qc = useQueryClient();

  const [container, setContainer] = useState(defaultContainer);
  const [vehicleId, setVehicleId] = useState(NONE);
  const [driverId, setDriverId] = useState(NONE);
  const [moveType, setMoveType] = useState<string>(MOVE_TYPES[0].value);
  const [rows, setRows] = useState<CheckRow[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const vehiclesQ = useQuery({
    queryKey: ["uc3-available-vehicles"],
    queryFn: () => api.availableVehicles(undefined, 200),
  });
  const driversQ = useQuery({
    queryKey: ["uc3-active-drivers"],
    queryFn: () => api.activeDrivers(),
  });
  // /api/vehicles/available excludes trucks held by a driver enrollment, but NOT
  // trucks already holding an open job — the backend rejects those with
  // `vehicle_already_assigned`. Cross-filter here so a guaranteed-to-fail truck
  // is never offered in the first place.
  const openJobsQ = useQuery({
    queryKey: ["uc3-jobs", "open-for-assign"],
    queryFn: () => api.jobs({ open_only: true, limit: 200 }),
  });

  const busyVehicles = useMemo(
    () =>
      new Set(
        (openJobsQ.data?.items ?? [])
          .map((j) => j.vehicle_id)
          .filter((v): v is string => Boolean(v)),
      ),
    [openJobsQ.data],
  );

  const allVehicles = vehiclesQ.data?.vehicles ?? [];
  const vehicles = allVehicles.filter((v) => !busyVehicles.has(v.vehicle_id));
  const busyCount = allVehicles.length - vehicles.length;
  const drivers = useMemo(() => driversQ.data?.drivers ?? [], [driversQ.data]);

  const cn = container.trim().toUpperCase();
  const ready = cn.length > 0 && vehicleId !== NONE;

  const payload = (): JobAssignInput => ({
    container_number: cn,
    vehicle_id: vehicleId,
    // Omitted entirely when unset — sending "" would fail the driver lookup.
    ...(driverId !== NONE ? { driver_id: driverId } : {}),
    move_type: moveType,
  });

  /** Both actions share the same rejection shape, so they share the handler. */
  const onReject = (err: unknown) => {
    const e = apiError(err);
    setFailure(e.detail);
    setRows([
      {
        key: "rejected",
        ok: false,
        label: (e.code ?? "rejected").replace(/_/g, " "),
        detail: e.detail,
      },
    ]);
  };

  const validate = useMutation({
    mutationFn: () => api.jobValidate(payload()),
    onMutate: () => {
      setFailure(null);
      setRows(null);
    },
    onSuccess: (res) => setRows(toRows(res.checks ?? [])),
    onError: onReject,
  });

  const assign = useMutation({
    mutationFn: () => api.jobAssign(payload()),
    onMutate: () => {
      setFailure(null);
      setRows(null);
    },
    onSuccess: (res: { job: ContainerJob; checks: JobCheck[] }) => {
      setRows(toRows(res.checks ?? []));
      // Refresh the list this panel sits above, then hand the new job to the
      // stepper so the operator continues straight into Accept.
      qc.invalidateQueries({ queryKey: ["uc3-jobs"] });
      qc.invalidateQueries({ queryKey: ["uc3-available-vehicles"] });
      onAssigned?.(res.job.id);
      setContainer("");
      setVehicleId(NONE);
      setDriverId(NONE);
    },
    onError: onReject,
  });

  const busy = validate.isPending || assign.isPending;

  const vehicleOptions = useMemo(
    () => [
      { value: NONE, label: vehicles.length ? "Select a truck…" : "No trucks available" },
      ...vehicles.map((v) => ({
        value: v.vehicle_id,
        label: v.plate ? `${v.plate} — ${v.vehicle_id}` : v.vehicle_id,
      })),
    ],
    [vehicles],
  );

  const driverOptions = useMemo(
    () => [
      { value: NONE, label: "No driver (optional)" },
      ...drivers.map((d) => ({
        value: d.driver_id,
        // A driver with no licence on file cannot clear the PDP chain; say so in
        // the option rather than letting Validate be the first hint.
        label: `${d.name ?? d.driver_id}${d.license_no ? ` — ${d.license_no}` : " — no licence on file"}`,
      })),
    ],
    [drivers],
  );

  // A 403 here means the signed-in role may raise jobs but not read the masters.
  const mastersForbidden =
    apiError(vehiclesQ.error).status === 403 || apiError(driversQ.error).status === 403;

  return (
    <Card>
      <CardHeader className="flex-row items-center gap-2 border-b border-border">
        <Truck className="h-4 w-4 text-muted-foreground" aria-hidden />
        <CardTitle>Assign a Job</CardTitle>
        <span className="ml-auto text-xs text-muted-foreground">
          Container → Vehicle → Driver → Validate → Assign
        </span>
      </CardHeader>

      <CardContent className="space-y-4 pt-4">
        {mastersForbidden ? (
          <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-3">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" aria-hidden />
            <div className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">
                Your role cannot read the vehicle / driver masters.
              </span>{" "}
              Assigning a job needs CONTROL_ROOM or CUSTOMS, but these dropdowns are restricted to
              CUSTOMS and DTCCC_ADMIN. Sign in with one of those to assign.
            </div>
          </div>
        ) : null}

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {/* ------------------------------------------------------ container */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Container</span>
            <input
              value={container}
              onChange={(e) => setContainer(e.target.value)}
              placeholder="e.g. MSCU1234566"
              spellCheck={false}
              autoComplete="off"
              className="h-9 rounded-md border border-border bg-background px-2 text-[13px] font-medium uppercase text-foreground outline-none transition-colors placeholder:normal-case placeholder:text-muted-foreground focus:ring-2 focus:ring-primary/20"
            />
          </label>

          {/* -------------------------------------------------------- vehicle */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">
              Vehicle{" "}
              {vehiclesQ.isSuccess ? (
                <span className="text-muted-foreground/70">
                  ({vehicles.length} available
                  {busyCount > 0 ? `, ${busyCount} on an open job` : ""})
                </span>
              ) : null}
            </span>
            {vehiclesQ.isLoading ? (
              <LoadingState label="Loading trucks…" />
            ) : vehiclesQ.isError && !mastersForbidden ? (
              <ErrorState
                onRetry={() => vehiclesQ.refetch()}
                detail={apiError(vehiclesQ.error).detail}
              />
            ) : (
              <FilterSelect
                label="Vehicle"
                value={vehicleId}
                onChange={setVehicleId}
                options={vehicleOptions}
              />
            )}
          </label>

          {/* --------------------------------------------------------- driver */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Driver (optional)</span>
            {driversQ.isLoading ? (
              <LoadingState label="Loading drivers…" />
            ) : driversQ.isError && !mastersForbidden ? (
              <ErrorState
                onRetry={() => driversQ.refetch()}
                detail={apiError(driversQ.error).detail}
              />
            ) : (
              <FilterSelect
                label="Driver"
                value={driverId}
                onChange={setDriverId}
                options={driverOptions}
              />
            )}
          </label>

          {/* ------------------------------------------------------ move type */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Move type</span>
            <FilterSelect
              label="Move type"
              value={moveType}
              onChange={setMoveType}
              options={MOVE_TYPES.map((m) => ({ value: m.value, label: m.label }))}
            />
          </label>
        </div>

        {/* ------------------------------------------------------------ actions */}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={!ready || busy}
            onClick={() => validate.mutate()}
          >
            <UserCheck className="h-3.5 w-3.5" />
            {validate.isPending ? "Validating…" : "Validate"}
          </Button>
          <Button size="sm" disabled={!ready || busy} onClick={() => assign.mutate()}>
            <Truck className="h-3.5 w-3.5" />
            {assign.isPending ? "Assigning…" : "Assign Job"}
          </Button>

          {driverId === NONE ? (
            <StatusChip label="No driver — PDP check skipped" tone="neutral" />
          ) : null}
          {!ready ? (
            <span className="text-xs text-muted-foreground">
              Enter a container and pick a vehicle to continue.
            </span>
          ) : null}
        </div>

        {/* ------------------------------------------------------- check results */}
        {rows === null ? null : rows.length === 0 ? (
          <EmptyState>No checks were returned.</EmptyState>
        ) : (
          <div
            role="status"
            className="divide-y divide-border rounded-md border border-border bg-background"
          >
            {failure ? (
              <div className="flex items-center gap-2 px-3 py-2 text-xs">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-500" aria-hidden />
                <span className="font-medium text-foreground">Assignment refused</span>
              </div>
            ) : null}
            {rows.map((r) => (
              <div key={r.key} className="flex items-start gap-2 px-3 py-2">
                {r.ok ? (
                  <CheckCircle2
                    className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600"
                    aria-hidden
                  />
                ) : (
                  <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden />
                )}
                <span className="text-xs font-medium capitalize text-foreground">{r.label}</span>
                <span className="ml-auto text-right text-xs text-muted-foreground">{r.detail}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
