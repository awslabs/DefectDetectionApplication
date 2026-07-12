/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';
import { execSync } from 'child_process';
import { readFileSync } from 'fs';

// Resolve build metadata for the portal version banner. These are baked in at
// build time via `define` so the running app can display exactly which build
// it is. Fall back gracefully when git/package.json are unavailable.
function readPkgVersion(): string {
  try {
    return JSON.parse(readFileSync(path.resolve(__dirname, 'package.json'), 'utf-8')).version || '0.0.0';
  } catch {
    return '0.0.0';
  }
}
function gitShort(): string {
  // Allow CI to override (e.g. when building from a tarball without .git).
  if (process.env.VITE_GIT_SHA) return process.env.VITE_GIT_SHA;
  try {
    return execSync('git rev-parse --short HEAD', { cwd: __dirname }).toString().trim();
  } catch {
    return 'unknown';
  }
}
const BUILD_VERSION = process.env.VITE_BUILD_VERSION || readPkgVersion();
const BUILD_SHA = gitShort();
const BUILD_TIME = process.env.VITE_BUILD_TIME || new Date().toISOString();

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(BUILD_VERSION),
    __APP_GIT_SHA__: JSON.stringify(BUILD_SHA),
    __APP_BUILD_TIME__: JSON.stringify(BUILD_TIME),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    open: true,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  // Vitest configuration. `environment: 'jsdom'` gives component tests a DOM;
  // the setup file registers `@testing-library/jest-dom` matchers. Vite's
  // production build ignores the `test` field, so this does not affect builds.
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
});
