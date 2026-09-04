import { build } from "vite";

/**
 * Global setup for the Triple_HMI layout suite (task 13.1).
 *
 * The suite measures the **built** kiosk page — the same static bundle the
 * LocalServer's `/hmi` mount serves (Requirement 6.6) — so the build runs once
 * per suite run against the project's real `vite.config.ts`, emitting both
 * entries into `dist/` (`dist/index.html` and `dist/triple.html`). The spec then
 * serves those files straight from disk through request interception.
 *
 * `dist/` is a build artifact (gitignored), so writing it here is safe.
 */
export default async function globalSetup(): Promise<void> {
  const projectRoot = new URL("..", import.meta.url).pathname;
  await build({
    root: projectRoot,
    configFile: `${projectRoot}vite.config.ts`,
    logLevel: "warn",
  });
}
