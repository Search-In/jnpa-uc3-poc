import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

// Shared client-side pagination primitive: a `usePagination` hook that slices a
// row array into pages, and a <Pagination> bar (Previous · numbered pages with
// ellipsis · Next + "Showing X–Y of Z"). One implementation reused wherever a
// list needs paging, so the control looks and behaves identically everywhere.

export interface PageState<T> {
  /** Current (clamped) page index, 0-based. */
  page: number;
  setPage: (p: number) => void;
  /** Total number of pages (min 1). */
  pageCount: number;
  /** The rows for the current page. */
  pageRows: T[];
  /** 1-based index of the first row shown (0 when empty). */
  from: number;
  /** 1-based index of the last row shown. */
  to: number;
  /** Total row count across all pages. */
  total: number;
  pageSize: number;
}

/**
 * Client-side pagination over an in-memory array.
 *
 * The page index is component-local state, so a data refresh (manual or auto)
 * that replaces `rows` in place keeps the user on the same page — it only clamps
 * to the last page if the new data has fewer pages than the current index.
 */
export function usePagination<T>(rows: T[], pageSize = 10): PageState<T> {
  const [page, setPage] = useState(0);
  const total = rows.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  // Clamp for display/slicing without resetting the stored page — so growing
  // data keeps the operator exactly where they were.
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = useMemo(
    () => rows.slice(safePage * pageSize, safePage * pageSize + pageSize),
    [rows, safePage, pageSize],
  );
  return {
    page: safePage,
    setPage,
    pageCount,
    pageRows,
    from: total === 0 ? 0 : safePage * pageSize + 1,
    to: Math.min(total, safePage * pageSize + pageSize),
    total,
    pageSize,
  };
}

/** Build a windowed page list: first, last, current±1, joined by ellipses. */
function pageWindow(current: number, count: number): (number | "ellipsis")[] {
  const wanted = new Set<number>([0, count - 1, current - 1, current, current + 1]);
  const pages = [...wanted].filter((p) => p >= 0 && p < count).sort((a, b) => a - b);
  const out: (number | "ellipsis")[] = [];
  let prev = -1;
  for (const p of pages) {
    if (prev >= 0 && p - prev > 1) out.push("ellipsis");
    out.push(p);
    prev = p;
  }
  return out;
}

/**
 * Pagination bar. Renders nothing when everything fits on one page
 * (total <= pageSize), so short lists show no controls.
 */
export function Pagination({
  page,
  pageCount,
  from,
  to,
  total,
  onPage,
  className,
}: {
  page: number;
  pageCount: number;
  from: number;
  to: number;
  total: number;
  onPage: (p: number) => void;
  className?: string;
}) {
  const { t } = useTranslation();
  if (pageCount <= 1) return null;

  const btn =
    "inline-flex h-6 min-w-6 items-center justify-center gap-1 rounded border border-border px-1.5 disabled:opacity-40 enabled:hover:bg-muted";

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 border-t border-border px-3 py-2 text-[11px] text-muted-foreground",
        className,
      )}
    >
      <span className="tabular-nums">
        {t("dtable.showing", "Showing {{from}}–{{to}} of {{total}}", { from, to, total })}
      </span>
      <div className="ml-auto flex items-center gap-1">
        <button
          type="button"
          disabled={page === 0}
          onClick={() => onPage(page - 1)}
          className={btn}
          aria-label={t("dtable.prev", "Previous page")}
        >
          <ChevronLeft className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">{t("dtable.previous", "Previous")}</span>
        </button>
        {pageWindow(page, pageCount).map((p, i) =>
          p === "ellipsis" ? (
            <span key={`e${i}`} className="px-1">
              …
            </span>
          ) : (
            <button
              key={p}
              type="button"
              onClick={() => onPage(p)}
              aria-current={p === page ? "page" : undefined}
              className={cn(
                btn,
                "tabular-nums",
                p === page && "border-primary bg-primary text-primary-foreground",
              )}
            >
              {p + 1}
            </button>
          ),
        )}
        <button
          type="button"
          disabled={page >= pageCount - 1}
          onClick={() => onPage(page + 1)}
          className={btn}
          aria-label={t("dtable.next", "Next page")}
        >
          <span className="hidden sm:inline">{t("dtable.nextLabel", "Next")}</span>
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}
