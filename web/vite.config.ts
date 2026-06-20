import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// In dev, /api and /api/ws are proxied to the gateway (host 8000) so the SPA
// talks to it same-origin. In the nginx production image the same proxying is
// done by nginx (see nginx/default.conf), so the app always calls relative /api.
export default defineConfig(({ command, mode }) => {
  // Load .env / .env.<mode> (all keys, not just VITE_) so config can read them.
  const env = loadEnv(mode, process.cwd(), "");
  const GATEWAY = env.VITE_GATEWAY_URL || "http://localhost:8000";

  // ---- Data-adapter mode is decided HERE, at build time ------------------
  // Production builds (`vite build`) compile to the LiveAdapter and REFUSE
  // mock unless an explicit local escape hatch (VITE_ALLOW_MOCK=true) is set.
  // Dev/serve is configurable and defaults to mock for zero-credential demos.
  // The result is inlined as a compile-time constant so the unused adapter is
  // dead-code-eliminated (MockAdapter is tree-shaken out of production bundles).
  const isProdBuild = command === "build";
  const requested = (env.VITE_DATA_MODE || "").toLowerCase();
  const allowMock = (env.VITE_ALLOW_MOCK || "").toLowerCase() === "true";
  const dataMode: "live" | "mock" = isProdBuild
    ? requested === "mock" && allowMock
      ? "mock"
      : "live"
    : requested === "live"
      ? "live"
      : "mock";

  if (isProdBuild && dataMode === "mock") {
    // Loud signal in CI logs that a NON-shippable bundle is being produced.
    console.warn(
      "[vite] WARNING: building a PRODUCTION bundle in MOCK mode (VITE_ALLOW_MOCK=true). " +
        "Do NOT deploy this image.",
    );
  }

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "./src") },
    },
    define: {
      __JNPA_DATA_MODE__: JSON.stringify(dataMode),
      // Single greppable string literal baked into the bundle so a deploy guard
      // can assert the shipped JS is live (see web/Dockerfile + verify script).
      __JNPA_DATA_MODE_MARKER__: JSON.stringify(`JNPA_DATA_MODE:${dataMode}`),
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/api/ws": { target: GATEWAY.replace(/^http/, "ws"), ws: true, changeOrigin: true },
        "/api": { target: GATEWAY, changeOrigin: true },
      },
    },
    build: { outDir: "dist", sourcemap: false },
  };
});
