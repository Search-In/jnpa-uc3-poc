import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { ParkingFacility } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { parkingStatusColour } from "@/lib/tokens";

// Pre-gate parking availability board (capability C7): facility cards with
// capacity / occupied / available + a status chip (AVAILABLE / FILLING / FULL,
// coloured via tokens) and parkingSummary() header totals.

function FacilityCard({ f }: { f: ParkingFacility }) {
  const colour = parkingStatusColour(f.status);
  return (
    <div className="space-y-2 rounded-md border border-border bg-background p-3">
      <div className="flex items-start justify-between gap-2">
        <span className="truncate text-xs font-medium" title={f.name}>
          {f.name}
        </span>
        <Badge colour={colour}>{f.status}</Badge>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full"
          style={{ width: `${Math.min(100, f.utilisation_pct)}%`, backgroundColor: colour }}
        />
      </div>
      <div className="flex justify-between text-[10px] tabular-nums text-muted-foreground">
        <span>{f.occupied}/{f.capacity}</span>
        <span style={{ color: colour }}>{f.available} free</span>
      </div>
    </div>
  );
}

export function ParkingBoard() {
  const { t } = useTranslation();
  const facQ = useQuery({
    queryKey: ["parking-availability"],
    queryFn: () => getAdapter().parkingAvailability(),
  });
  const sumQ = useQuery({ queryKey: ["parking-summary"], queryFn: () => getAdapter().parkingSummary() });

  const facilities = facQ.data ?? [];
  const s = sumQ.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("panels.parking.title")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.parking.subtitle")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        {s && (
          <div className="flex items-end justify-between rounded-md border border-border bg-background px-3 py-2">
            <div>
              <div className="text-[11px] text-muted-foreground">{t("panels.parking.totalAvailable")}</div>
              <div className="text-2xl font-semibold tabular-nums">
                {s.total_available}
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  / {s.total_capacity}
                </span>
              </div>
            </div>
            <div className="text-right text-[11px] text-muted-foreground">
              <div className="tabular-nums">
                {s.facilities} {t("panels.parking.facilities")}
              </div>
              <div className="tabular-nums">
                {s.full_count} {t("panels.parking.full")}
              </div>
            </div>
          </div>
        )}

        {facQ.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </div>
        ) : facilities.length === 0 ? (
          <EmptyState>{t("panels.parking.empty")}</EmptyState>
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {facilities.map((f) => (
              <FacilityCard key={f.facility_id} f={f} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default ParkingBoard;
