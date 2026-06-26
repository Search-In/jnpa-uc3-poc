import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { Alert, AutoLeoResult } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusDot, Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { relativeAge } from "@/lib/utils";
import { alertFocusStore, useAlertFocus } from "@/lib/alertFocus";

// Auto-LEO gate-out queue (capabilities C4/C5) and the Customs alert feed are now
// two sibling cards so they sit in the same dashboard row as the TAS widget.
//
// Selection is SINGLE and SHARED: both panels read the one alertFocusStore that
// also drives the map. Because the store holds exactly one focused item, only
// one row across BOTH panels can be highlighted at a time — clicking anywhere
// clears the previous selection for free, and the UI highlight always matches
// the map focus (no separate per-panel state to drift).

// Selected affordance (spec): border + tint + glow, via ring so the 2px outline
// never shifts row layout.
//   border: 2px #3b82f6; background: rgba(59,130,246,0.08); shadow: 0 0 12px …
const SELECTED_ITEM =
  "bg-[#3b82f6]/[0.08] ring-2 ring-[#3b82f6] shadow-[0_0_12px_rgba(59,130,246,0.3)]";
const ROW_BASE =
  "cursor-pointer rounded-sm transition-all duration-200 ease-in-out hover:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-[#3b82f6]";

/** Stable focus id for a queue row, so selection ↔ map stay in sync. */
const leoFocusId = (containerNo: string): string => `leo-${containerNo}`;

/** Pan/zoom the map to a lat/lon by reusing the alert-focus channel. */
function focusOnMap(opts: { id: string; lat?: number; lon?: number; alert?: Alert }): void {
  if (opts.alert) {
    alertFocusStore.focus(opts.alert);
    return;
  }
  if (typeof opts.lat !== "number" || typeof opts.lon !== "number") return;
  alertFocusStore.focus({
    id: opts.id,
    ts: new Date().toISOString(),
    kind: "LEO",
    severity: "info",
    payload: { lat: opts.lat, lon: opts.lon },
  });
}

function LeoRow({
  row,
  selected,
  onSelect,
}: {
  row: AutoLeoResult;
  selected: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const colour = row.leo_ready ? STATUS.ok : STATUS.critical;
  const focusable = typeof row.lat === "number" && typeof row.lon === "number";
  return (
    <div
      role={focusable ? "button" : undefined}
      tabIndex={focusable ? 0 : undefined}
      onClick={focusable ? onSelect : undefined}
      onKeyDown={
        focusable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect();
              }
            }
          : undefined
      }
      className={`flex items-start justify-between gap-2 border-b border-border/50 px-3 py-2 ${
        focusable ? ROW_BASE : ""
      } ${selected ? SELECTED_ITEM : ""}`}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <StatusDot colour={colour} />
          <span className="font-mono text-xs">{row.container_no}</span>
        </div>
        <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
          {row.vehicle_plate ?? "—"}
        </div>
        {row.customs_flags.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {row.customs_flags.map((f) => (
              <Badge key={f} colour={STATUS.warning}>
                {f}
              </Badge>
            ))}
          </div>
        )}
      </div>
      <Badge colour={colour}>
        {row.leo_ready ? t("panels.leo.ready") : t("panels.leo.blocked")}
      </Badge>
    </div>
  );
}

export function AutoLeoPanel() {
  const { t } = useTranslation();
  const queueQ = useQuery({ queryKey: ["leo-queue"], queryFn: () => getAdapter().leoQueue() });
  // Single, shared selection — derived from the same store that drives the map.
  const focus = useAlertFocus();
  const rows = queueQ.data ?? [];

  return (
    <Card className="flex h-full flex-col">
      <CardHeader>
        <CardTitle>{t("panels.leo.title")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.leo.subtitle")}</p>
      </CardHeader>
      <CardContent className="flex-1">
        {queueQ.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState>{t("panels.leo.empty")}</EmptyState>
        ) : (
          <div className="rounded-md border border-border">
            {rows.map((r) => (
              <LeoRow
                key={r.container_no}
                row={r}
                selected={focus.alert?.id === leoFocusId(r.container_no)}
                onSelect={() => focusOnMap({ id: leoFocusId(r.container_no), lat: r.lat, lon: r.lon })}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function CustomsFeedPanel() {
  const { t } = useTranslation();
  const flagsQ = useQuery({
    queryKey: ["customs-flags"],
    queryFn: () => getAdapter().customsFlags(),
  });
  // Same shared single-selection store as the queue panel + the map.
  const focus = useAlertFocus();
  const flags = flagsQ.data ?? [];

  return (
    <Card className="flex h-full flex-col">
      <CardHeader>
        <CardTitle>{t("panels.leo.customsTitle")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.leo.customsSubtitle")}</p>
      </CardHeader>
      <CardContent className="flex-1">
        <div className="rounded-md border border-border">
          {flagsQ.isLoading ? (
            <div className="flex items-center gap-2 p-3 text-sm text-muted-foreground">
              <Spinner /> {t("common.loading")}
            </div>
          ) : flags.length === 0 ? (
            <EmptyState>{t("panels.leo.customsEmpty")}</EmptyState>
          ) : (
            <ul className="divide-y divide-border/50">
              {flags.map((a) => {
                const lat = a.payload?.lat as number | undefined;
                const lon = a.payload?.lon as number | undefined;
                const focusable = typeof lat === "number" && typeof lon === "number";
                return (
                  <li
                    key={a.id}
                    role={focusable ? "button" : undefined}
                    tabIndex={focusable ? 0 : undefined}
                    onClick={focusable ? () => focusOnMap({ id: a.id, alert: a }) : undefined}
                    onKeyDown={
                      focusable
                        ? (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              focusOnMap({ id: a.id, alert: a });
                            }
                          }
                        : undefined
                    }
                    className={`flex items-center justify-between gap-2 px-3 py-2 ${
                      focusable ? ROW_BASE : ""
                    } ${focus.alert?.id === a.id ? SELECTED_ITEM : ""}`}
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Badge colour={STATUS.warning}>{a.kind}</Badge>
                        <span className="truncate font-mono text-[11px]">
                          {(a.payload?.container_no as string) ?? a.plate ?? "—"}
                        </span>
                      </div>
                      <div className="mt-0.5 flex flex-wrap gap-1">
                        {((a.payload?.customs_flags as string[]) ?? []).map((f) => (
                          <span key={f} className="font-mono text-[10px] text-muted-foreground">
                            {f}
                          </span>
                        ))}
                      </div>
                    </div>
                    <span className="shrink-0 text-[10px] text-muted-foreground">
                      {relativeAge(a.ts)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default AutoLeoPanel;
