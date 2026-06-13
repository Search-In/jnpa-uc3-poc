import { cn } from "@/lib/utils";
import { Loader2 } from "lucide-react";

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("h-4 w-4 animate-spin text-muted-foreground", className)} aria-label="loading" />;
}

export function StatusDot({ colour, pulse }: { colour: string; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-2.5 w-2.5" aria-hidden>
      {pulse && (
        <span
          className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60"
          style={{ backgroundColor: colour }}
        />
      )}
      <span className="relative inline-flex h-2.5 w-2.5 rounded-full" style={{ backgroundColor: colour }} />
    </span>
  );
}

export function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="p-6 text-center text-sm text-muted-foreground">{children}</div>;
}
