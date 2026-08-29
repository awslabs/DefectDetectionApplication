import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "test/**/*.test.ts"],
    // Configures fast-check globally (minimum 100 runs per property).
    setupFiles: ["test/setup.ts"],
  },
});
