// HeaderActions — the dashboard's notification cluster: a Material-style
// notification IconButton (with unread badge) that opens a right-side sliding
// drawer (Calcite sheet) listing active alerts.
//
// Pure UI: alert data still comes from the WebSocket + the typed adapter, and
// clicking an alert publishes to the map via the alertFocus store. No API /
// backend / GIS changes. (Reset to baseline lives in the header itself; this
// component no longer owns an overflow menu.)

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { CalciteSheet } from "@esri/calcite-components-react";
import { AlertTriangle, Bell, BellOff, ChevronRight, Search, X } from "lucide-react";
import { getAdapter } from "@/data";
import { useSocket } from "@/hooks/SocketContext";
import { useTourStore } from "@/whatif/useTourStore";
import { getScript } from "@/whatif/scenarioScripts";
import type { Alert } from "@/lib/types";
import { Spinner } from "@/components/ui/misc";
import { AlertEvidenceDialog } from "@/components/AlertEvidenceDialog";
import { alertFocusStore } from "@/lib/alertFocus";
import { alertKey, mergeAlerts } from "@/lib/alerts";
import { severityColour } from "@/lib/palette";
import { relativeAge } from "@/lib/utils";

export function HeaderActions() {
  // Alert feed = WS-live ∪ adapter seed (shared React-Query cache key, so this
  // dedupes with any other consumer). Drives both the badge and the drawer.
  const { alerts: liveAlerts } = useSocket();
  const seedQ = useQuery({
    queryKey: ["alerts-seed"],
    queryFn: () => getAdapter().alerts({ limit: 20 }),
  });
  const merged = useMemo(
    () => mergeAlerts(liveAlerts, seedQ.data ?? [], 50),
    [liveAlerts, seedQ.data],
  );

  return <NotificationBell alerts={merged} loading={seedQ.isLoading && merged.length === 0} />;
}

// --- Notification bell + sliding drawer -------------------------------------

function NotificationBell({ alerts, loading }: { alerts: Alert[]; loading: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [seen, setSeen] = useState<Set<string>>(() => new Set());
  const [evidence, setEvidence] = useState<Alert | null>(null);

  // The guided tour rings an alert card (a "dom" target like "alert-WRONG_WAY");
  // force the drawer open during those steps so the tagged element is mounted.
  const tour = useTourStore();
  const alertStepActive = useMemo(() => {
    if (!tour.scenarioId) return false;
    const tgt = getScript(tour.scenarioId)?.steps[tour.stepIndex]?.target;
    return tgt?.kind === "dom" && typeof tgt.selector === "string" && tgt.selector.startsWith("alert-");
  }, [tour.scenarioId, tour.stepIndex]);

  const expanded = open || alertStepActive;
  const unread = useMemo(() => alerts.filter((a) => !seen.has(alertKey(a))).length, [alerts, seen]);

  // Opening the drawer marks everything currently shown as read.
  useEffect(() => {
    if (expanded) setSeen(new Set(alerts.map(alertKey)));
  }, [expanded, alerts]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return alerts;
    return alerts.filter((a) =>
      [a.plate, a.kind, a.gate_id, a.payload?.zone_id, a.severity]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q)),
    );
  }, [alerts, query]);

  function onAlertClick(a: Alert) {
    // Publish to the map (LiveOperations pans/zooms + rings it), then reveal it.
    alertFocusStore.focus(a);
    setOpen(false);
    navigate("/live");
  }

  function onEvidence(a: Alert) {
    // Close the drawer first so the evidence modal isn't occluded by the sheet.
    setOpen(false);
    setEvidence(a);
  }

  return (
    <>
      {/* Material-style notification IconButton: 44px target, 22px glyph. The
          bell only OPENS the drawer — the sheet's scrim / Esc / × close it, which
          avoids the click-through open↔close flicker. */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t("notifications.title")}
        title={t("notifications.title")}
        className="relative inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-full text-foreground transition-colors hover:bg-black/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-severity-info/60 active:bg-black/10"
      >
        <Bell className="h-[22px] w-[22px]" strokeWidth={2} />
        {unread > 0 && (
          <span className="absolute right-1.5 top-1.5 flex h-[18px] min-w-[18px] items-center justify-center rounded-full border-2 border-white bg-severity-critical px-1 text-[10px] font-bold leading-none text-white">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>

      <CalciteSheet
        label={t("notifications.title")}
        open={expanded}
        position="inline-end"
        displayMode="overlay"
        widthScale="s"
        style={{ "--calcite-sheet-width": "412px" } as unknown as React.CSSProperties}
        // Keep the drawer pinned open while the guided tour is ringing an alert.
        outsideCloseDisabled={alertStepActive || undefined}
        escapeDisabled={alertStepActive || undefined}
        onCalciteSheetClose={() => setOpen(false)}
      >
        <div className="flex h-full w-full flex-col bg-muted/30 text-foreground">
          {/* Sticky header (does not scroll with the list). */}
          <div className="flex items-center justify-between gap-2 border-b border-border bg-card px-4 py-3 shadow-sm">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold tracking-tight">{t("notifications.title")}</h2>
              <span className="inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-severity-critical/10 px-1.5 text-[11px] font-bold tabular-nums text-severity-critical">
                {alerts.length}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label={t("common.close")}
              className="-mr-1 inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-black/5 hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          {/* Sticky search box. */}
          <div className="border-b border-border bg-card px-4 pb-3 pt-1">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("notifications.search")}
                className="w-full rounded-full border border-border bg-background py-2.5 pl-9 pr-3 text-sm outline-none transition-colors focus:border-severity-info focus:ring-2 focus:ring-severity-info/30"
              />
            </div>
          </div>

          {/* Scrollable list of compact alert cards. A thin overlay scrollbar
              (no reserved gutter) lets the cards fill the drawer width evenly. */}
          <ul
            className="min-h-0 flex-1 space-y-2 overflow-y-auto overflow-x-hidden overscroll-contain scroll-smooth px-4 py-3 [scrollbar-color:rgb(203_213_225)_transparent] [scrollbar-width:thin] [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-slate-300 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar]:w-1.5"
            data-testid="alerts-panel"
          >
            {loading ? (
              <li className="flex items-center justify-center gap-2 px-4 py-12 text-sm text-muted-foreground">
                <Spinner /> {t("common.loading")}
              </li>
            ) : filtered.length === 0 ? (
              <li className="flex flex-col items-center gap-2 px-4 py-12 text-center text-sm text-muted-foreground">
                <BellOff className="h-8 w-8 opacity-40" />
                {t("notifications.empty")}
              </li>
            ) : (
              filtered.map((a) => {
                const sev = severityColour(a.severity);
                const location = a.gate_id ?? (a.payload?.zone_id as string) ?? "—";
                return (
                  // Tagged by alert kind so the guided tour rings the EXACT alert.
                  // The whole card locates the alert on the map (role=button so it
                  // stays keyboard-reachable); the evidence link stops propagation.
                  <li key={alertKey(a)} data-guided-id={`alert-${a.kind}`}>
                    <div
                      role="button"
                      tabIndex={0}
                      onClick={() => onAlertClick(a)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onAlertClick(a);
                        }
                      }}
                      title={t("notifications.locate")}
                      className="group relative cursor-pointer overflow-hidden rounded-lg border border-border bg-card py-2.5 pl-4 pr-3 shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-severity-info/40"
                    >
                      {/* Left colored severity indicator. */}
                      <span
                        className="absolute inset-y-0 left-0 w-1"
                        style={{ backgroundColor: sev }}
                        aria-hidden
                      />

                      {/* Title row — severity icon + alert title, status badge right. */}
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex min-w-0 items-center gap-1.5">
                          <AlertTriangle
                            className="h-4 w-4 shrink-0"
                            style={{ color: sev }}
                            aria-hidden
                          />
                          <span className="truncate text-[15px] font-semibold leading-tight tracking-tight">
                            {t(`alertKind.${a.kind}`, { defaultValue: a.kind })}
                          </span>
                        </div>
                        {a.ack ? (
                          <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                            {t("notifications.statusAck")}
                          </span>
                        ) : (
                          <span
                            className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                            style={{ backgroundColor: `${sev}1f`, color: sev }}
                          >
                            {t("notifications.statusActive")}
                          </span>
                        )}
                      </div>

                      {/* Detail rows — compact label : value pairs. */}
                      <div className="mt-1.5 space-y-0.5 text-[13px] leading-snug">
                        <div className="flex items-baseline gap-1.5">
                          <span className="w-14 shrink-0 text-muted-foreground">
                            {t("notifications.vehicle")}
                          </span>
                          <span className="truncate font-mono font-medium text-foreground">
                            {a.plate ?? "—"}
                          </span>
                        </div>
                        <div className="flex items-baseline gap-1.5">
                          <span className="w-14 shrink-0 text-muted-foreground">
                            {t("notifications.location")}
                          </span>
                          <span className="truncate font-medium text-foreground">{location}</span>
                        </div>
                      </div>

                      {/* Footer — timestamp + evidence link. */}
                      <div className="mt-2 flex items-center justify-between">
                        <span className="text-[11px] text-muted-foreground">
                          {relativeAge(a.ts)}
                        </span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            onEvidence(a);
                          }}
                          className="inline-flex cursor-pointer items-center gap-0.5 text-[11px] font-semibold text-severity-info transition-colors hover:underline"
                        >
                          {t("notifications.evidence")}
                          <ChevronRight className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  </li>
                );
              })
            )}
          </ul>
        </div>
      </CalciteSheet>

      <AlertEvidenceDialog alert={evidence} onClose={() => setEvidence(null)} />
    </>
  );
}

export default HeaderActions;
