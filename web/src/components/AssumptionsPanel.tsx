import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  CalciteButton,
  CalciteDialog,
  CalciteNotice,
  CalciteBlock,
} from "@esri/calcite-components-react";

// In-app Assumptions & Methodology panel (D.2 sub-criterion 1). A Calcite dialog
// summarising docs/ASSUMPTIONS.md — production-API → simulator posture, synthetic
// data, the DPDP "synthetic faces only" stance, KPI baselines, and what is REAL
// (not assumed) in the PoC. Self-contained: it renders its own trigger button so
// any screen can drop <AssumptionsPanel /> in without touching the Shell.

const SECTIONS = [
  "sources",
  "synthetic",
  "kpi",
  "real",
] as const;

export function AssumptionsPanel({
  buttonScale = "s",
}: {
  buttonScale?: "s" | "m" | "l";
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  return (
    <>
      <CalciteButton
        appearance="outline"
        kind="neutral"
        iconStart="information"
        scale={buttonScale}
        onClick={() => setOpen(true)}
      >
        {t("assumptions.open")}
      </CalciteButton>

      <CalciteDialog
        modal
        open={open}
        heading={t("assumptions.title")}
        description={t("assumptions.subtitle")}
        widthScale="l"
        onCalciteDialogClose={() => setOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <p style={{ margin: 0, fontSize: "0.8125rem", lineHeight: 1.5 }}>
            {t("assumptions.intro")}
          </p>

          {/* DPDP gets a prominent notice — it is the highest-sensitivity claim. */}
          <CalciteNotice open kind="warning" icon="lock" scale="s">
            <div slot="title">{t("assumptions.dpdp.title")}</div>
            <div slot="message">{t("assumptions.dpdp.body")}</div>
          </CalciteNotice>

          {SECTIONS.map((key) => (
            <CalciteBlock
              key={key}
              open
              collapsible
              heading={t(`assumptions.${key}.title`)}
            >
              <p style={{ margin: 0, fontSize: "0.8125rem", lineHeight: 1.5 }}>
                {t(`assumptions.${key}.body`)}
              </p>
            </CalciteBlock>
          ))}
        </div>
      </CalciteDialog>
    </>
  );
}

export default AssumptionsPanel;
