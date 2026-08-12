// UC-3 Job Assignment — the CREATE step that opens a container lifecycle.
//
// The lifecycle console below this panel operates on jobs that already exist;
// this is where one is raised: pick a container, a truck and (optionally) a
// driver, dry-run the pre-conditions, then assign.
//
// Two deliberate behaviours:
//
//   * Driver is DERIVED FROM THE TRUCK, and REQUIRED for the move types in
//     DRIVER_REQUIRED_MOVE_TYPES (BUG-4). Selecting a vehicle auto-selects the
//     driver bound to it in core.driver_identity, which /api/vehicles/available
//     now returns alongside each row — previously the driver was a free-standing
//     optional dropdown and every job was created with driver_id = NULL, leaving
//     the driver PWA with nobody to notify. The operator may still override the
//     selection; for the remaining move types the driver stays optional so a
//     truck can be dispatched while a permit is being renewed. Drivers whose
//     permit cannot clear are still listed, flagged, and selectable — the
//     operator sees the reason from Validate rather than an empty dropdown.
//   * The two master lists sit under a STRICTER RBAC policy than /api/jobs
//     itself (CUSTOMS + DTCCC_ADMIN vs CONTROL_ROOM + CUSTOMS), so a 403 on a
//     dropdown is a permission fact, not an outage, and is reported as such.
//
// Composes the DTCCC kit only (Card / Button / FilterSelect / SearchInput /
// StatusChip / LoadingState / ErrorState / EmptyState) with semantic theme
// tokens — no design system of its own, matching Uc3Lifecycle.tsx.

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ShieldAlert, Truck, UserCheck, XCircle } from "lucide-react";

import { FilterSelect, SearchInput, StatusChip } from "@/components/ui/dtccc";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/misc";

import { api, apiError } from "../../lib/api";
import { customsBlock, type CustomsBlock } from "../../lib/customs";
import type { ContainerJob, JobAssignInput, JobCheck } from "../../lib/api";
import { assignableCount, vehicleLabel } from "../../lib/vehicles";
import {
  autoSelect,
  boundDriverFor,
  dedupeBy,
  driverIdentity,
  vehicleIdentity,
} from "../../lib/assign";

// The move types the backend accepts (services/container_job/service.py MOVE_TYPES).
const MOVE_TYPES = [
  { value: "IMPORT_PICK", label: "Import pick" },
  { value: "EXPORT_DROP", label: "Export drop" },
  { value: "EMPTY_PICK", label: "Empty pick" },
  { value: "EMPTY_DROP", label: "Empty drop" },
] as const;

const NONE = "";

// Mirrors services/container_job/service.py DRIVER_REQUIRED_MOVE_TYPES. A laden
// box leaving the terminal must be attributable to a person, so the backend
// rejects these move types with `driver_required` when driver_id is absent;
// gating the button here turns that 400 into an inline hint instead.
const DRIVER_REQUIRED_MOVE_TYPES = new Set<string>(["IMPORT_PICK"]);

/** Debounce the search boxes so a keystroke is not a request. */
function useDebounced<T>(value: T, delay = 320): T {
  const [d, setD] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setD(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return d;
}

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
  // UX: clicking a container in the Container Jobs list below pre-fills this
  // form. `defaultContainer` used to seed useState only, so it applied on the
  // FIRST render and never again — every later click left the operator retyping
  // the number they had just clicked. Syncing on change is what makes the click
  // do anything. It pre-fills only: nothing is submitted (see `assign`, which
  // runs from the button alone).
  useEffect(() => {
    if (defaultContainer) setContainer(defaultContainer);
  }, [defaultContainer]);
  const [vehicleId, setVehicleId] = useState(NONE);
  const [driverId, setDriverId] = useState(NONE);
  const [moveType, setMoveType] = useState<string>(MOVE_TYPES[0].value);
  const [rows, setRows] = useState<CheckRow[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  // Set from a `customs_flagged` rejection. The backend stays the enforcement
  // point; this only stops the operator re-submitting an assignment customs has
  // already refused, and shows the reason customs actually recorded.
  const [customs, setCustoms] = useState<CustomsBlock | null>(null);

  // Search is a QUERY PARAMETER of the availability endpoint, never a filter
  // applied to a wider list: `q` is applied inside the same WHERE as the
  // occupancy rule server-side, so searching for a truck that is on a job
  // returns nothing at all. Fetching the master and filtering it here — the
  // obvious alternative — would put occupied trucks back in reach of the
  // operator, which is the whole bug.
  const [vehicleSearch, setVehicleSearch] = useState("");
  const [driverSearch, setDriverSearch] = useState("");
  const vehicleQ = useDebounced(vehicleSearch.trim());
  const driverQ = useDebounced(driverSearch.trim());

  const vehiclesQ = useQuery({
    queryKey: ["uc3-available-vehicles", vehicleQ],
    queryFn: () => api.availableVehicles(vehicleQ || undefined, 200),
  });
  // Availability is decided by the DATABASE, not here: /api/identity/drivers/available
  // returns only ACTIVE drivers with no open container job. An occupied driver is
  // never delivered to this component, so there is nothing to filter out below —
  // and nothing a client-side check could get wrong.
  const driversQ = useQuery({
    queryKey: ["uc3-available-drivers", driverQ],
    queryFn: () => api.availableDrivers(driverQ || undefined, 200),
  });
  // /api/vehicles/available now excludes trucks holding an open job server-side
  // (BUG-1). This stays as a belt-and-braces cross-filter: the two queries are
  // fetched independently, so a job assigned between them would otherwise offer a
  // truck the backend is about to reject with `vehicle_already_assigned`.
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

  const allVehicles = useMemo(
    () => dedupeBy(vehiclesQ.data?.vehicles ?? [], vehicleIdentity),
    [vehiclesQ.data],
  );
  const vehicles = allVehicles.filter((v) => !busyVehicles.has(v.vehicle_id));
  const busyCount = allVehicles.length - vehicles.length;
  // The count comes from the DB (vehicles ACTIVE with no open job), not from the
  // page length — the page is capped by `limit`, and the fleet total is not the
  // available total. `busyCount` is what this client noticed became busy after
  // the dropdown was fetched, so it is subtracted rather than assumed to be 0.
  const availableCount = assignableCount(
    vehiclesQ.data?.available_total,
    vehicles.length,
    busyCount,
  );
  // One person = one option. The backend already collapses duplicate
  // core.driver_identity records for a licence; this guarantees it for the
  // rendered list too, so "AAKIL KHAN — MH01 20100095262" can never appear three
  // times. It only ever REMOVES a repeat, never admits an occupied driver.
  const drivers = useMemo(
    () => dedupeBy(driversQ.data?.drivers ?? [], driverIdentity),
    [driversQ.data],
  );

  const selectedVehicle = useMemo(
    () => vehicles.find((v) => v.vehicle_id === vehicleId),
    [vehicles, vehicleId],
  );

  // BUG-4: the driver follows the truck. /api/vehicles/available carries the
  // driver bound to each vehicle in core.driver_identity, so picking a truck
  // fills the driver in rather than leaving the operator to match them by hand
  // (which nobody did — every existing job has driver_id = NULL). Clearing the
  // truck clears the driver; the operator can still override the selection
  // afterwards, and this does not fight them because it only re-runs when the
  // selected VEHICLE changes.
  // A truck can be free while the driver bound to it is out on ANOTHER truck's
  // job (the binding is an enrollment fact, the job is not). Resolved against the
  // availability response by PERSON: the bound record may not be the record the
  // list carries for them, and matching ids alone would call a free driver busy.
  const boundDriver = useMemo(
    () => boundDriverFor(selectedVehicle, drivers),
    [selectedVehicle, drivers],
  );
  const boundDriverFree = boundDriver !== null;

  const cn = container.trim().toUpperCase();

  // UX: once a container is chosen, offer a complete assignment rather than two
  // empty dropdowns. Both values come from `autoSelect`, which picks only from
  // the availability responses — an occupied truck or driver is not in them, so
  // it cannot be auto-selected. A selection the operator has already made is
  // kept as long as it is still available. Nothing is submitted: this fills the
  // form, and the Assign button stays the only thing that posts a job.
  useEffect(() => {
    if (!cn) return;
    const next = autoSelect(vehicles, drivers, {
      vehicleId: vehicleId || undefined,
      driverId: driverId || undefined,
    });
    setVehicleId(next.vehicleId);
    setDriverId(next.driverId);
    // `vehicleId`/`driverId` are read, not tracked: re-running on our own writes
    // would fight the operator's next manual change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cn, vehicles, drivers]);
  const driverRequired = DRIVER_REQUIRED_MOVE_TYPES.has(moveType);
  // A container customs has flagged cannot be assigned; the button stays out of
  // reach until the operator changes container. `customsFor` pins the block to
  // the container it was raised for, so typing a different one clears it.
  const customsBlocked = Boolean(customs && customs.container === cn);
  const ready =
    cn.length > 0 &&
    vehicleId !== NONE &&
    (!driverRequired || driverId !== NONE) &&
    !customsBlocked;

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
    setCustoms(customsBlock(e));
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
      setCustoms(null);
      setRows(null);
    },
    onSuccess: (res) => setRows(toRows(res.checks ?? [])),
    onError: onReject,
  });

  const assign = useMutation({
    mutationFn: () => api.jobAssign(payload()),
    onMutate: () => {
      setFailure(null);
      setCustoms(null);
      setRows(null);
    },
    onSuccess: (res: { job: ContainerJob; checks: JobCheck[] }) => {
      setRows(toRows(res.checks ?? []));
      // Refresh the list this panel sits above, then hand the new job to the
      // stepper so the operator continues straight into Accept.
      qc.invalidateQueries({ queryKey: ["uc3-jobs"] });
      qc.invalidateQueries({ queryKey: ["uc3-available-vehicles"] });
      qc.invalidateQueries({ queryKey: ["uc3-available-drivers"] });
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
        // Value stays the Vehicle ID the assign API takes; the label is the
        // registration the yard identifies the truck by.
        value: v.vehicle_id,
        label: vehicleLabel(v),
      })),
    ],
    [vehicles],
  );

  const driverOptions = useMemo(() => {
    const opts = [
      {
        value: NONE,
        label: driverRequired ? "Select a driver…" : "No driver (optional)",
      },
      ...drivers.map((d) => ({
        value: d.driver_id,
        // A driver with no licence on file cannot clear the PDP chain; say so in
        // the option rather than letting Validate be the first hint.
        label: `${d.name ?? d.driver_id}${d.license_no ? ` — ${d.license_no}` : " — no licence on file"}`,
      })),
    ];
    // Nothing is spliced in for the truck's bound driver. The panel used to
    // re-admit them here when the page had not reached them, which is the seam an
    // occupied driver got back through; `boundDriverFor` now resolves the binding
    // WITHIN this same list (by licence, so the person's other record counts), so
    // a bound driver who is free is already an option and one who is out on a job
    // has no way back into the dropdown.
    return opts;
  }, [drivers, driverRequired]);

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
                  ({availableCount} available
                  {busyCount > 0 ? `, ${busyCount} on an open job` : ""})
                </span>
              ) : null}
            </span>
            <SearchInput
              value={vehicleSearch}
              onChange={setVehicleSearch}
              placeholder="Search registration / Vehicle ID…"
            />
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
            {vehiclesQ.isSuccess && vehicles.length === 0 ? (
              <span className="text-xs text-amber-600 dark:text-amber-500">
                {vehicleQ
                  ? `No available vehicle matches “${vehicleQ}” — a truck on an open job is not
                     offered, however it is searched for.`
                  : `No available vehicles — every ACTIVE truck is on an open job. One frees up
                     when its job is completed or cancelled.`}
              </span>
            ) : null}
          </label>

          {/* --------------------------------------------------------- driver */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">
              {driverRequired ? "Driver (required)" : "Driver (optional)"}{" "}
              {driversQ.isSuccess ? (
                <span className="text-muted-foreground/70">
                  ({driversQ.data.available_total} available)
                </span>
              ) : null}
            </span>
            <SearchInput
              value={driverSearch}
              onChange={setDriverSearch}
              placeholder="Search name / licence / Driver ID…"
            />
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
            {driversQ.isSuccess && drivers.length === 0 ? (
              <span className="text-xs text-amber-600 dark:text-amber-500">
                {driverQ
                  ? `No available driver matches “${driverQ}” — a driver on an open job is not
                     offered, however they are searched for.`
                  : `No available drivers — every ACTIVE driver is on an open job. One frees up
                     when their job is completed or cancelled.`}
              </span>
            ) : null}
            {/* Say WHY the driver is filled in / missing, so an operator never has
                to guess whether the truck has a driver on file. */}
            {vehicleId !== NONE && selectedVehicle?.driver_id && boundDriverFree ? (
              <span className="text-xs text-muted-foreground">
                Auto-selected from {vehicleLabel(selectedVehicle)}
              </span>
            ) : vehicleId !== NONE && selectedVehicle?.driver_id ? (
              <span className="text-xs text-amber-600 dark:text-amber-500">
                {selectedVehicle.driver_name ?? selectedVehicle.driver_id} is assigned to this truck
                but is already on an open job — pick another driver.
              </span>
            ) : vehicleId !== NONE && driverRequired ? (
              <span className="text-xs text-amber-600 dark:text-amber-500">
                No driver is assigned to this truck — pick one, or assign a driver to the vehicle in
                Driver Management first.
              </span>
            ) : null}
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
            {customs ? (
              <div
                role="alert"
                className="flex flex-col gap-1 border-l-2 border-destructive px-3 py-2 text-xs"
              >
                <div className="flex items-center gap-2">
                  <ShieldAlert className="h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden />
                  <span className="font-medium text-foreground">{customs.reason}</span>
                </div>
                {/* Reason, explanation and note all come from the refusal itself,
                    so the panel cannot drift from what the backend enforced. Only
                    what customs recorded — no note is invented when absent. */}
                <span className="pl-5 text-muted-foreground">{customs.message}</span>
                {customs.note ? (
                  <span className="pl-5 text-muted-foreground">Customs note: {customs.note}</span>
                ) : null}
              </div>
            ) : failure ? (
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
