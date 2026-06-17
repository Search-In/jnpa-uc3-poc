import { Badge } from "@/components/ui/badge";
import { STATUS } from "@/lib/tokens";

// A small LIVE / CACHED / SYNTHETIC chip so the fallback path of any capability
// call is visible on screen (Task 6). Colour comes from tokens.ts only:
//   LIVE      → ok (green)
//   CACHED    → warning (amber)
//   SYNTHETIC → info (sky blue)
// so the demo's "this number is synthetic" provenance is never hidden.
export function decisionPathColour(path?: string | null): string {
  const p = (path ?? "").toUpperCase();
  if (p.includes("LIVE") || p === "PRIMARY") return STATUS.ok;
  if (p.includes("CACHE")) return STATUS.warning;
  if (p.includes("SYNTH")) return STATUS.info;
  return STATUS.unknown;
}

export function DecisionPathBadge({
  path,
  className,
}: {
  path?: string | null;
  className?: string;
}) {
  const label = (path ?? "—").toUpperCase();
  return (
    <Badge colour={decisionPathColour(path)} className={className}>
      {label}
    </Badge>
  );
}

export default DecisionPathBadge;
