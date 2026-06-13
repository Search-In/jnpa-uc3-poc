import * as React from "react";
import { cn } from "@/lib/utils";

// A severity-aware chip. `colour` lets callers pass an explicit hex (from the
// CB-safe palette) for the dot + border; the label stays high-contrast white.
export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  colour?: string;
  dot?: boolean;
}

export function Badge({ className, colour, dot = true, children, style, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        className
      )}
      style={{
        borderColor: colour ? `${colour}80` : undefined,
        backgroundColor: colour ? `${colour}26` : undefined,
        ...style,
      }}
      {...props}
    >
      {dot && colour && (
        <span className="h-2 w-2 rounded-full" style={{ backgroundColor: colour }} aria-hidden />
      )}
      {children}
    </span>
  );
}
