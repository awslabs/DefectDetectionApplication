import { beforeAll, describe, expect, it } from "vitest";
import { build } from "vite";
import type { OutputAsset, OutputChunk, RollupOutput } from "rollup";

/**
 * Build smoke test for the multi-entry Vite output (task 3.2).
 *
 * The IMTS Triple Inspection HMI is a second entry point of the existing `hmi/`
 * project, built into the same `dist` and served by the LocalServer's existing
 * `/hmi` static mount with no backend serving change (Requirement 6.6). The
 * output must stay a pure static-asset bundle with no server-side entry, so the
 * kiosk browser needs nothing installed on the device beyond the served files
 * (Requirements 6.6, 6.7).
 *
 * The build runs in memory (`write: false`) against the project's real
 * `vite.config.ts`, so the checked-in `hmi/dist` and the existing entry's
 * bundle are left untouched.
 */

const projectRoot = new URL("..", import.meta.url).pathname;

/** File extensions a static-asset-only bundle may contain. */
const STATIC_EXTENSIONS = [".html", ".js", ".css", ".map", ".svg", ".png", ".ico", ".woff2"];

/** Output names that would indicate a server-side (SSR) entry. */
const SERVER_ENTRY_PATTERN = /(^|[.\-/])(ssr|entry-server|server)([.\-]|$)/i;

let output: (OutputChunk | OutputAsset)[];
let emitted: string[];

/** Returns the emitted file's text, for HTML and other text assets. */
function assetSource(fileName: string): string {
  const asset = output.find((item) => item.fileName === fileName);
  if (asset === undefined) throw new Error(`no emitted file named ${fileName}`);
  if (asset.type !== "asset") throw new Error(`${fileName} is not an emitted asset`);
  return typeof asset.source === "string"
    ? asset.source
    : new TextDecoder().decode(asset.source);
}

/** Extracts the `src`/`href` URLs an HTML document references. */
function referencedUrls(html: string): string[] {
  const urls: string[] = [];
  const pattern = /(?:src|href)="([^"]+)"/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(html)) !== null) urls.push(match[1] as string);
  return urls;
}

beforeAll(async () => {
  const result = await build({
    root: projectRoot,
    configFile: `${projectRoot}vite.config.ts`,
    logLevel: "silent",
    build: { write: false },
  });
  // A static build produces exactly one rollup output set: no SSR pass (which
  // would yield an array of outputs) and no watcher.
  expect(Array.isArray(result)).toBe(false);
  expect("close" in (result as object)).toBe(false);
  output = (result as RollupOutput).output as (OutputChunk | OutputAsset)[];
  emitted = output.map((item) => item.fileName);
}, 180_000);

describe("multi-entry HMI build", () => {
  it("emits both the existing index.html and the new triple.html entries", () => {
    expect(emitted).toContain("index.html");
    expect(emitted).toContain("triple.html");
  });

  it("emits a distinct JavaScript bundle per entry", () => {
    const entryChunks = output.filter(
      (item): item is OutputChunk => item.type === "chunk" && item.isEntry,
    );
    expect(entryChunks.map((chunk) => chunk.name).sort()).toEqual(["index", "triple"]);
    const entryFiles = new Set(entryChunks.map((chunk) => chunk.fileName));
    expect(entryFiles.size).toBe(2);
    for (const fileName of entryFiles) expect(emitted).toContain(fileName);
  });

  it("produces static assets only, with no server-side entry", () => {
    for (const file of emitted) {
      const extension = file.slice(file.lastIndexOf("."));
      expect(STATIC_EXTENSIONS, `unexpected non-static output file: ${file}`).toContain(
        extension,
      );
      expect(SERVER_ENTRY_PATTERN.test(file), `server-side entry emitted: ${file}`).toBe(
        false,
      );
    }
    expect(emitted).not.toContain("ssr-manifest.json");
    expect(emitted.some((file) => file.endsWith(".cjs"))).toBe(false);
  });

  it("resolves every asset URL of both entries under /hmi/", () => {
    for (const entry of ["index.html", "triple.html"]) {
      const urls = referencedUrls(assetSource(entry)).filter(
        (url) => !/^(https?:|data:|#|\/\/)/.test(url),
      );
      expect(urls.length, `${entry} references no bundled assets`).toBeGreaterThan(0);
      for (const url of urls) {
        expect(url.startsWith("/hmi/"), `${entry} asset URL outside /hmi/: ${url}`).toBe(
          true,
        );
        expect(emitted, `${entry} references a missing asset: ${url}`).toContain(
          url.slice("/hmi/".length),
        );
      }
    }
  });
});
