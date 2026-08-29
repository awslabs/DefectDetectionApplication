import { defineConfig } from "vite";

// The HMI is served same-origin by the LocalServer FastAPI app, mounted at
// /hmi (see design.md, Design Decision 2). The build output is a plain
// static-asset bundle (Requirement 6.7): no server-side rendering, no
// runtime framework.
export default defineConfig({
  base: "/hmi/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Static assets only; keep the bundle inspectable on the device.
    sourcemap: true,
    target: "es2020",
  },
});
