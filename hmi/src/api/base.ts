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
 *      (`?api=http://192.168.8.224:5000`, or just `?api=5000`). Anyone who can
 *      send the operator a link can set it, so it is used only when it names a
 *      host `hosts.ts` allows: this page's host, a private-network, loopback
 *      or link-local IP literal, a `.local` or single-label name, or a host on
 *      the deploy-time allow list (`<meta name="dda-api-allow-hosts">`,
 *      `VITE_API_ALLOW_HOSTS`). Any other host is ignored: resolution
 *      continues with the next source, and `getApiBaseNotice()` gives the page
 *      a notice naming the ignored host.
 *   2. `<meta name="dda-api-base" content="...">` in the host HTML — editable
 *      in a BUILT `dist/` without rebuilding, which is what an operator
 *      deploying the bundle by hand wants
 *   3. the build-time `VITE_API_BASE`
 *   4. same-origin (the empty string) — the pre-existing behaviour, so a
 *      bundle served by the LocalServer mount is byte-for-byte unaffected
 *
 * Sources 2-4 and the `__DDA_HMI_API_BASE__` global are deploy-time inputs
 * and are not host-checked.
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

import {
  checkApiBaseHost,
  parseApiHostAllowList,
  type ApiHostRejectionReason,
} from "./hosts";

/** The window-level hook tests and embedders can set directly. */
export const API_BASE_GLOBAL = "__DDA_HMI_API_BASE__";

/** `<meta name="...">` carrying the base in a built `dist/`. */
export const API_BASE_META_NAME = "dda-api-base";

/** `<meta name="...">` listing the extra hosts `?api=` may name (`hosts.ts`). */
export const API_ALLOW_HOSTS_META_NAME = "dda-api-allow-hosts";

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

/** The `content` of `<meta name="...">` in the host document, when present. */
function metaContent(name: string, doc?: Document): string | null {
  const d = doc ?? (typeof document !== "undefined" ? document : undefined);
  if (d === undefined) return null;
  const meta = d.querySelector(`meta[name="${name}"]`);
  const content = meta?.getAttribute("content");
  return content === null || content === undefined ? null : content;
}

/** Reads `<meta name="dda-api-base" content="...">`, when present. */
export function apiBaseFromMeta(doc?: Document): string | null {
  return metaContent(API_BASE_META_NAME, doc);
}

/** Reads `<meta name="dda-api-allow-hosts" content="...">`, when present. */
export function apiAllowHostsFromMeta(doc?: Document): string | null {
  return metaContent(API_ALLOW_HOSTS_META_NAME, doc);
}

/** A build-time `VITE_*` value, or null when it was not defined. */
function buildTimeValue(name: string): string | null {
  const env = import.meta.env as unknown as Record<string, unknown> | undefined;
  const value = env?.[name];
  return typeof value === "string" ? value : null;
}

export interface ResolveApiBaseInputs {
  /** Page query string (defaults to `location.search`). */
  search?: string;
  /** Host document (defaults to the global `document`). */
  doc?: Document;
  /** Build-time value (defaults to `VITE_API_BASE`). */
  buildTime?: string | null;
  /** Build-time `?api=` allow list (defaults to `VITE_API_ALLOW_HOSTS`). */
  buildTimeAllowHosts?: string | null;
  /** Page scheme/host for the bare-port form and the `?api=` host check. */
  pageProtocol?: string;
  pageHostname?: string;
}

/** A `?api=` value that was present but not used. */
export interface ApiBaseRejection {
  reason: ApiHostRejectionReason;
  /** The host it named, as the URL parser normalized it; null when none. */
  host: string | null;
}

export interface ApiBaseResolution {
  /** The API_Base (see the accepted forms above). */
  base: string;
  /** Set when a `?api=` value was ignored because of its host. */
  rejected: ApiBaseRejection | null;
}

/**
 * The resolved API_Base, following the documented precedence, plus the
 * `?api=` value the host policy ignored, if any. Pure with respect to its
 * inputs, so the precedence itself is directly testable.
 */
export function resolveApiBaseDetailed(
  inputs: ResolveApiBaseInputs = {},
): ApiBaseResolution {
  let rejected: ApiBaseRejection | null = null;

  const fromQuery = apiBaseFromSearch(inputs.search ?? globalThis.location?.search);
  if (fromQuery !== null) {
    const normalized = normalizeApiBase(
      fromQuery,
      inputs.pageProtocol,
      inputs.pageHostname,
    );
    // A value normalizeApiBase cannot use falls through silently, as before.
    if (normalized !== "") {
      const allow = parseApiHostAllowList(
        apiAllowHostsFromMeta(inputs.doc),
        inputs.buildTimeAllowHosts !== undefined
          ? inputs.buildTimeAllowHosts
          : buildTimeValue("VITE_API_ALLOW_HOSTS"),
      );
      const page = {
        protocol: inputs.pageProtocol ?? globalThis.location?.protocol ?? "http:",
        hostname: inputs.pageHostname ?? globalThis.location?.hostname ?? "",
      };
      const check = checkApiBaseHost(normalized, page, allow);
      if (check.ok) return { base: normalized, rejected: null };
      rejected = { reason: check.reason, host: check.host };
    }
  }

  const deployTime: Array<string | null> = [
    apiBaseFromMeta(inputs.doc),
    inputs.buildTime !== undefined ? inputs.buildTime : buildTimeValue("VITE_API_BASE"),
  ];
  for (const candidate of deployTime) {
    if (candidate === null) continue;
    const normalized = normalizeApiBase(
      candidate,
      inputs.pageProtocol,
      inputs.pageHostname,
    );
    // A present-but-unusable candidate does not veto the next one; only a
    // usable value stops the search. An explicitly empty value means
    // same-origin, which is also the fallthrough, so either way "" is right.
    if (normalized !== "") return { base: normalized, rejected };
  }
  return { base: "", rejected };
}

/** The resolved API_Base alone (see `resolveApiBaseDetailed`). */
export function resolveApiBase(inputs: ResolveApiBaseInputs = {}): string {
  return resolveApiBaseDetailed(inputs).base;
}

// --------------------------------------------------------------------------
// Module state
// --------------------------------------------------------------------------

let resolution: ApiBaseResolution | null = null;

function currentResolution(): ApiBaseResolution {
  if (resolution === null) {
    const injected = (globalThis as Record<string, unknown>)[API_BASE_GLOBAL];
    resolution =
      typeof injected === "string"
        ? { base: normalizeApiBase(injected), rejected: null }
        : resolveApiBaseDetailed();
  }
  return resolution;
}

/**
 * The API_Base every builder in `routes.ts` prefixes. Resolved once on first
 * use (or by an explicit `setApiBase`) and cached, so a mid-session change to
 * the URL cannot make two requests disagree about their origin.
 */
export function getApiBase(): string {
  return currentResolution().base;
}

/** The `?api=` value the host policy ignored for this page, if any. */
export function getApiBaseRejection(): ApiBaseRejection | null {
  return currentResolution().rejected;
}

/** Overrides the API_Base (embedders and tests). */
export function setApiBase(base: string): void {
  resolution = { base: normalizeApiBase(base), rejected: null };
}

/** Clears the cache so the next `getApiBase()` re-resolves (test isolation). */
export function resetApiBase(): void {
  resolution = null;
}

// --------------------------------------------------------------------------
// The API origin: what a session is bound to and the login form names
// --------------------------------------------------------------------------

/**
 * The origin (`scheme://host:port`) a request URL goes to, resolved against
 * the page the way `fetch` and `<img src>` resolve it. "" when it cannot be
 * determined: a relative URL with no page location (the node test
 * environment), or a URL the parser rejects, which no request can use either.
 */
export function requestOrigin(
  url: string,
  pageHref: string | undefined = globalThis.location?.href,
): string {
  try {
    return new URL(url, pageHref).origin;
  } catch {
    return "";
  }
}

/** The LocalServer origin every `routes.ts` URL goes to. */
export function getApiOrigin(): string {
  return requestOrigin(`${getApiBase()}/`);
}

/**
 * `host:port` of the LocalServer the HMI talks to, port always spelled out:
 * what the login form shows as the place the credentials are sent. "" when
 * it cannot be determined.
 */
export function getApiAddress(): string {
  let url: URL;
  try {
    url = new URL(getApiOrigin());
  } catch {
    return "";
  }
  if (url.hostname === "") return "";
  const port = url.port !== "" ? url.port : url.protocol === "https:" ? "443" : "80";
  return `${url.hostname}:${port}`;
}

/**
 * The text of the visible, non-blocking notice shown when a `?api=` value was
 * ignored: the host it named, why, and where the HMI connects instead.
 */
export function apiBaseNoticeText(
  rejected: ApiBaseRejection,
  usingAddress: string,
): string {
  const named = rejected.host === null ? "?api=" : `?api= host "${rejected.host}"`;
  const why =
    rejected.reason === "userinfo"
      ? "addresses with a user name or password are not accepted"
      : rejected.reason === "invalid"
        ? "the value is not a valid address"
        : "it is not this page's host, a private-network address or an allowed host";
  const instead = usingAddress === "" ? "" : ` Using ${usingAddress} instead.`;
  return `Ignored ${named}: ${why}.${instead}`;
}

/** The notice for this page's ignored `?api=` value, or null when there is none. */
export function getApiBaseNotice(): string | null {
  const rejected = getApiBaseRejection();
  return rejected === null ? null : apiBaseNoticeText(rejected, getApiAddress());
}
