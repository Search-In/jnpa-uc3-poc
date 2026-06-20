import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery } from "@tanstack/react-query";
import { getAdapter } from "@/data";
import type { IdentityVerifyResult } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";
import { fmtDateTimeIST } from "@/lib/utils";

// Driver identity verification (capability C2): the synthetic gallery from
// identityGallery() + a Verify control that runs identityVerify() in a chosen
// simulate mode (genuine / impostor / unknown) and shows the decision
// (VERIFIED / PROVISIONAL / REJECTED) with score + the 24h cure window for
// PROVISIONAL. DPDP posture (synthetic faces only) is shown inline.

type SimMode = "genuine" | "impostor" | "unknown";

function decisionColour(decision?: string): string {
  switch (decision) {
    case "VERIFIED":
      return STATUS.ok;
    case "PROVISIONAL":
      return STATUS.warning;
    case "REJECTED":
      return STATUS.critical;
    default:
      return STATUS.unknown;
  }
}

function ResultCard({ result }: { result: IdentityVerifyResult }) {
  const { t } = useTranslation();
  const colour = decisionColour(result.decision);
  return (
    <div className="space-y-2 rounded-md border border-border bg-background p-3">
      <div className="flex items-center justify-between">
        <Badge colour={colour}>{result.decision}</Badge>
        <span className="text-xs tabular-nums text-muted-foreground">
          {t("panels.identity.score")} {result.score.toFixed(2)}
        </span>
      </div>
      {result.reason && <p className="text-[11px] text-muted-foreground">{result.reason}</p>}
      {result.decision === "PROVISIONAL" && (
        <div
          className="rounded-md border px-2 py-1.5 text-[11px]"
          style={{ borderColor: `${STATUS.warning}80` }}
        >
          {t("panels.identity.cureWindow")}: {result.cure_window_h ?? 24} h
          {result.provisional_until && (
            <span className="block text-muted-foreground">
              {t("panels.identity.cureUntil")} {fmtDateTimeIST(result.provisional_until)}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export function IdentityPanel() {
  const { t } = useTranslation();
  const galleryQ = useQuery({
    queryKey: ["identity-gallery"],
    queryFn: () => getAdapter().identityGallery(),
  });
  const gallery = galleryQ.data ?? [];

  const [driverId, setDriverId] = useState<string>("");
  const [mode, setMode] = useState<SimMode>("genuine");

  const selected = driverId || gallery[0]?.driver_id || "";

  const verify = useMutation({
    mutationFn: () => getAdapter().identityVerify(selected, mode),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("panels.identity.title")}</CardTitle>
        <p className="text-[11px] text-muted-foreground">{t("panels.identity.subtitle")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* DPDP note — always visible */}
        <div
          className="rounded-md border px-3 py-2 text-[11px]"
          style={{ borderColor: `${STATUS.info}80`, backgroundColor: `${STATUS.info}1a` }}
        >
          {t("panels.identity.dpdpNote")}
        </div>

        {galleryQ.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </div>
        ) : gallery.length === 0 ? (
          <EmptyState>{t("panels.identity.empty")}</EmptyState>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <label className="text-[11px] text-muted-foreground">
                {t("panels.identity.driver")}
                <select
                  value={selected}
                  onChange={(e) => setDriverId(e.target.value)}
                  className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs"
                >
                  {gallery.map((d) => (
                    <option key={d.driver_id} value={d.driver_id}>
                      {d.name} · {d.license_no}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-[11px] text-muted-foreground">
                {t("panels.identity.simulate")}
                <select
                  value={mode}
                  onChange={(e) => setMode(e.target.value as SimMode)}
                  className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs"
                >
                  <option value="genuine">{t("panels.identity.genuine")}</option>
                  <option value="impostor">{t("panels.identity.impostor")}</option>
                  <option value="unknown">{t("panels.identity.unknown")}</option>
                </select>
              </label>
            </div>

            <Button
              size="sm"
              onClick={() => verify.mutate()}
              disabled={verify.isPending || !selected}
            >
              {verify.isPending ? <Spinner /> : null}
              {t("panels.identity.verify")}
            </Button>

            {verify.data && <ResultCard result={verify.data} />}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default IdentityPanel;
