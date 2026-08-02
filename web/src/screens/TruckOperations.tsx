// Truck Operations — truck-lifecycle analytics host.
//
// A THIN HOST only: it owns no business logic and no queries of its own. It
// gives ECY TRT and Double Trip a single, correct home in the Truck & Cargo
// Lifecycle section — they previously lived as tabs on Live Operations (a
// corridor-traffic screen) and were duplicated again under Reports.
//
// Composes the existing DTCCC kit exactly like every other host screen; the two
// embedded screens are rendered inside <Embedded> so they drop their own page
// chrome and this host owns the header.

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Repeat, Timer, Truck } from "lucide-react";

import { PageContainer, PageHeader, SegmentedTabs, Embedded } from "@/components/ui/dtccc";
import EcyTrt from "@/screens/EcyTrt";
import DoubleTrip from "@/screens/DoubleTrip";

type TabKey = "trt" | "double-trip";
const TAB_KEYS: TabKey[] = ["trt", "double-trip"];

export default function TruckOperations() {
  // `?tab=` deep-link: the legacy /trt and /double-trip routes redirect here
  // with a tab, and the tab is reflected back into the URL so a view is
  // shareable.
  const [params, setParams] = useSearchParams();
  const urlTab = params.get("tab") as TabKey | null;
  const [tab, setTabState] = useState<TabKey>(urlTab && TAB_KEYS.includes(urlTab) ? urlTab : "trt");
  useEffect(() => {
    if (urlTab && TAB_KEYS.includes(urlTab) && urlTab !== tab) setTabState(urlTab);
  }, [urlTab]); // eslint-disable-line react-hooks/exhaustive-deps
  const setTab = (k: TabKey) => {
    setTabState(k);
    const next = new URLSearchParams(params);
    next.set("tab", k);
    setParams(next, { replace: true });
  };

  return (
    <PageContainer>
      <PageHeader
        icon={Truck}
        title="Truck Operations"
        subtitle="Truck turn-round & tractor-cycle analytics — ECY TRT · Double Trip"
      />

      <div className="flex flex-col gap-3 p-3 sm:gap-4 sm:p-4">
        <SegmentedTabs<TabKey>
          value={tab}
          onChange={setTab}
          tabs={[
            { key: "trt", label: "ECY TRT", icon: Timer },
            { key: "double-trip", label: "Double Trip", icon: Repeat },
          ]}
        />

        <Embedded>{tab === "trt" ? <EcyTrt /> : <DoubleTrip />}</Embedded>
      </div>
    </PageContainer>
  );
}
