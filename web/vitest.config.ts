import { defineConfig } from "vitest/config";
import path from "node:path";

// Node-environment Vitest config for the data-adapter contract test. Mirrors the
// `@` -> ./src alias from vite.config.ts so the mock's `@/lib/types` imports
// resolve identically to the app build.
export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
