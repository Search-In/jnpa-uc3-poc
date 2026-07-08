// DTCCC shared UI kit — the consistent command-centre building blocks every
// redesigned screen composes so the portal reads as one system:
//   PageContainer / PageHeader   — page chrome (title, last-updated, refresh)
//   StatCard / StatGrid          — summary KPI cards
//   SegmentedTabs                — the pill tab control
//   SearchInput / FilterSelect   — toolbar controls
//   DataTable                    — Top-N table with search + pagination +
//                                  View-all + loading/empty/error/retry states
//
// Pure presentation — no data fetching, no backend coupling. Colours come from
// tokens.ts only.

import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ChevronLeft, ChevronRight, RefreshCw, Search, type LucideIcon } from "lucide-react";
import { Card } from "@/components/ui/card";
import {
  ErrorState,
  LoadingState,
  EmptyState,
  LastUpdated,
  type AsyncStatus,
} from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { cn } from "@/lib/utils";

export type Tone = "info" | "ok" | "warn" | "critical" | "neutral";
export const TONE_COLOUR: Record<Tone, string> = {
  info: STATUS.info,
  ok: STATUS.ok,
  warn: STATUS.warning,
  critical: STATUS.critical,
  neutral: "#64748b",
};

// --- Page chrome -------------------------------------------------------------

export function PageContainer({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex h-full flex-col overflow-y-auto bg-background", className)}>
      {children}
    </div>
  );
}

export function PageHeader({
  icon: Icon,
  title,
  subtitle,
  updatedAt,
  isFetching,
  onRefresh,
  actions,
}: {
  icon?: LucideIcon;
  title: string;
  subtitle?: string;
  updatedAt?: number;
  isFetching?: boolean;
  onRefresh?: () => void;
  actions?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-card px-4 py-3">
      <div className="flex items-center gap-2.5">
        {Icon && (
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Icon className="h-5 w-5" strokeWidth={2} />
          </span>
        )}
        <div>
          <h1 className="text-lg font-bold leading-tight tracking-tight text-foreground">
            {title}
          </h1>
          {subtitle && <p className="text-xs text-muted-foreground">{subtitle}</p>}
        </div>
      </div>
      <div className="ml-auto flex items-center gap-3">
        {actions}
        {(updatedAt !== undefined || isFetching) && (
          <LastUpdated at={updatedAt || undefined} isFetching={isFetching} />
        )}
        {onRefresh && (
          <button
            type="button"
            onClick={onRefresh}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
            {t("commandCenter.refresh")}
          </button>
        )}
      </div>
    </div>
  );
}

// --- Summary cards -----------------------------------------------------------

export function StatGrid({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-4", className)}>
      {children}
    </div>
  );
}

export function StatCard({
  icon: Icon,
  label,
  value,
  tone = "info",
  sub,
  loading,
}: {
  icon?: LucideIcon;
  label: string;
  value: ReactNode;
  tone?: Tone;
  sub?: ReactNode;
  loading?: boolean;
}) {
  const colour = TONE_COLOUR[tone];
  return (
    <Card className="flex items-center gap-3 p-3">
      {Icon && (
        <span
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg"
          style={{ backgroundColor: `${colour}1a`, color: colour }}
        >
          <Icon className="h-5 w-5" strokeWidth={2} />
        </span>
      )}
      <div className="min-w-0">
        <div className="text-xl font-bold tabular-nums leading-none text-foreground">
          {loading ? <span className="text-muted-foreground">…</span> : value}
        </div>
        <div className="mt-1 truncate text-[11px] font-medium text-muted-foreground" title={label}>
          {label}
        </div>
        {sub && <div className="mt-0.5 text-[10.5px] text-muted-foreground">{sub}</div>}
      </div>
    </Card>
  );
}

// --- Tabs --------------------------------------------------------------------

export function SegmentedTabs<T extends string>({
  tabs,
  value,
  onChange,
  className,
}: {
  tabs: { key: T; label: string; icon?: LucideIcon; count?: number }[];
  value: T;
  onChange: (key: T) => void;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "inline-flex flex-wrap gap-0.5 rounded-lg border border-border bg-card p-0.5",
        className,
      )}
    >
      {tabs.map((tb) => {
        const active = tb.key === value;
        const Icon = tb.icon;
        return (
          <button
            key={tb.key}
            type="button"
            onClick={() => onChange(tb.key)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
              active
                ? "bg-primary text-primary-foreground shadow-sm"
                : "text-foreground hover:bg-muted",
            )}
          >
            {Icon && <Icon className="h-3.5 w-3.5" aria-hidden />}
            {tb.label}
            {typeof tb.count === "number" && (
              <span
                className={cn(
                  "rounded-full px-1.5 text-[10px] font-bold tabular-nums",
                  active ? "bg-white/20" : "bg-muted",
                )}
              >
                {tb.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// --- Toolbar controls --------------------------------------------------------

export function SearchInput({
  value,
  onChange,
  placeholder,
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <div className={cn("relative", className)}>
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder ?? t("common.search")}
        className="h-9 w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-3 text-[13px] outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20"
      />
    </div>
  );
}

export function FilterSelect({
  value,
  onChange,
  options,
  label,
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  label?: string;
  className?: string;
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        "h-9 rounded-md border border-border bg-background px-2 text-[13px] font-medium text-foreground outline-none transition-colors hover:bg-muted focus:ring-2 focus:ring-primary/20",
        className,
      )}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

// --- DataTable ---------------------------------------------------------------

export interface Column<T> {
  key: string;
  header: ReactNode;
  render?: (row: T) => ReactNode;
  align?: "left" | "right" | "center";
  className?: string;
  headerClassName?: string;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  status,
  onRetry,
  emptyLabel,
  search,
  searchPlaceholder,
  toolbar,
  pageSize = 10,
  viewAllTo,
  viewAllLabel,
  onRowClick,
  isRowActive,
  maxHeight,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  status?: AsyncStatus;
  onRetry?: () => void;
  emptyLabel?: ReactNode;
  /** Enables the search box; return true to keep the row for query `q`. */
  search?: (row: T, q: string) => boolean;
  searchPlaceholder?: string;
  /** Extra filter controls rendered in the toolbar (right of search). */
  toolbar?: ReactNode;
  pageSize?: number;
  viewAllTo?: string;
  viewAllLabel?: string;
  onRowClick?: (row: T) => void;
  isRowActive?: (row: T) => boolean;
  maxHeight?: string;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);

  const filtered = useMemo(() => {
    if (!search || !q.trim()) return rows;
    return rows.filter((r) => search(r, q.trim().toLowerCase()));
  }, [rows, q, search]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = filtered.slice(safePage * pageSize, safePage * pageSize + pageSize);

  const showToolbar = !!search || !!toolbar;

  return (
    <div className="flex flex-col">
      {showToolbar && (
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
          {search && (
            <SearchInput
              value={q}
              onChange={(v) => {
                setQ(v);
                setPage(0);
              }}
              placeholder={searchPlaceholder}
              className="w-full max-w-xs"
            />
          )}
          {toolbar}
        </div>
      )}

      {status?.isLoading ? (
        <LoadingState />
      ) : status?.isError ? (
        <ErrorState onRetry={onRetry} detail={(status.error as Error)?.message} />
      ) : filtered.length === 0 ? (
        <EmptyState>{emptyLabel}</EmptyState>
      ) : (
        <>
          <div className="overflow-auto" style={{ maxHeight }}>
            <table className="w-full text-left text-[13px]">
              <thead className="sticky top-0 z-10 bg-muted/80 text-[11px] uppercase tracking-wide text-muted-foreground backdrop-blur">
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={cn(
                        "px-3 py-2 font-semibold",
                        c.align === "right" && "text-right",
                        c.align === "center" && "text-center",
                        c.headerClassName,
                      )}
                    >
                      {c.header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {pageRows.map((row) => {
                  const active = isRowActive?.(row);
                  return (
                    <tr
                      key={rowKey(row)}
                      onClick={onRowClick ? () => onRowClick(row) : undefined}
                      className={cn(
                        onRowClick && "cursor-pointer",
                        active ? "bg-primary/10" : "hover:bg-muted/40",
                      )}
                    >
                      {columns.map((c) => (
                        <td
                          key={c.key}
                          className={cn(
                            "px-3 py-2",
                            c.align === "right" && "text-right",
                            c.align === "center" && "text-center",
                            c.className,
                          )}
                        >
                          {c.render ? c.render(row) : (row as Record<string, ReactNode>)[c.key]}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Footer: count + pagination + view-all */}
          <div className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
            <span>
              {t("dtable.showing", "Showing {{from}}–{{to}} of {{total}}", {
                from: filtered.length === 0 ? 0 : safePage * pageSize + 1,
                to: Math.min(filtered.length, safePage * pageSize + pageSize),
                total: filtered.length,
              })}
            </span>
            <div className="ml-auto flex items-center gap-2">
              {viewAllTo && (
                <Link to={viewAllTo} className="font-semibold text-primary hover:underline">
                  {viewAllLabel ?? t("commandCenter.viewAll")}
                </Link>
              )}
              {pageCount > 1 && (
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    disabled={safePage === 0}
                    onClick={() => setPage(safePage - 1)}
                    className="inline-flex h-6 w-6 items-center justify-center rounded border border-border disabled:opacity-40 enabled:hover:bg-muted"
                    aria-label={t("dtable.prev", "Previous page")}
                  >
                    <ChevronLeft className="h-3.5 w-3.5" />
                  </button>
                  <span className="tabular-nums">
                    {safePage + 1}/{pageCount}
                  </span>
                  <button
                    type="button"
                    disabled={safePage >= pageCount - 1}
                    onClick={() => setPage(safePage + 1)}
                    className="inline-flex h-6 w-6 items-center justify-center rounded border border-border disabled:opacity-40 enabled:hover:bg-muted"
                    aria-label={t("dtable.next", "Next page")}
                  >
                    <ChevronRight className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// --- Status chip -------------------------------------------------------------

export function StatusChip({ label, tone = "neutral" }: { label: ReactNode; tone?: Tone }) {
  const colour = TONE_COLOUR[tone];
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[10.5px] font-semibold"
      style={{ backgroundColor: `${colour}1f`, color: colour }}
    >
      {label}
    </span>
  );
}
