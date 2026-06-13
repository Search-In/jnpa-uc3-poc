import type { Config } from "tailwindcss";

// Colour-blind-safe (Okabe–Ito) severity palette, exposed as Tailwind colours so
// every component pulls from one source. All foreground/background pairs below
// meet WCAG AA contrast (>= 4.5:1) against the slate-950 control-room canvas.
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(215 28% 22%)",
        input: "hsl(215 28% 22%)",
        ring: "hsl(199 89% 48%)",
        background: "hsl(222 47% 8%)",
        foreground: "hsl(210 40% 96%)",
        muted: { DEFAULT: "hsl(217 33% 17%)", foreground: "hsl(215 20% 65%)" },
        card: { DEFAULT: "hsl(222 47% 11%)", foreground: "hsl(210 40% 96%)" },
        primary: { DEFAULT: "hsl(199 89% 48%)", foreground: "hsl(222 47% 8%)" },
        // Okabe–Ito severity ramp (colour-blind safe).
        severity: {
          info: "#56B4E9", // sky blue
          warning: "#E69F00", // orange
          critical: "#D55E00", // vermillion
          ok: "#009E73", // bluish green
          unknown: "#999999",
        },
        // Throughput / jam ramp (also CB-safe).
        flow: {
          good: "#009E73",
          warn: "#E69F00",
          bad: "#D55E00",
        },
      },
      borderRadius: { lg: "0.5rem", md: "0.375rem", sm: "0.25rem" },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
} satisfies Config;
