import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { Alert, AutoLeoResult } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusDot, Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { relativeAge } from "@/lib/utils";

// Auto-LEO gate-out queue (capabilities C4/C5): per-container leo_ready status
// (green/red dot) with customs-flag chips, plus a Customs alert feed derived
// from the blocked rows (customsFlags()). Real reconciliation logic over
// schema-faithful synthetic gate-data.

function LeoRow({ row }: { row: AutoLeoResult }) {
  const { t } = useTranslation();
  const colour = row.leo_ready ? STATUS.ok : STATUS.critical;
  return (
    <div className="flex items-start justify-between gap-2 border-b border-border/50 px-3 py-2">
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
      <Badge colour={colour}>{row.leo_ready ? t("panels.leo.ready") : t("panels.leo.blocked")}</Badge>
    </div>
  );
}

function CustomsFeed({ flags }: { flags: Alert[] }) {
  const { t } = useTranslation();
  if (flags.length === 0) {
    return <EmptyState>{t("panels.leo.customsEmpty")}</EmptyState>;
  }
  return (
    <ul className="divide-y divide-border/50">
      {flags.map((a) => (
        <li key={a.id} className="flex items-center justify-between gap-2 px-3 py-2">
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
          <span className="shrink-0 text-[10px] text-muted-foreground">{relativeAge(a.ts)}</span>
        </li>
      ))}
    </ul>
  );
}

export function AutoLeoPanel() {
  const { t } = useTranslation();
  const queueQ = useQuery({ queryKey: ["leo-queue"], queryFn: () => getAdapter().leoQueue() });
  const flagsQ = useQuery({ queryKey: ["customs-flags"], queryFn: () => getAdapter().customsFlags() });

  const rows = queueQ.data ?? [];
  const flags = flagsQ.data ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("panels.leo.title")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.leo.subtitle")}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {queueQ.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState>{t("panels.leo.empty")}</EmptyState>
        ) : (
          <div className="rounded-md border border-border">
            {rows.map((r) => (
              <LeoRow key={r.container_no} row={r} />
            ))}
          </div>
        )}

        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("panels.leo.customsTitle")}
          </div>
          <div className="rounded-md border border-border">
            {flagsQ.isLoading ? (
              <div className="flex items-center gap-2 p-3 text-sm text-muted-foreground">
                <Spinner /> {t("common.loading")}
              </div>
            ) : (
              <CustomsFeed flags={flags} />
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default AutoLeoPanel;
