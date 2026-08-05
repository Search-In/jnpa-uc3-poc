// <DataSourceToggle> — a compact LIVE | DEMO segmented control + a small badge
// showing the active data-SOURCE mode (the provenance of gateway-served rows).
// LIVE surfaces JNPA-API-sourced rows; DEMO (default) the reliable
// manually-imported rows. The choice is persisted (localStorage) and injected as
// the `x-data-mode` header on every gateway request by lib/api.ts.
//
// On change we invalidate every TanStack Query so all panels refetch with the
// new header (no reload needed).

import { useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Database, Zap } from "lucide-react";
import {
  getDataSourceMode,
  setDataSourceMode,
  subscribeDataSourceMode,
  type DataSourceMode,
} from "@/lib/dataSourceMode";
import { cn } from "@/lib/utils";

export function DataSourceToggle() {
  const queryClient = useQueryClient();
  const mode = useSyncExternalStore(subscribeDataSourceMode, getDataSourceMode, getDataSourceMode);

  const onSelect = (next: DataSourceMode) => {
    if (next === mode) return;
    setDataSourceMode(next);
    // Re-fetch everything so panels reflect the new X-Data-Mode header.
    void queryClient.invalidateQueries();
  };

  const isLive = mode === "LIVE";

  return (
    <div
      className="flex items-center gap-2"
      title={
        isLive
          ? "Data source: LIVE — rows from the JNPA integration APIs. Switch to DEMO for the reliable pre-loaded data."
          : "Data source: DEMO — the reliable pre-loaded data. Switch to LIVE for JNPA-API-sourced rows."
      }
    >
      <div
        role="group"
        aria-label="Data source mode"
        className="inline-flex h-9 items-center rounded-md border border-border bg-background p-0.5"
      >
        {(["LIVE", "DEMO"] as const).map((value) => {
          const active = mode === value;
          return (
            <button
              key={value}
              type="button"
              aria-pressed={active}
              onClick={() => onSelect(value)}
              className={cn(
                "inline-flex items-center gap-1 rounded px-2.5 py-1 text-[12px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
                active
                  ? "bg-primary text-primary-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-muted",
              )}
            >
              {value === "LIVE" ? (
                <Zap className="h-3 w-3" aria-hidden />
              ) : (
                <Database className="h-3 w-3" aria-hidden />
              )}
              {value}
            </button>
          );
        })}
      </div>
      <span
        aria-label={`Data source ${mode}`}
        className={cn(
          "hidden rounded-full px-2 py-0.5 text-[11px] font-semibold lg:inline",
          isLive ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
        )}
      >
        {isLive ? "LIVE · JNPA API" : "DEMO · pre-loaded"}
      </span>
    </div>
  );
}
