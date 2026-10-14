/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

/**
 * API_Base resolution — where the HMI sends its LocalServer requests.
 *
 * The bundle was originally served by the LocalServer's own `/hmi` mount, so
 * every URL in `routes.ts` was root-relative and same-origin was implied. To
 * let the HMI be hosted detached from the LocalServer (any static server, any
 * port, or a different machine entirely) the origin has to become an input.
 *
 * Resolution order, first usable wins:
 *
 *   1. the `api` query parameter of the page URL — per-URL, nothing to edit
 *      (`?api=http://192.168.8.224:5000`, or just `?api=5000`)
 *   2. `<meta name="dda-api-base" content="...">` in the host HTML — editable
 *      in a BUILT `dist/` without rebuilding, which is what an operator
 *      deploying the bundle by hand wants
 *   3. the build-time `VITE_API_BASE`
 *   4. same-origin (the empty string) — the pre-existing behaviour, so a
 *      bundle served by the LocalServer mount is byte-for-byte unaffected
 *
 * Everything here is synchronous on purpose: an async config fetch would have
 * to be awaited before the first request, adding a startup ordering hazard to
 * every entry point. A meta tag and a query parameter are both available the
 * moment the document parses.
 *
 * Accepted forms (`normalizeApiBase`):
 *
 *   ""                        -> same-origin
 *   "5000"                    -> same host, that port (scheme from the page)
 *   "//host:5000"             -> protocol-relative
 *   "http://host:5000"        -> absolute origin
 *   ".../trailing/slash/"     -> trailing slashes stripped
 *
 * A value that cannot be understood resolves to same-origin rather than
 * throwing: a malformed override must not brick the kiosk, and same-origin is
 * the behaviour the device has always had.
 */

/** The window-level hook tests and embedders can set directly. */
export const API_BASE_GLOBAL = "__DDA_HMI_API_BASE__";

/** `<meta name="...">` carrying the base in a built `dist/`. */
export const API_BASE_META_NAME = "dda-api-base";

/** The page-URL query parameter that overrides everything else. */
export const API_BASE_QUERY_PARAM = "api";

/**
 * Normalize an API_Base candidate to either "" (same-origin) or an origin with
 * no trailing slash. See the accepted forms in the module docstring.
 *
 * `pageProtocol` / `pageHostname` supply the pieces a bare port needs; they
 * default to the current document's, and are injectable for tests.
 */
export function normalizeApiBase(
  raw: unknown,
  pageProtocol?: string,
  pageHostname?: string,
): string {
  if (typeof raw !== "string") return "";
  const value = raw.trim();
  if (value === "") return "";

  // A bare port number: keep the page's scheme and host, swap the port. This
  // is the common case for "HMI on 8081, API on 5000, same box".
  if (/^\d{1,5}$/.test(value)) {
    const port = Number(value);
    if (port <= 0 || port > 65535) return "";
    const protocol =
      pageProtocol ?? globalThis.location?.protocol ?? "http:";
    const hostname = pageHostname ?? globalThis.location?.hostname ?? "";
    if (hostname === "") return "";
    return `${protocol}//${hostname}:${port}`;
  }

  // Anything else must be an absolute or protocol-relative origin. Reject
  // values that are neither, rather than silently producing a broken path.
  if (!/^(https?:)?\/\//i.test(value)) return "";

  const stripped = value.replace(/\/+$/, "");
  // Guard against a value that was nothing but slashes.
  return /^(https?:)?\/\/[^/]/i.test(stripped) ? stripped : "";
}

/** Reads the `api` query parameter from a search string. */
export function apiBaseFromSearch(search: string | undefined): string | null {
  if (typeof search !== "string" || search === "") return null;
  const value = new URLSearchParams(search).get(API_BASE_QUERY_PARAM);
  return value === null ? null : value;
}

/** Reads `<meta name="dda-api-base" content="...">`, when present. */
export function apiBaseFromMeta(doc?: Document): string | null {
  const d = doc ?? (typeof document !== "undefined" ? document : undefined);
  if (d === undefined) return null;
  const meta = d.querySelector(`meta[name="${API_BASE_META_NAME}"]`);
  const content = meta?.getAttribute("content");
  return content === null || content === undefined ? null : content;
}

/** The build-time `VITE_API_BASE`, or null when it was not defined. */
function apiBaseFromBuild(): string | null {
  const env = import.meta.env as unknown as Record<string, unknown> | undefined;
  const value = env?.["VITE_API_BASE"];
  return typeof value === "string" ? value : null;
}

export interface ResolveApiBaseInputs {
  /** Page query string (defaults to `location.search`). */
  search?: string;
  /** Host document (defaults to the global `document`). */
  doc?: Document;
  /** Build-time value (defaults to `VITE_API_BASE`). */
  buildTime?: string | null;
  /** Page scheme/host for the bare-port form. */
  pageProtocol?: string;
  pageHostname?: string;
}

/**
 * The resolved API_Base, following the documented precedence. Pure with
 * respect to its inputs, so the precedence itself is directly testable.
 */
export function resolveApiBase(inputs: ResolveApiBaseInputs = {}): string {
  const candidates: Array<string | null> = [
    apiBaseFromSearch(inputs.search ?? globalThis.location?.search),
    apiBaseFromMeta(inputs.doc),
    inputs.buildTime !== undefined ? inputs.buildTime : apiBaseFromBuild(),
  ];
  for (const candidate of candidates) {
    if (candidate === null) continue;
    const normalized = normalizeApiBase(
      candidate,
      inputs.pageProtocol,
      inputs.pageHostname,
    );
    // A present-but-unusable candidate does not veto the next one; only a
    // usable value stops the search. An explicitly empty value means
    // same-origin, which is also the fallthrough, so either way "" is right.
    if (normalized !== "") return normalized;
  }
  return "";
}

// --------------------------------------------------------------------------
// Module state
// --------------------------------------------------------------------------

let apiBase: string | null = null;

/**
 * The API_Base every builder in `routes.ts` prefixes. Resolved once on first
 * use (or by an explicit `setApiBase`) and cached, so a mid-session change to
 * the URL cannot make two requests disagree about their origin.
 */
export function getApiBase(): string {
  if (apiBase === null) {
    const injected = (globalThis as Record<string, unknown>)[API_BASE_GLOBAL];
    apiBase =
      typeof injected === "string"
        ? normalizeApiBase(injected)
        : resolveApiBase();
  }
  return apiBase;
}

/** Overrides the API_Base (embedders and tests). */
export function setApiBase(base: string): void {
  apiBase = normalizeApiBase(base);
}

/** Clears the cache so the next `getApiBase()` re-resolves (test isolation). */
export function resetApiBase(): void {
  apiBase = null;
}
