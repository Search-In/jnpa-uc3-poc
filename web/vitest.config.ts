import { defineConfig } from "vitest/config";
import path from "node:path";

// Node-environment Vitest config for the data-adapter contract test. Mirrors the
// `@` -> ./src alias from vite.config.ts so the mock's `@/lib/types` imports
// resolve identically to the app build.
export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  // Mirror the build-time globals injected by vite.config.ts so `@/data`
  // resolves a mode in the Node test context (the adapter-contract test
  // exercises the MockAdapter, so tests run in mock mode).
  define: {
    __JNPA_DATA_MODE__: JSON.stringify("mock"),
    __JNPA_DATA_MODE_MARKER__: JSON.stringify("JNPA_DATA_MODE:mock"),
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
