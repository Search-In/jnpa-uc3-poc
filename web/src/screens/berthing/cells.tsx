// Berthing Reports — reusable cell renderers (module 7, presentation only).
//
// One place that turns a vessel-call row + a field name into rendered table content, so
// the Vessel List, the Timeline dialog and any future berthing surface explain a blank
// the same way. All semantics live in @/lib/berthing; this file only draws them.
//
// Renders nothing new from the network and calls no API — it consumes the row the
// backend already returned.

import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

import { StatusChip, TONE_COLOUR } from "@/components/ui/dtccc";
import {
  arrivalWatch,
  callAnomalies,
  classifyField,
  fmtTs,
  type BerthingField,
  type BerthingRow,
} from "@/lib/berthing";

/** The muted placeholder used for every "no value, and here's why" case. */
function Placeholder({ label, hint, tone }: { label: string; hint: string; tone: string }) {
  return (
    <span
      title={hint}
      className="cursor-help text-[11.5px] italic text-muted-foreground/80 underline decoration-dotted underline-offset-2"
      style={tone === "critical" ? { color: TONE_COLOUR.critical, fontStyle: "normal" } : undefined}
    >
      {label}
    </span>
  );
}

/**
 * Render one field of one vessel call.
 *
 * A present value renders through `format` (defaults to the raw string). A blank renders
 * as "Pending" / "Not allocated" / "Not reported" / "Anomaly" according to
 * `classifyField`, always with a tooltip stating why.
 */
export function FieldCell({
  row,
  field,
  format = (v) => String(v),
}: {
  row: BerthingRow;
  field: BerthingField;
  format?: (value: string) => ReactNode;
}) {
  const verdict = classifyField(row, field);

  if (verdict.state === "value") {
    return <>{format(String(row[field]))}</>;
  }

  if (verdict.state === "anomaly") {
    const raw = row[field];
    return (
      <span className="inline-flex items-center gap-1" title={verdict.hint}>
        <AlertTriangle size={12} style={{ color: TONE_COLOUR.critical }} aria-hidden />
        {raw ? (
          <span style={{ color: TONE_COLOUR.critical }}>{format(String(raw))}</span>
        ) : (
          <span style={{ color: TONE_COLOUR.critical }} className="text-[11.5px] font-medium">
            Anomaly
          </span>
        )}
      </span>
    );
  }

  return <Placeholder label={verdict.label} hint={verdict.hint} tone={verdict.tone} />;
}

/** Convenience wrapper for the timestamp columns. */
export function TimeCell({ row, field }: { row: BerthingRow; field: BerthingField }) {
  return <FieldCell row={row} field={field} format={(v) => fmtTs(v)} />;
}

/**
 * Row-level indicators shown beside the vessel name: one badge summarising any business
 * -rule violations, plus the graduated arrival-freshness chip for a still-expected call
 * whose ETA has passed.
 */
export function CallFlags({ row, now }: { row: BerthingRow; now?: number }) {
  const anomalies = callAnomalies(row);
  const watch = arrivalWatch(row, now);
  if (anomalies.length === 0 && !watch) return null;

  return (
    <span className="ml-1.5 inline-flex items-center gap-1 align-middle">
      {anomalies.length > 0 && (
        <span
          title={anomalies.map((a) => `• ${a.message}`).join("\n")}
          className="inline-flex cursor-help items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-semibold"
          style={{
            backgroundColor: `${TONE_COLOUR.critical}1f`,
            color: TONE_COLOUR.critical,
          }}
        >
          <AlertTriangle size={10} aria-hidden />
          {anomalies.length > 1 ? `${anomalies.length} anomalies` : "Anomaly"}
        </span>
      )}
      {watch && (
        <span title={watch.hint} className="cursor-help">
          <StatusChip label={watch.label} tone={watch.tone} />
        </span>
      )}
    </span>
  );
}
