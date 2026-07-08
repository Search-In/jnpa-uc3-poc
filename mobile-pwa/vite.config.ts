import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import path from "node:path";

// The PWA is served under /pwa (the web/ nginx image mounts the built bundle
// there so an evaluator can open it at http://localhost:3000/pwa). The base
// must match so the service-worker scope, asset URLs, and manifest start_url
// are all rooted at /pwa/. In dev (`make dev-pwa`) it runs standalone on :3001
// with the same base; /api is proxied to the gateway.
const BASE = process.env.PWA_BASE || "/pwa/";
const GATEWAY = process.env.PWA_GATEWAY_URL || "http://localhost:8000";

export default defineConfig({
  base: BASE,
  plugins: [
    react(),
    VitePWA({
      // We hand-write the service worker (src/sw.ts) so it can handle WebPush
      // `push` / `notificationclick` events; Workbox still injects the precache
      // manifest into it via injectManifest.
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "autoUpdate",
      injectRegister: null, // we register the SW ourselves in lib/pwa.ts
      devOptions: { enabled: true, type: "module", navigateFallback: `${BASE}index.html` },
      manifest: {
        id: BASE,
        name: "JNPA Trucking — Driver Advisory",
        short_name: "JNPA Truck",
        description:
          "Driver-side ETA, gate slot and live re-route advisory for the JNPA UC-III port corridor.",
        start_url: BASE,
        scope: BASE,
        display: "standalone",
        orientation: "portrait",
        background_color: "#f4f7fb",
        theme_color: "#f4f7fb",
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,svg,png}"],
      },
    }),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    host: "0.0.0.0",
    port: 3001,
    proxy: {
      "/api/ws": { target: GATEWAY.replace(/^http/, "ws"), ws: true, changeOrigin: true },
      "/api": { target: GATEWAY, changeOrigin: true },
    },
  },
  // `vite preview` (serving the built bundle) also proxies /api -> gateway so a
  // production-preview of the PWA fetches real data without a separate nginx.
  preview: {
    host: "0.0.0.0",
    port: 3001,
    proxy: {
      "/api/ws": { target: GATEWAY.replace(/^http/, "ws"), ws: true, changeOrigin: true },
      "/api": { target: GATEWAY, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false, target: "es2020" },
});
