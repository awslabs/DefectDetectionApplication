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
 * Host policy for the page URL's `?api=` override.
 *
 * `?api=` is the one API_Base source anyone can set: sending a link is enough.
 * The HMI sends its Session_Token with every request to the API_Base and posts
 * the login form's credentials there, so the hosts `?api=` may name are
 * limited. The deploy-time sources (the `dda-api-base` meta tag,
 * `VITE_API_BASE`, the `__DDA_HMI_API_BASE__` global) are set by whoever
 * deploys the bundle and are not checked here.
 *
 * Stations are reached across hosts on the plant network, usually by IP
 * address, so `?api=` can still name another host, as long as it is one of:
 *
 *   - the page's own host, on any port (a bare `?api=5000` always lands here)
 *   - an IPv4 literal in 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
 *     127.0.0.0/8 or 169.254.0.0/16
 *   - an IPv6 literal in ::1/128, fc00::/7 or fe80::/10
 *   - a name ending in `.local` (mDNS)
 *   - a single-label name such as `station-01` (no dots)
 *   - a host on the deploy-time allow list (`parseApiHostAllowList`)
 *
 * 100.64.0.0/10 (RFC 6598 shared address space: carrier-grade NAT, and the
 * address range of some VPNs such as Tailscale) is OUT of the built-in set.
 * Addresses in it can belong to other customers of the same carrier, so the
 * range is not the plant network by default. A site that reaches stations
 * through such a VPN adds `100.64.0.0/10`, or a narrower range, to the allow
 * list. IPv4-mapped IPv6 forms (`[::ffff:192.168.1.10]`) are not in the list
 * either; use the IPv4 literal.
 *
 * Every decision is made on the hostname as `new URL(...)` normalizes it,
 * which is the host `fetch` and `<img src>` connect to. Decimal, hex, octal and
 * short IPv4 forms become a dotted quad (`http://3232235786` is 192.168.1.10),
 * IPv6 becomes its bracketed compressed form, and names are lower-cased and
 * IDNA-mapped. A host whose last label is not numeric, such as
 * `192.168.1.10.example.com`, is a DNS name and gets no IP-range treatment.
 * A value with user info (`user@host`) is rejected whatever its host.
 */

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

/** An IPv4 range as a 32-bit network address and mask. */
interface Ipv4Range {
  network: number;
  mask: number;
}

/** The parsed deploy-time allow list (`parseApiHostAllowList`). */
export interface ApiHostAllowList {
  /** Exact hostnames and IP literals, URL-normalized, without a trailing dot. */
  exact: ReadonlySet<string>;
  /** `.suffix` entries without the leading dot. */
  suffixes: readonly string[];
  /** IPv4 CIDR entries. */
  ranges: readonly Ipv4Range[];
}

/** Why a `?api=` value is not used. */
export type ApiHostRejectionReason =
  | "host-not-allowed" // the host is outside the policy above
  | "userinfo" // the value carries `user@` or `user:password@`
  | "invalid"; // the URL parser rejects the value, so it names no host

export type ApiHostCheck =
  | { ok: true }
  | {
      ok: false;
      reason: ApiHostRejectionReason;
      /** The host the value named, URL-normalized; null when it named none. */
      host: string | null;
    };

/** The page whose `?api=` is being checked. */
export interface ApiHostPage {
  /** `location.protocol`; completes a protocol-relative `//host` value. */
  protocol: string;
  /** `location.hostname`; "" when the page has no host. */
  hostname: string;
}

// --------------------------------------------------------------------------
// Address parsing (inputs are already URL-normalized)
// --------------------------------------------------------------------------

/** Drops one trailing dot (the absolute DNS form): `printer.local.` → `printer.local`. */
function withoutTrailingDot(host: string): string {
  return host.endsWith(".") ? host.slice(0, -1) : host;
}

/** True for `a..b` or `.local`: names with an empty label are not hosts. */
function hasEmptyLabel(host: string): boolean {
  return host.split(".").some((label) => label === "");
}

/** The 32-bit value of a URL-normalized dotted-quad hostname, or null. */
function parseIpv4(host: string): number | null {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (match === null) return null;
  const octets = match.slice(1).map(Number);
  if (octets.some((octet) => octet > 255)) return null;
  return octets.reduce((value, octet) => value * 256 + octet, 0);
}

/** The eight 16-bit pieces of a URL-normalized `[...]` IPv6 hostname, or null. */
function parseIpv6(host: string): number[] | null {
  const text = /^\[([0-9a-f:]+)\]$/.exec(host)?.[1];
  if (text === undefined) return null;
  const halves = text.split("::");
  if (halves.length > 2) return null;
  const pieces = (part: string): number[] =>
    part === ""
      ? []
      : part
          .split(":")
          .map((piece) => (/^[0-9a-f]{1,4}$/.test(piece) ? parseInt(piece, 16) : Number.NaN));
  const head = pieces(halves[0] ?? "");
  const tail = halves.length === 2 ? pieces(halves[1] ?? "") : [];
  if ([...head, ...tail].some(Number.isNaN)) return null;
  if (halves.length === 1) return head.length === 8 ? head : null;
  const zeros = 8 - head.length - tail.length;
  if (zeros < 1) return null;
  return [...head, ...new Array<number>(zeros).fill(0), ...tail];
}

function ipv4Range(address: number, prefix: number): Ipv4Range {
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return { network: (address & mask) >>> 0, mask };
}

function inRange(address: number, range: Ipv4Range): boolean {
  return ((address & range.mask) >>> 0) === range.network;
}

/** The built-in IPv4 ranges; 100.64.0.0/10 is deliberately absent (see above). */
const BUILT_IN_IPV4_RANGES: readonly Ipv4Range[] = [
  ipv4Range(0x0a000000, 8), // 10.0.0.0/8 (RFC 1918)
  ipv4Range(0xac100000, 12), // 172.16.0.0/12 (RFC 1918)
  ipv4Range(0xc0a80000, 16), // 192.168.0.0/16 (RFC 1918)
  ipv4Range(0x7f000000, 8), // 127.0.0.0/8 (loopback)
  ipv4Range(0xa9fe0000, 16), // 169.254.0.0/16 (link-local)
];

/** ::1/128 (loopback), fc00::/7 (unique local) or fe80::/10 (link-local). */
function isBuiltInIpv6(pieces: readonly number[]): boolean {
  const first = pieces[0] ?? 0;
  const loopback = pieces.every((piece, index) => piece === (index === 7 ? 1 : 0));
  return loopback || (first & 0xfe00) === 0xfc00 || (first & 0xffc0) === 0xfe80;
}

// --------------------------------------------------------------------------
// Deploy-time allow list
// --------------------------------------------------------------------------

/**
 * Normalizes one allow-list host exactly like a `?api=` host, so an entry
 * matches every spelling the URL parser maps to the same host. Null for
 * anything that is more than a host: a scheme, port, path or user info.
 */
function normalizeHostEntry(entry: string): string | null {
  if (entry === "" || /[\s/\\?#@]/.test(entry)) return null;
  // A colon is only part of a host inside an IPv6 literal; elsewhere it is a port.
  if (entry.includes(":") && !/^\[[0-9a-f:.]+\]$/i.test(entry)) return null;
  let hostname: string;
  try {
    hostname = new URL(`http://${entry}/`).hostname;
  } catch {
    return null;
  }
  const host = withoutTrailingDot(hostname);
  return host === "" || hasEmptyLabel(host) ? null : host;
}

/**
 * Parses the deploy-time allow list: the `dda-api-allow-hosts` meta tag and
 * `VITE_API_ALLOW_HOSTS`, each a comma-separated list. Entries from every
 * source are combined. An entry is one of:
 *
 *   - an exact host: `station-01.plant.example`, `203.0.113.7`,
 *     `[2001:db8::7]`
 *   - a `.suffix` domain: `.plant.example` allows `plant.example` and every
 *     name under it (never an IP literal)
 *   - an IPv4 CIDR: `100.64.0.0/10`
 *
 * Case and a trailing dot do not matter. An entry that is none of these is
 * ignored, so a typo can only allow less.
 */
export function parseApiHostAllowList(
  ...sources: ReadonlyArray<string | null | undefined>
): ApiHostAllowList {
  const exact = new Set<string>();
  const suffixes: string[] = [];
  const ranges: Ipv4Range[] = [];
  for (const source of sources) {
    if (typeof source !== "string") continue;
    for (const raw of source.split(",")) {
      const entry = raw.trim().toLowerCase();
      if (entry === "") continue;

      const cidr = /^([^/]+)\/(\d{1,2})$/.exec(entry);
      if (cidr !== null) {
        const host = normalizeHostEntry(cidr[1] ?? "");
        const address = host === null ? null : parseIpv4(host);
        const prefix = Number(cidr[2]);
        if (address !== null && prefix <= 32) ranges.push(ipv4Range(address, prefix));
        continue;
      }

      if (entry.startsWith(".")) {
        const suffix = normalizeHostEntry(entry.slice(1));
        // IP literals are matched exactly or by CIDR, never by suffix.
        if (suffix !== null && parseIpv4(suffix) === null && !suffix.startsWith("[")) {
          suffixes.push(suffix);
        }
        continue;
      }

      const host = normalizeHostEntry(entry);
      if (host !== null) exact.add(host);
    }
  }
  return { exact, suffixes, ranges };
}

// --------------------------------------------------------------------------
// Classification
// --------------------------------------------------------------------------

/**
 * True iff `hostname`, as `new URL(...).hostname` reports it, is a host
 * `?api=` may name (see the module docstring).
 */
export function isAllowedApiHost(
  hostname: string,
  pageHostname: string,
  allow: ApiHostAllowList,
): boolean {
  const host = withoutTrailingDot(hostname.toLowerCase());
  if (host === "") return false;
  if (host === withoutTrailingDot(pageHostname.toLowerCase())) return true;
  if (allow.exact.has(host)) return true;

  const ipv4 = parseIpv4(host);
  if (ipv4 !== null) {
    return [...BUILT_IN_IPV4_RANGES, ...allow.ranges].some((range) => inRange(ipv4, range));
  }
  if (host.startsWith("[")) {
    const ipv6 = parseIpv6(host);
    return ipv6 !== null && isBuiltInIpv6(ipv6);
  }

  // A DNS name.
  if (hasEmptyLabel(host)) return false;
  // Single label as written. `station-01.` (trailing dot) is an absolute
  // top-level name instead, so it does not count.
  if (!hostname.includes(".")) return true;
  if (host.endsWith(".local")) return true;
  return allow.suffixes.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
}

/**
 * Applies the policy to a `?api=` value that `normalizeApiBase` already
 * accepted: `http(s)://...` or `//...` (a bare port has been expanded to the
 * page's own host by then).
 */
export function checkApiBaseHost(
  base: string,
  page: ApiHostPage,
  allow: ApiHostAllowList,
): ApiHostCheck {
  const absolute = base.startsWith("//") ? `${page.protocol}${base}` : base;
  let url: URL;
  try {
    url = new URL(absolute);
  } catch {
    return { ok: false, reason: "invalid", host: null };
  }
  // The authority as written, up to the first `/`, `\`, `?` or `#`. Any `@` in
  // it is user info, including an empty one (`http://@host`) that the URL
  // parser drops without a trace.
  const authority = /^(?:https?:)?\/\/([^/\\?#]*)/i.exec(base)?.[1] ?? "";
  if (url.username !== "" || url.password !== "" || authority.includes("@")) {
    return { ok: false, reason: "userinfo", host: url.hostname };
  }
  return isAllowedApiHost(url.hostname, page.hostname, allow)
    ? { ok: true }
    : { ok: false, reason: "host-not-allowed", host: url.hostname };
}
