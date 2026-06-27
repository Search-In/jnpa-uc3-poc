import * as React from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { useWidgetCollapsed, widgetCollapseStore } from "@/lib/widgetCollapse";

// CollapsibleCard — a drop-in replacement for the <Card><CardHeader><CardTitle>
// …</CardHeader><CardContent>…</CardContent></Card> pattern used by every
// dashboard widget. It adds a chevron in the header that toggles the body with a
// smooth height animation, while preserving each widget's existing header and
// body markup. Collapsed state is independent per `id` and persisted via the
// widgetCollapse store (survives navigation and reloads).
//
// Body animation uses the grid-rows 0fr↔1fr technique: it animates to the
// content's natural height with no JS measurement and no layout jump, and the
// card itself shrinks to just the header when collapsed so the dashboard grid
// reflows with no empty gap. Honours prefers-reduced-motion.

interface CollapsibleCardProps {
  /** Stable id used to persist this widget's collapsed state independently. */
  id: string;
  /** Header title text (rendered with the standard CardTitle styling). */
  title: React.ReactNode;
  /** Optional subtitle shown under the title (muted, like existing widgets). */
  subtitle?: React.ReactNode;
  /** Optional content shown in the header to the LEFT of the chevron (e.g. a Badge). */
  headerRight?: React.ReactNode;
  /** Extra classes for the outer Card (e.g. "flex h-full flex-col"). */
  className?: string;
  /** Extra classes for the body (CardContent), matching the old className. */
  bodyClassName?: string;
  /** data-guided-id passthrough for the guided tour. */
  "data-guided-id"?: string;
  children: React.ReactNode;
}

export function CollapsibleCard({
  id,
  title,
  subtitle,
  headerRight,
  className,
  bodyClassName,
  children,
  ...rest
}: CollapsibleCardProps) {
  const { t } = useTranslation();
  const collapsed = useWidgetCollapsed(id);
  const contentId = `${id}-content`;
  const toggle = () => widgetCollapseStore.toggle(id);

  return (
    <Card
      // When collapsed inside an items-stretch grid row, self-start stops the
      // card from stretching to a taller sibling's height (no empty gap).
      className={cn(className, collapsed && "self-start")}
      data-guided-id={rest["data-guided-id"]}
    >
      <CardHeader
        className="flex-row items-start justify-between gap-2 pb-2"
        onClick={toggle}
        role="button"
        tabIndex={-1}
      >
        <div className="flex min-w-0 flex-1 cursor-pointer flex-col gap-1">
          <CardTitle>{title}</CardTitle>
          {subtitle != null && <p className="text-[11px] text-muted-foreground">{subtitle}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {headerRight}
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              toggle();
            }}
            aria-expanded={!collapsed}
            aria-controls={contentId}
            aria-label={collapsed ? t("common.expand") : t("common.collapse")}
            title={collapsed ? t("common.expand") : t("common.collapse")}
            className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronDown
              className={cn(
                "h-4 w-4 transition-transform duration-200 motion-reduce:transition-none",
                !collapsed && "rotate-180",
              )}
            />
          </button>
        </div>
      </CardHeader>
      <div
        id={contentId}
        aria-hidden={collapsed}
        className={cn(
          "grid transition-[grid-template-rows] duration-300 ease-in-out motion-reduce:transition-none",
          // flex-1/min-h-0 lets equal-height cards (flex h-full flex-col) fill
          // their grid row when expanded; collapsed it stays at zero height.
          collapsed ? "grid-rows-[0fr]" : "min-h-0 flex-1 grid-rows-[1fr]",
        )}
      >
        <div className="flex flex-col overflow-hidden">
          <CardContent className={bodyClassName}>{children}</CardContent>
        </div>
      </div>
    </Card>
  );
}

export default CollapsibleCard;
