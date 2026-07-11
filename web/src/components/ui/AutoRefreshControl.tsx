import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";
import { REFRESH_OPTIONS, useRefresh } from "@/lib/refresh";
import { cn } from "@/lib/utils";

/** Global Auto-Refresh selector (Off / 10s / 30s / 1m / 5m).
 *
 * Off is the default — the Refresh button is the primary mechanism. Rendered in
 * every page header (via PageHeader / CommandCenter) so the setting is reachable
 * anywhere and applies app-wide (backed by the single RefreshProvider). */
export function AutoRefreshControl({ className }: { className?: string }) {
  const { t } = useTranslation();
  const { intervalMs, setIntervalMs, autoRefreshOn } = useRefresh();
  return (
    <label
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground",
        className,
      )}
      title={t("refresh.autoTitle", "Auto refresh interval")}
    >
      <RefreshCw
        className={cn("h-3.5 w-3.5", autoRefreshOn ? "text-primary" : "opacity-60")}
        aria-hidden
      />
      <span className="hidden sm:inline">{t("refresh.auto", "Auto")}</span>
      <select
        aria-label={t("refresh.auto", "Auto refresh")}
        value={intervalMs}
        onChange={(e) => setIntervalMs(Number(e.target.value))}
        className="cursor-pointer bg-transparent text-[11px] font-semibold text-foreground outline-none"
      >
        {REFRESH_OPTIONS.map((o) => (
          <option key={o.ms} value={o.ms}>
            {t(o.labelKey, o.label)}
          </option>
        ))}
      </select>
    </label>
  );
}
