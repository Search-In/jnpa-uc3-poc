// TasWidget — Terminal Appointment System slot board (TFC-1 step 5). Shows the
// gate's appointment slots and, when TFC-1 reschedules them, the RESCHEDULED
// state. Built from the existing Card/Badge design — a real dashboard widget the
// guided tour can navigate to, scroll to, and highlight (data-guided-id).
//
// Data is read-only through the typed adapter (getAdapter().tasSlots), so it
// works in both live (gateway /api/tas/slots) and mock modes.
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import { CollapsibleCard } from "@/components/ui/CollapsibleCard";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/misc";
import { fmtTimeIST } from "@/lib/utils";

// TFC-1 closes G-NSICT, so the appointment board for that gate is the one the
// scenario reschedules.
const GATE = "G-NSICT";

const STATUS_COLOUR: Record<string, string> = {
  BOOKED: "#009E73",
  RESCHEDULED: "#E69F00",
  CANCELLED: "#D55E00",
};

function Stat({ label, value, colour }: { label: string; value: number; colour: string }) {
  return (
    <div className="rounded-md border border-border bg-muted/40 p-2 text-center">
      <div className="text-lg font-semibold tabular-nums" style={{ color: colour }}>
        {value}
      </div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  );
}

export function TasWidget() {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: ["tas-slots", GATE],
    queryFn: () => getAdapter().tasSlots(GATE),
    refetchInterval: 5000,
  });
  const slots = q.data ?? [];
  const active = slots.filter((s) => s.status === "BOOKED").length;
  const rescheduled = slots.filter((s) => s.status === "RESCHEDULED").length;
  const pending = slots.filter((s) => s.status !== "BOOKED" && s.status !== "RESCHEDULED").length;
  const slotStatus = rescheduled > 0 ? t("tas.rescheduledStatus") : t("tas.onSchedule");
  const updatedTs = q.dataUpdatedAt ? new Date(q.dataUpdatedAt).toISOString() : undefined;

  return (
    <CollapsibleCard
      id="tas"
      data-guided-id="tas-widget"
      className="flex h-full flex-col"
      title={t("tas.title")}
      headerRight={
        <Badge colour="#56B4E9" dot={false}>
          {t("tas.gate")} {GATE.replace("G-", "")}
        </Badge>
      }
      bodyClassName="flex-1 space-y-3"
    >
      {/* Active / Rescheduled / Pending slot counts */}
        <div className="grid grid-cols-3 gap-2">
          <Stat label={t("tas.active")} value={active} colour={STATUS_COLOUR.BOOKED} />
          <Stat
            label={t("tas.rescheduled")}
            value={rescheduled}
            colour={STATUS_COLOUR.RESCHEDULED}
          />
          <Stat label={t("tas.pending")} value={pending} colour="#56B4E9" />
        </div>

        {/* Gate + overall slot status */}
        <div className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">{t("tas.slotStatus")}</span>
          <Badge colour={rescheduled > 0 ? STATUS_COLOUR.RESCHEDULED : STATUS_COLOUR.BOOKED}>
            {slotStatus}
          </Badge>
        </div>

        {/* Slot table */}
        <div className="max-h-32 overflow-y-auto rounded-md border border-border">
          {q.isLoading ? (
            <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
              <Spinner /> {t("tas.loadingSlots")}
            </div>
          ) : slots.length === 0 ? (
            <div className="p-3 text-xs text-muted-foreground">{t("tas.noSlots")}</div>
          ) : (
            <table className="w-full text-xs">
              <thead className="sticky top-0 border-b border-border bg-card text-left text-[10px] uppercase text-muted-foreground">
                <tr>
                  <th className="px-3 py-1.5">{t("tas.colSlot")}</th>
                  <th className="px-3 py-1.5">{t("tas.colGate")}</th>
                  <th className="px-3 py-1.5">{t("tas.colWindow")}</th>
                  <th className="px-3 py-1.5">{t("tas.colStatus")}</th>
                </tr>
              </thead>
              <tbody>
                {slots.slice(0, 12).map((s) => (
                  <tr key={s.slot_id} className="border-b border-border/40">
                    <td className="px-3 py-1.5 font-mono">
                      {s.slot_id.replace(`TAS-${GATE}-`, "#")}
                    </td>
                    <td className="px-3 py-1.5">{s.gate_id.replace("G-", "")}</td>
                    <td className="px-3 py-1.5 tabular-nums">{fmtTimeIST(s.start)}</td>
                    <td className="px-3 py-1.5">
                      <Badge colour={STATUS_COLOUR[s.status] ?? "#56B4E9"}>{s.status}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Last updated */}
        <div className="text-[10px] text-muted-foreground">
          {slots.length} {t("tas.slotsCount")} · {t("tas.lastUpdated")}{" "}
          {updatedTs ? fmtTimeIST(updatedTs) : "—"}
        </div>
    </CollapsibleCard>
  );
}

export default TasWidget;
