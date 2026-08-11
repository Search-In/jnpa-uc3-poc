/**
 * The four-beat answer.
 *
 * A what-if result has to be readable by someone who has never run a port, and
 * auditable by someone who will check every figure against the Notice. Those pull
 * in opposite directions, so the panel is ordered rather than balanced:
 *
 *   1. VERDICT      one plain sentence. What happens.
 *   2. PICTURE      one chart. Nothing competing with it.
 *   3. WHAT TO DO   at most three actions.
 *   4. THE WORKING  method, assumptions, query trace, all figures — collapsed.
 *
 * Beats 1-3 are for the person deciding. Beat 4 is for the person checking, and
 * it is one click away rather than absent: JNPA Notice §1 requires the method,
 * the assumptions *stated separately from the result*, and the queries used.
 *
 * On provenance chips: MEASURED and DERIVED are deliberately unmarked. Badging
 * every number turns the screen into a wall of chips and stops anyone reading any
 * of them; only ASSUMED and PARAMETER change how far a reader should trust a
 * figure, so only those two are shown.
 *
 * SHARED FILE — canonical copy in jnpa-uc3-poc. UC-1 and UC-2 hold copies so all
 * three dashboards present an answer identically. Styling is driven by the
 * `classNames` prop so each app keeps its own look without forking the structure.
 */
import { useState, type ReactNode } from "react";
import { coverageNotice, shouldChip, verdictFor, type SimAssumptionSource } from "./verdict";

export interface AnswerAssumption {
  field: string;
  value: unknown;
  reason: string;
  source: SimAssumptionSource;
}

export interface AnswerQuery {
  purpose: string;
  sql: string;
  params: Record<string, unknown>;
  api?: string;
  row_count?: number;
  error?: string;
}

export interface AnswerResult {
  scenario: string;
  method: string;
  result: Record<string, any>;
  // Booleans are part of this contract: channel-closure reports
  // `berth_lock_reached` and modal-shift `gate_absorbs_load` as figures.
  figures: Record<string, number | string | boolean | null>;
  assumptions: AnswerAssumption[];
  queries: AnswerQuery[];
  recommendations: Array<{ action: string; reason: string; [k: string]: unknown }>;
  data_available: boolean;
  notes: string[];
}

export interface WhatIfAnswerProps {
  result: AnswerResult;
  /** The chart. Passed in so each app draws with its own library. */
  chart?: ReactNode;
  /** Human label for the scenario, e.g. "I-A — Vessel Bunching". */
  title?: string;
  /**
   * Beat 4. Defaults to this component's own rendering of method, assumptions,
   * figures and query trace. An app that already has richer panels for those
   * passes them here instead, so the reading ORDER is shared without forcing a
   * single presentation on three dashboards.
   */
  evidence?: ReactNode;
  /** Per-app class hooks; every one is optional. */
  classNames?: Partial<Record<
    "root" | "verdict" | "headline" | "detail" | "banner" | "section" |
    "actions" | "action" | "evidence" | "summary" | "table" | "chip", string>>;
}

/** Turn `peak_queue_with_outage` into `Peak queue with outage`. */
function humanise(key: string): string {
  const s = key.replace(/_/g, " ").replace(/\bpct\b/g, "%").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "not reported";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)));
  }
  return String(v);
}

export default function WhatIfAnswer({
  result, chart, title, evidence, classNames = {},
}: WhatIfAnswerProps): JSX.Element {
  const [openEvidence, setOpenEvidence] = useState(false);
  const verdict = verdictFor(result);
  const coverage = coverageNotice(result.result);
  const cx = (k: keyof NonNullable<WhatIfAnswerProps["classNames"]>) => classNames[k] ?? "";

  const failedQueries = result.queries.filter((q) => q.error);
  const declared = result.assumptions.filter((a) => shouldChip(a.source));

  return (
    <section className={cx("root")} data-scenario={result.scenario}
             data-tone={verdict.tone} aria-label={title ?? result.scenario}>

      {/* 1 — the verdict */}
      <div className={cx("verdict")} data-tone={verdict.tone}>
        {title ? <p className={cx("detail")}>{title}</p> : null}
        <p className={cx("headline")} role="status">{verdict.headline}</p>
        {verdict.detail ? <p className={cx("detail")}>{verdict.detail}</p> : null}
      </div>

      {/* The coverage banner sits under the verdict, not in a footnote: a figure
          for a day beyond the data must not be read as a measurement. */}
      {coverage ? (
        <p className={cx("banner")} data-kind="projected">{coverage}</p>
      ) : null}

      {/* A failed query is not an empty result. Say so before anything else is
          read, because the two are indistinguishable by row count. */}
      {failedQueries.length > 0 ? (
        <p className={cx("banner")} data-kind="error">
          {failedQueries.length === 1 ? "A query" : `${failedQueries.length} queries`}{" "}
          behind this answer failed to run. The figures below are incomplete — this
          is not "no data for this period".
        </p>
      ) : null}

      {/* 2 — the picture */}
      {result.data_available && chart ? (
        <div className={cx("section")}>{chart}</div>
      ) : null}

      {/* 3 — what to do */}
      {result.data_available && result.recommendations.length > 0 ? (
        <ul className={cx("actions")}>
          {result.recommendations.slice(0, 3).map((r, i) => (
            <li key={`${r.action}-${i}`} className={cx("action")}>
              <strong>{humanise(r.action)}</strong>
              <span> — {r.reason}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {/* 4 — the working, closed by default */}
      <details className={cx("evidence")} open={openEvidence}
               onToggle={(e) => setOpenEvidence((e.target as HTMLDetailsElement).open)}>
        <summary className={cx("summary")}>
          Show the working — method, {result.assumptions.length} assumption
          {result.assumptions.length === 1 ? "" : "s"}, {result.queries.length} quer
          {result.queries.length === 1 ? "y" : "ies"}
          {declared.length > 0 ? `, ${declared.length} declared` : ""}
        </summary>

        <div className={cx("section")}>
          <h4>How this was worked out</h4>
          <p>{result.method}</p>
        </div>

        {evidence ?? (
        <>
        {/* Default evidence rendering, used when the host app has not supplied
            its own panels. */}

        {result.assumptions.length > 0 ? (
          <div className={cx("section")}>
            <h4>What we assumed</h4>
            <table className={cx("table")}>
              <thead>
                <tr><th scope="col">Input</th><th scope="col">Value</th><th scope="col">Why</th></tr>
              </thead>
              <tbody>
                {result.assumptions.map((a, i) => (
                  <tr key={`${a.field}-${i}`}>
                    <td>
                      {humanise(a.field)}
                      {shouldChip(a.source) ? (
                        <span className={cx("chip")} data-source={a.source}>
                          {a.source === "ASSUMED" ? "assumed" : "you set this"}
                        </span>
                      ) : null}
                    </td>
                    <td>{formatValue(a.value)}</td>
                    <td>{a.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {Object.keys(result.figures).length > 0 ? (
          <div className={cx("section")}>
            <h4>Every figure</h4>
            <table className={cx("table")}>
              <tbody>
                {Object.entries(result.figures).map(([k, v]) => (
                  <tr key={k}><th scope="row">{humanise(k)}</th><td>{formatValue(v)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {result.queries.length > 0 ? (
          <div className={cx("section")}>
            <h4>Where the data came from</h4>
            {result.queries.map((q, i) => (
              <details key={`${q.purpose}-${i}`} open={Boolean(q.error)}>
                <summary>
                  {q.purpose}
                  {q.row_count !== undefined ? ` — ${q.row_count} rows` : ""}
                  {q.error ? " — FAILED" : ""}
                </summary>
                {q.api ? <p><code>{q.api}</code></p> : null}
                {q.error ? <p role="alert">{q.error}</p> : null}
                <pre>{q.sql}</pre>
                {Object.keys(q.params).length > 0 ? (
                  <pre>{JSON.stringify(q.params, null, 1)}</pre>
                ) : null}
              </details>
            ))}
          </div>
        ) : null}

        {result.notes.length > 0 ? (
          <div className={cx("section")}>
            <h4>Notes</h4>
            <ul>{result.notes.map((nt, i) => <li key={i}>{nt}</li>)}</ul>
          </div>
        ) : null}
        </>
        )}
      </details>
    </section>
  );
}
