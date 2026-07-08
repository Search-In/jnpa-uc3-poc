// Global command-centre search (header omnibox). One box to look up any entity
// across the portal — Vehicle, Driver, RC, DL, Container, FASTag, Alert or Case
// ID. Typing surfaces categorised actions; picking one stores the query
// (searchStore) and routes to the destination screen, which reads the store and
// runs its own RDS-backed lookup. No backend coupling here — pure navigation.

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Search,
  CornerDownLeft,
  Truck,
  IdCard,
  Container,
  CreditCard,
  BellRing,
  FileText,
} from "lucide-react";
import { useClickOutside } from "@/hooks/useClickOutside";
import { searchStore, detectEntity, type SearchEntity } from "@/lib/searchStore";
import { cn } from "@/lib/utils";

interface Target {
  entity: SearchEntity;
  route: string;
  icon: typeof Truck;
  labelKey: string;
}

// Order = suggestion order (the detected entity is hoisted to the top at runtime).
const TARGETS: Target[] = [
  { entity: "vehicle", route: "/intelligence", icon: Truck, labelKey: "search.vehicle" },
  { entity: "driver", route: "/intelligence", icon: IdCard, labelKey: "search.driver" },
  { entity: "container", route: "/gate-customs", icon: Container, labelKey: "search.container" },
  { entity: "fastag", route: "/fastag", icon: CreditCard, labelKey: "search.fastag" },
  { entity: "alert", route: "/alerts", icon: BellRing, labelKey: "search.alert" },
  { entity: "case", route: "/reports", icon: FileText, labelKey: "search.case" },
];

export function GlobalSearch() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  const suggestions = useMemo(() => {
    if (!q.trim()) return [];
    const detected = detectEntity(q);
    const ordered = [...TARGETS].sort(
      (a, b) => (a.entity === detected ? -1 : 0) - (b.entity === detected ? -1 : 0),
    );
    return ordered;
  }, [q]);

  useEffect(() => setActive(0), [q]);

  function go(tgt: Target) {
    searchStore.set(q.trim(), tgt.entity);
    setOpen(false);
    setQ("");
    navigate(`${tgt.route}?q=${encodeURIComponent(q.trim())}`);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (!suggestions.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, suggestions.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(suggestions[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div ref={ref} className="relative hidden flex-1 md:block md:max-w-md">
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <input
        type="search"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => q.trim() && setOpen(true)}
        onKeyDown={onKeyDown}
        placeholder={t("search.placeholder")}
        aria-label={t("search.placeholder")}
        className="h-9 w-full rounded-md border border-border bg-background py-1.5 pl-9 pr-3 text-[13px] outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20"
      />

      {open && suggestions.length > 0 && (
        <div className="absolute left-0 right-0 top-11 z-50 overflow-hidden rounded-lg border border-border bg-card shadow-lg">
          <div className="border-b border-border px-3 py-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("search.hint")}
          </div>
          <ul>
            {suggestions.map((s, i) => {
              const Icon = s.icon;
              return (
                <li key={s.entity}>
                  <button
                    type="button"
                    onMouseEnter={() => setActive(i)}
                    onClick={() => go(s)}
                    className={cn(
                      "flex w-full items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors",
                      i === active ? "bg-primary/10" : "hover:bg-muted",
                    )}
                  >
                    <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="text-foreground">
                      {t(s.labelKey)}{" "}
                      <span className="font-mono font-semibold text-primary">{q.trim()}</span>
                    </span>
                    {i === active && (
                      <CornerDownLeft className="ml-auto h-3.5 w-3.5 text-muted-foreground" />
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

export default GlobalSearch;
