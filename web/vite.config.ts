import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// In dev, /api and /api/ws are proxied to the gateway (host 8000) so the SPA
// talks to it same-origin. In the nginx production image the same proxying is
// done by nginx (see nginx/default.conf), so the app always calls relative /api.
const GATEWAY = process.env.VITE_GATEWAY_URL || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
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
});
