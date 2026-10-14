import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the Triple_HMI layout suite (task 13.1).
 *
 * The suite measures the built kiosk page in a real browser engine, which is
 * the only place the layout requirements of Requirement 6 (and the equal-height
 * / uncropped image rules of Requirement 5.3) can actually be checked: they are
 * statements about *rendered* geometry, so jsdom (where the rest of the HMI's
 * DOM tests run) cannot decide them — it has no layout engine.
 *
 * Two deliberate choices keep this suite out of the way of everything else:
 *
 *  - **`.spec.ts`, not `.test.ts`.** Vitest's `include` globs are
 *    `src/**\/*.test.ts` and `test/**\/*.test.ts`, so `triple-layout.spec.ts`
 *    is invisible to `npm test` and this runner never sees a Vitest file.
 *  - **No web server.** The spec serves the built `dist/` and every stubbed
 *    LocalServer response through Playwright's request interception, so the
 *    suite needs no port, no LocalServer, and no device (see the spec's
 *    header). `globalSetup` produces `dist/` once per run.
 *
 * Chromium only, headless: the kiosk requirement is a Chromium-based browser in
 * full-screen kiosk mode (Requirement 6.7), and viewports are set per test
 * group rather than here because the suite measures 1920x1080 and 1280 widths
 * separately (Requirements 6.1, 6.5).
 */
export default defineConfig({
  testDir: "./test",
  testMatch: /.*\.spec\.ts$/,
  // Layout measurement is geometry, not timing: no retries, so a failure is
  // always a real layout regression rather than a flake to be re-rolled.
  retries: 0,
  // One worker sharing one build: the tests only read the page they render.
  workers: 1,
  fullyParallel: false,
  reporter: process.env.CI !== undefined ? [["list"], ["github"]] : [["list"]],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  globalSetup: "./test/playwright-global-setup.ts",
  use: {
    ...devices["Desktop Chrome"],
    headless: true,
    // Every request is intercepted, so the origin is a routing label only.
    baseURL: "http://kiosk.test",
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
