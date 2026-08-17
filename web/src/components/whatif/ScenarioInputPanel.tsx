// Scenario input panel — the left column of the Cargo What-If Dashboard.
//
// One form per scenario, with every default preloaded to the value the JNPA
// Notice (05 Aug 2026) actually states: a 6-hour berth overrun, a 20% modal
// shift, a 25% productivity cut, a one-third trip reduction, a 48-hour horizon,
// and the Notice's own dates. An operator can therefore run the briefed scenario
// without typing anything, and still override every field.
//
// Percentages are entered as PERCENT (25) and submitted as the FRACTION (0.25)
// the API takes — the conversion happens here, in one place, so no caller can
// send 25 and get a 2500% reduction.

import { useEffect, useMemo, useState } from "react";
import { usePortFocus } from "@/lib/focusStore";
import { Play, RotateCcw } from "lucide-react";
import type { SimScenarioEntry } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";

// Notice §2-4 dates. Kept as constants so the demo defaults are auditable and
// changing the briefed window is a one-line edit.
const NOTICE = {
  berthOverrun: "2026-08-02T00:00", // I-B  "On 2nd August 2026…"
  craneCall: "2026-08-06T00:00", // II-B "Take up a vessel on 6th August 2026"
  windowFrom: "2026-08-01", // II-A / III-B "1st August 2026 to 3rd August 2026"
  windowTo: "2026-08-03",
  stateOn: "2026-08-04", // III-B "show state on 4th August 2026"
  gateFrom: "2026-08-03T00:00", // III-A a full day inside the briefed window
  gateTo: "2026-08-04T00:00",
  bunchingDay: "2026-08-06T00:00", // I-A  "On 6 August 2026…"
  // N-1: mid-morning, so a 12-hour closure spans a full working day rather than
  // straddling the quiet overnight window and understating the berth-lock risk.
  closureStart: "2026-08-06T06:00",
};

export type FieldKind = "text" | "number" | "percent" | "date" | "datetime";

interface FieldSpec {
  name: string;
  label: string;
  kind: FieldKind;
  value: string;
  hint?: string;
  step?: string;
  min?: number;
  max?: number;
}

/** The form for each scenario. `percent` fields are divided by 100 on submit. */
function fieldsFor(scenario: string): FieldSpec[] {
  switch (scenario) {
    case "berth-cascade":
      return [
        {
          name: "as_of",
          label: "Overrun date/time",
          kind: "datetime",
          value: NOTICE.berthOverrun,
          hint: "Start of the cascade horizon",
        },
        {
          name: "delay_hours",
          label: "Delay (hours)",
          kind: "number",
          value: "6",
          step: "0.5",
          min: 0.5,
          max: 240,
          hint: "The operation overrun",
        },
        {
          name: "horizon_hours",
          label: "Horizon (hours)",
          kind: "number",
          value: "48",
          step: "1",
          min: 1,
          max: 336,
          hint: "Cumulative delay is reported over this window",
        },
        {
          name: "terminal",
          label: "Terminal",
          kind: "text",
          value: "",
          hint: "Blank = all terminals",
        },
        {
          name: "vessel_name",
          label: "Vessel call",
          kind: "text",
          value: "",
          hint: "Blank = the first call in the window (declared as an assumption)",
        },
      ];
    case "crane-productivity":
      return [
        { name: "as_of", label: "Day under study", kind: "datetime", value: NOTICE.craneCall },
        {
          name: "reduction_pct",
          label: "Productivity reduction (%)",
          kind: "percent",
          value: "25",
          step: "1",
          min: 1,
          max: 99,
        },
        {
          name: "window_hours",
          label: "Window (hours)",
          kind: "number",
          value: "48",
          step: "1",
          min: 1,
          max: 336,
        },
        { name: "terminal", label: "Terminal", kind: "text", value: "" },
        {
          name: "vessel_name",
          label: "Vessel",
          kind: "text",
          value: "",
          hint: "Blank = the call with the highest derivable productivity",
        },
      ];
    case "modal-shift":
      return [
        { name: "from_date", label: "From date", kind: "date", value: NOTICE.windowFrom },
        { name: "to_date", label: "To date", kind: "date", value: NOTICE.windowTo },
        {
          name: "shift_pct",
          label: "Rail → road shift (%)",
          kind: "percent",
          value: "20",
          step: "1",
          min: 1,
          max: 100,
        },
        { name: "terminal", label: "Terminal", kind: "text", value: "" },
        { name: "gate_id", label: "Gate", kind: "text", value: "" },
        {
          name: "sustained_rate",
          label: "Sustained gate rate (trucks/h)",
          kind: "number",
          value: "",
          step: "1",
          min: 1,
          hint: "Blank = derived from the data (TAS capacity, else observed p90)",
        },
      ];
    case "gate-slotting":
      return [
        { name: "from_ts", label: "From", kind: "datetime", value: NOTICE.gateFrom },
        { name: "to_ts", label: "To", kind: "datetime", value: NOTICE.gateTo },
        { name: "terminal", label: "Terminal", kind: "text", value: "" },
        { name: "gate_id", label: "Gate", kind: "text", value: "" },
        {
          name: "sustained_rate",
          label: "Sustained gate rate (trucks/h)",
          kind: "number",
          value: "",
          step: "1",
          min: 1,
          hint: "Blank = derived. Override when the slot book is unprovisioned",
        },
      ];
    case "driver-shortage":
      return [
        { name: "from_date", label: "From date", kind: "date", value: NOTICE.windowFrom },
        { name: "to_date", label: "To date", kind: "date", value: NOTICE.windowTo },
        { name: "state_date", label: "Report state on", kind: "date", value: NOTICE.stateOn },
        {
          name: "reduction_pct",
          label: "Trip reduction (%)",
          kind: "percent",
          value: "33.33",
          step: "0.01",
          min: 1,
          max: 99,
          hint: "One third, per the scenario",
        },
      ];
    case "vessel-bunching":
      return [
        {
          name: "as_of",
          label: "Study day",
          kind: "datetime",
          value: NOTICE.bunchingDay,
          hint: "The Notice names 6 August, which is beyond the data — the answer says so and projects",
        },
        {
          name: "horizon_hours",
          label: "Horizon (hours)",
          kind: "number",
          value: "24",
          step: "1",
          min: 1,
          max: 336,
        },
        {
          name: "terminal",
          label: "Terminal",
          kind: "text",
          value: "",
          hint: "Blank = the whole port",
        },
      ];

    // ------------------------------------------------ bidder-proposed (N-1..3)
    case "channel-closure":
      return [
        {
          name: "as_of",
          label: "Channel closes at",
          kind: "datetime",
          value: NOTICE.closureStart,
        },
        {
          name: "closure_hours",
          label: "Closure length (hours)",
          kind: "number",
          value: "12",
          step: "0.5",
          min: 1,
          max: 168,
        },
        {
          name: "transit_hours",
          label: "Channel transit (hours)",
          kind: "number",
          value: "1.5",
          step: "0.5",
          min: 0.5,
          max: 24,
          hint: "One way. Declared — the data carries no pilotage timing",
        },
        { name: "terminal", label: "Terminal", kind: "text", value: "" },
      ];
    case "yard-feedback":
      return [
        { name: "from_date", label: "From date", kind: "date", value: NOTICE.windowFrom },
        { name: "to_date", label: "To date", kind: "date", value: "2026-08-05" },
        {
          name: "evacuation_drop_pct",
          label: "Evacuation shortfall (%)",
          kind: "percent",
          value: "50",
          step: "1",
          min: 1,
          max: 99,
        },
        {
          name: "yard_capacity_teu",
          label: "Yard capacity (TEU)",
          kind: "number",
          value: "",
          step: "100",
          min: 1,
          hint: "Blank = scaled from observed volume, and declared. Supplying the real figure moves the tipping day",
        },
        {
          name: "horizon_days",
          label: "Horizon (days)",
          kind: "number",
          value: "14",
          step: "1",
          min: 1,
          max: 120,
        },
      ];
    case "degraded-gate":
      return [
        { name: "from_ts", label: "Window from", kind: "datetime", value: NOTICE.gateFrom },
        { name: "to_ts", label: "Window to", kind: "datetime", value: NOTICE.gateTo },
        {
          name: "outage_hours",
          label: "Outage length (hours)",
          kind: "number",
          value: "4",
          step: "0.5",
          min: 0.5,
          max: 168,
        },
        {
          name: "degraded_fraction",
          label: "Manual rate (% of normal)",
          kind: "percent",
          value: "40",
          step: "1",
          min: 1,
          max: 99,
          hint: "Declared — nothing in the data records a manual gate's throughput",
        },
        {
          name: "sustained_rate",
          label: "Gate sustained rate (/h)",
          kind: "number",
          value: "",
          step: "1",
          min: 1,
          hint: "Blank = derived, same definition III-A and II-A use",
        },
      ];
    default:
      return [];
  }
}

export function ScenarioInputPanel({
  scenario,
  entry,
  onRun,
  running,
}: {
  scenario: string;
  entry?: SimScenarioEntry;
  onRun: (body: Record<string, unknown>) => void;
  running: boolean;
}) {
  const focus = usePortFocus();
  const baseFields = useMemo(() => fieldsFor(scenario), [scenario]);

  // GAP-UI-08. A vessel picked anywhere in the estate should already be in the
  // box, rather than retyped from memory into a free-text field — retyping is
  // where "XIN HANG ZHOU" becomes "XIN HANG ZHOU " and a scenario silently runs
  // against the whole window instead of one call.
  //
  // Prefill only fills fields the scenario ALREADY declares, and only from a
  // focus that carries a value. It never invents a parameter the scenario does
  // not take, and it never overwrites a Notice default with a blank.
  const defaults = useMemo(() => {
    const fromFocus: Record<string, string | undefined> = {
      vessel_name: focus.vesselName,
      voyage_number: focus.viaNo,
      // The berth application is keyed by VCN; the field takes the same value.
      berthing_record_id: focus.vcn,
      container_no: focus.containerNo,
      from_date: focus.fromDate,
      to_date: focus.toDate,
    };
    return baseFields.map((f) => {
      const v = fromFocus[f.name];
      return v && v.trim()
        ? { ...f, value: v.trim(), hint: `From the current selection. ${f.hint ?? ""}`.trim() }
        : f;
    });
    // `nonce` so a re-selection of the SAME vessel still re-applies.
  }, [baseFields, focus.vesselName, focus.viaNo, focus.vcn, focus.containerNo,
      focus.fromDate, focus.toDate, focus.nonce]);

  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(defaults.map((f) => [f.name, f.value])),
  );

  // Switching scenario resets the form to that scenario's Notice defaults —
  // carrying a stale `delay_hours` into the modal-shift form would be worse than
  // useless, it would look like the parameter had been considered. A new focus
  // resets it the same way, for the same reason.
  useEffect(() => {
    setValues(Object.fromEntries(defaults.map((f) => [f.name, f.value])));
  }, [defaults]);

  const set = (name: string, v: string) => setValues((prev) => ({ ...prev, [name]: v }));

  const reset = () => setValues(Object.fromEntries(defaults.map((f) => [f.name, f.value])));

  const submit = () => {
    const body: Record<string, unknown> = {};
    for (const f of defaults) {
      const raw = (values[f.name] ?? "").trim();
      if (raw === "") continue; // omit → the backend applies its own default
      if (f.kind === "percent") body[f.name] = Number(raw) / 100;
      else if (f.kind === "number") body[f.name] = Number(raw);
      else body[f.name] = raw;
    }
    onRun(body);
  };

  if (!defaults.length) {
    return (
      <Card className="p-3 text-[12px] text-muted-foreground">
        No input form is defined for “{scenario}”. Use the generic endpoint.
      </Card>
    );
  }

  return (
    <Card className="flex flex-col gap-3 p-3">
      <div>
        <h3 className="text-[13px] font-semibold text-foreground">Parameters</h3>
        {entry && (
          <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">{entry.question}</p>
        )}
      </div>

      <div className="flex flex-col gap-2.5">
        {defaults.map((f) => (
          <label key={f.name} className="flex flex-col gap-1">
            <span className="text-[11px] font-medium text-muted-foreground">{f.label}</span>
            <input
              type={
                f.kind === "date"
                  ? "date"
                  : f.kind === "datetime"
                    ? "datetime-local"
                    : f.kind === "text"
                      ? "text"
                      : "number"
              }
              value={values[f.name] ?? ""}
              step={f.step}
              min={f.min}
              max={f.max}
              onChange={(e) => set(f.name, e.target.value)}
              className="h-9 rounded-md border border-border bg-background px-2 text-[13px] text-foreground outline-none transition-colors focus:ring-2 focus:ring-primary/20"
            />
            {f.hint && <span className="text-[10.5px] text-muted-foreground">{f.hint}</span>}
          </label>
        ))}
      </div>

      <div className="flex items-center gap-2 pt-0.5">
        <Button onClick={submit} disabled={running} className="flex-1 gap-1.5">
          {running ? <Spinner className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {running ? "Running…" : "Run simulation"}
        </Button>
        <Button
          variant="outline"
          onClick={reset}
          disabled={running}
          aria-label="Reset to Notice defaults"
        >
          <RotateCcw className="h-4 w-4" />
        </Button>
      </div>

      {entry && (
        <p className="border-t border-border pt-2 text-[10.5px] leading-snug text-muted-foreground">
          Reads: {entry.reads.join(", ")}
        </p>
      )}
    </Card>
  );
}
