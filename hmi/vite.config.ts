import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// The HMI is served same-origin by the LocalServer FastAPI app, mounted at
// /hmi (see design.md, Design Decision 2). The build output is a plain
// static-asset bundle (Requirement 6.7): no server-side rendering, no
// runtime framework.
//
// Two entries share this project and this dist (imts-triple-inspection-hmi
// Design Decision 1, Requirement 6.6):
//
//   index.html  -> the single-inspection Quality Station HMI (unchanged)
//   triple.html -> the IMTS Triple Inspection HMI kiosk (/hmi/triple.html)
//
// Both are plain HTML entries of the same multi-page build, so the existing
// entry keeps building exactly as before and no backend serving code changes.
export default defineConfig({
  base: "/hmi/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Static assets only; keep the bundle inspectable on the device.
    sourcemap: true,
    target: "es2020",
    rollupOptions: {
      input: {
        index: fileURLToPath(new URL("index.html", import.meta.url)),
        triple: fileURLToPath(new URL("triple.html", import.meta.url)),
      },
    },
  },
});
