import { cn } from "@/lib/utils";
import { Loader2, TriangleAlert, RotateCw } from "lucide-react";
import { useTranslation } from "react-i18next";

export function Spinner({ className }: { className?: string }) {
  return (
    <Loader2
      className={cn("h-4 w-4 animate-spin text-muted-foreground", className)}
      aria-label="loading"
    />
  );
}

export function StatusDot({ colour, pulse }: { colour: string; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-2.5 w-2.5" aria-hidden>
      {pulse && (
        <span
          className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60"
          style={{ backgroundColor: colour }}
        />
      )}
      <span
        className="relative inline-flex h-2.5 w-2.5 rounded-full"
        style={{ backgroundColor: colour }}
      />
    </span>
  );
}

export function EmptyState({ children }: { children?: React.ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="p-6 text-center text-sm text-muted-foreground">
      {children ?? t("common.noData")}
    </div>
  );
}

/** Centred loading spinner (block-level) — the standard "loading a data section" state. */
export function LoadingState({ label }: { label?: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
      <Spinner /> {label ?? t("common.loading")}
    </div>
  );
}

/** Error state shown when a query FAILS (API/DB unavailable) — never conflated
 *  with "empty data". Offers a Retry button and a plain-language reason so an
 *  outage never masquerades as "No records". */
export function ErrorState({
  onRetry,
  detail,
}: {
  onRetry?: () => void;
  detail?: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-2 p-6 text-center"
    >
      <TriangleAlert className="h-6 w-6 text-amber-500" aria-hidden />
      <div className="text-sm font-medium">
        {t("common.errorTitle", "Unable to load live data")}
      </div>
      <div className="text-xs text-muted-foreground">
        {t("common.errorReason", "Backend / database is currently unavailable.")}
      </div>
      {detail ? (
        <div className="max-w-md truncate text-[11px] text-muted-foreground/70" title={detail}>
          {detail}
        </div>
      ) : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-muted"
        >
          <RotateCw className="h-3.5 w-3.5" /> {t("common.retry", "Retry")}
        </button>
      ) : null}
    </div>
  );
}

/** "Updated HH:MM:SS" chip driven by a react-query `dataUpdatedAt` epoch-ms. */
export function LastUpdated({ at, isFetching }: { at?: number; isFetching?: boolean }) {
  const { t } = useTranslation();
  if (isFetching) {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
        <Spinner className="h-3 w-3" /> {t("common.refreshing", "Refreshing…")}
      </span>
    );
  }
  if (!at) return null;
  const time = new Date(at).toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });
  return (
    <span className="text-[11px] text-muted-foreground" title={new Date(at).toISOString()}>
      {t("common.lastUpdated", "Updated {{time}} IST", { time })}
    </span>
  );
}

/** Minimal react-query result shape the boundary needs — keeps callers from
 *  passing the whole (typed) query object where the generics would fight. */
export interface AsyncStatus {
  isLoading: boolean;
  isError: boolean;
  isFetching?: boolean;
  error?: unknown;
}

/** Unified loading / error / empty / content gate for a data section.
 *
 * Precedence: loading → error (with Retry) → empty → children. This guarantees
 * that when a query FAILS we show the error+retry UI, and only show the
 * "no records" empty state on a *successful* empty response — so a backend/DB
 * outage is never mislabelled as "No data".
 */
export function AsyncBoundary({
  status,
  isEmpty,
  empty,
  onRetry,
  children,
}: {
  status: AsyncStatus;
  isEmpty?: boolean;
  empty?: React.ReactNode;
  onRetry?: () => void;
  children: React.ReactNode;
}) {
  if (status.isLoading) return <LoadingState />;
  if (status.isError)
    return <ErrorState onRetry={onRetry} detail={(status.error as Error)?.message} />;
  if (isEmpty) return <>{empty ?? <EmptyState />}</>;
  return <>{children}</>;
}
