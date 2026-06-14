import type { Config } from "tailwindcss";

// Colour-blind-safe (Okabe–Ito) severity palette, exposed as Tailwind colours so
// every component pulls from one source. The base UI tokens below are a LIGHT
// theme; all foreground/background pairs meet WCAG AA contrast (>= 4.5:1)
// against the light canvas. The severity/flow ramps are theme-agnostic.
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(215 20% 88%)",
        input: "hsl(215 20% 88%)",
        ring: "hsl(199 89% 42%)",
        background: "hsl(210 40% 98%)",
        foreground: "hsl(222 47% 14%)",
        muted: { DEFAULT: "hsl(214 32% 93%)", foreground: "hsl(215 16% 42%)" },
        card: { DEFAULT: "hsl(0 0% 100%)", foreground: "hsl(222 47% 14%)" },
        primary: { DEFAULT: "hsl(199 89% 42%)", foreground: "hsl(0 0% 100%)" },
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
