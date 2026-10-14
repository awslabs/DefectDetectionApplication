/**
 * Unit tests for the `?api=` host policy (`hosts.ts`).
 *
 * Every value goes through `checkApiBaseHost`, so the classification is
 * exercised on the hostname `new URL(...)` produces — the host `fetch` and
 * `<img src>` actually connect to — rather than on the raw text.
 */

import { describe, expect, it } from "vitest";
import { checkApiBaseHost, parseApiHostAllowList, type ApiHostAllowList } from "./hosts";

/** A kiosk page served from a plant DNS name, so no case below is "own host" by accident. */
const PAGE = { protocol: "http:", hostname: "kiosk.plant.example" };
const NONE = parseApiHostAllowList();

function allowed(value: string, allow: ApiHostAllowList = NONE, page = PAGE): boolean {
  return checkApiBaseHost(value, page, allow).ok;
}

describe("built-in IPv4 ranges", () => {
  it.each([
    // 10.0.0.0/8
    ["9.255.255.255", false],
    ["10.0.0.0", true],
    ["10.20.30.40", true],
    ["10.255.255.255", true],
    ["11.0.0.0", false],
    // 172.16.0.0/12
    ["172.15.255.255", false],
    ["172.16.0.0", true],
    ["172.20.1.2", true],
    ["172.31.255.255", true],
    ["172.32.0.0", false],
    // 192.168.0.0/16
    ["192.167.255.255", false],
    ["192.168.0.1", true],
    ["192.168.255.255", true],
    ["192.169.0.1", false],
    // 127.0.0.0/8
    ["127.0.0.1", true],
    ["127.255.255.254", true],
    ["128.0.0.1", false],
    // 169.254.0.0/16
    ["169.253.255.255", false],
    ["169.254.0.1", true],
    ["169.254.255.255", true],
    ["169.255.0.0", false],
    // public and special addresses
    ["8.8.8.8", false],
    ["203.0.113.7", false],
    ["0.0.0.0", false],
    ["255.255.255.255", false],
  ])("%s -> %s", (ip, expected) => {
    expect(allowed(`http://${ip}:5000`)).toBe(expected);
  });

  it("leaves 100.64.0.0/10 (carrier-grade NAT, some VPNs) out unless the allow list adds it", () => {
    expect(allowed("http://100.64.0.1:5000")).toBe(false);
    expect(allowed("http://100.127.255.254:5000")).toBe(false);

    const vpn = parseApiHostAllowList("100.64.0.0/10");
    expect(allowed("http://100.64.0.1:5000", vpn)).toBe(true);
    expect(allowed("http://100.127.255.254:5000", vpn)).toBe(true);
    expect(allowed("http://100.63.255.255:5000", vpn)).toBe(false);
    expect(allowed("http://100.128.0.0:5000", vpn)).toBe(false);
  });
});

describe("IPv4 spellings the URL parser normalizes", () => {
  it.each([
    ["http://3232235786:5000", true], // decimal 192.168.1.10
    ["http://0xC0A8010A:5000", true], // hex 192.168.1.10
    ["http://0300.0250.1.10:5000", true], // octal 192.168.1.10
    ["http://0x7f.1:5000", true], // short form of 127.0.0.1
    ["http://192.168.1.10.:5000", true], // trailing dot
    ["http://134744072:5000", false], // decimal 8.8.8.8
    ["http://0x08080808:5000", false], // hex 8.8.8.8
    ["http://0x08.0x08.0x08.0x08:5000", false], // dotted hex 8.8.8.8
  ])("%s -> %s", (value, expected) => {
    expect(allowed(value)).toBe(expected);
  });

  it("treats a dotted-quad look-alike as the DNS name it is", () => {
    expect(checkApiBaseHost("http://192.168.1.10.example.com:5000", PAGE, NONE)).toEqual({
      ok: false,
      reason: "host-not-allowed",
      host: "192.168.1.10.example.com",
    });
    expect(allowed("http://10.0.0.5.nip.io:5000")).toBe(false);
    // Only the allow list can admit it.
    expect(
      allowed("http://192.168.1.10.example.com:5000", parseApiHostAllowList(".example.com")),
    ).toBe(true);
  });
});

describe("IPv6 literals", () => {
  it.each([
    ["http://[::1]:5000", true],
    ["http://[0:0:0:0:0:0:0:1]:5000", true], // uncompressed ::1
    ["http://[::2]:5000", false],
    ["http://[::]:5000", false],
    ["http://[fbff::1]:5000", false], // just below fc00::/7
    ["http://[fc00::1]:5000", true],
    ["http://[fd12:3456:789a::1]:5000", true],
    ["http://[fe00::1]:5000", false], // between fc00::/7 and fe80::/10
    ["http://[FE80::1]:5000", true], // upper case is normalized
    ["http://[febf:ffff::1]:5000", true], // top of fe80::/10
    ["http://[fec0::1]:5000", false], // just above fe80::/10
    ["http://[2001:db8::1]:5000", false],
    ["http://[::ffff:192.168.1.10]:5000", false], // IPv4-mapped: use the IPv4 literal
  ])("%s -> %s", (value, expected) => {
    expect(allowed(value)).toBe(expected);
  });
});

describe("names", () => {
  it.each([
    ["http://station-01:5000", true], // single label
    ["http://STATION-01:5000", true], // upper case is normalized
    ["http://localhost:5000", true],
    ["http://station-01.:5000", false], // trailing dot: an absolute top-level name, not a single label
    ["http://printer.local:5000", true],
    ["http://PRINTER.LOCAL:5000", true],
    ["http://printer.local.:5000", true], // trailing dot: the same mDNS name
    ["http://a.b.local:5000", true],
    ["http://printer.locals:5000", false],
    ["http://local.example.com:5000", false],
    ["http://printer.local.example.com:5000", false],
    ["http://other.example:5000", false],
    ["http://other.example.:5000", false],
    ["http://.local:5000", false], // empty label
    ["http://a..b:5000", false], // empty label
  ])("%s -> %s", (value, expected) => {
    expect(allowed(value)).toBe(expected);
  });

  it("accepts the page's own host on any port, ignoring case and a trailing dot", () => {
    expect(allowed("http://kiosk.plant.example:5000")).toBe(true);
    expect(allowed("https://KIOSK.plant.example.:5443")).toBe(true);
    expect(allowed("http://kiosk.plant.example.evil.com:5000")).toBe(false);
  });

  it("accepts a public page host only for that same host", () => {
    const page = { protocol: "https:", hostname: "hmi.example.com" };
    expect(allowed("https://hmi.example.com:5443", NONE, page)).toBe(true);
    expect(allowed("https://api.example.com:5443", NONE, page)).toBe(false);
  });
});

describe("user info and protocol-relative values", () => {
  it.each([
    "http://user@192.168.1.10:5000",
    "http://user:pw@192.168.1.10:5000",
    "http://@192.168.1.10:5000", // empty user info, which the parser drops
    "//user@192.168.1.10:5000",
  ])("rejects %s whatever its host", (value) => {
    expect(checkApiBaseHost(value, PAGE, NONE)).toEqual({
      ok: false,
      reason: "userinfo",
      host: "192.168.1.10",
    });
  });

  it("names the real host when user info dresses it up as another", () => {
    expect(checkApiBaseHost("http://192.168.1.10@evil.example:5000", PAGE, NONE)).toEqual({
      ok: false,
      reason: "userinfo",
      host: "evil.example",
    });
  });

  it("classifies a protocol-relative value like the absolute one", () => {
    expect(allowed("//192.168.1.10:5000")).toBe(true);
    expect(allowed("//station-01:5000")).toBe(true);
    expect(checkApiBaseHost("//other.example:5000", PAGE, NONE)).toEqual({
      ok: false,
      reason: "host-not-allowed",
      host: "other.example",
    });
    expect(
      allowed("//kiosk.plant.example:5443", NONE, { protocol: "https:", hostname: "kiosk.plant.example" }),
    ).toBe(true);
  });

  it("classifies the host requests actually go to when the authority ends early", () => {
    // The URL parser ends the authority at "\" and "#", so requests built on
    // these bases go to 10.0.0.5; "@evil.example" is path or fragment.
    expect(allowed("http://10.0.0.5:5000\\@evil.example")).toBe(true);
    expect(allowed("http://10.0.0.5:5000#@evil.example")).toBe(true);
    expect(new URL("http://10.0.0.5:5000\\@evil.example/local-auth/login").hostname).toBe("10.0.0.5");
  });

  it("rejects a value the URL parser cannot parse", () => {
    expect(checkApiBaseHost("http://exa mple.com", PAGE, NONE)).toEqual({
      ok: false,
      reason: "invalid",
      host: null,
    });
    expect(checkApiBaseHost("http://1.2.3.4.5", PAGE, NONE)).toEqual({
      ok: false,
      reason: "invalid",
      host: null,
    });
  });
});

describe("deploy-time allow list", () => {
  it("matches an exact entry only exactly", () => {
    const allow = parseApiHostAllowList("station-01.plant.example, 203.0.113.7, [2001:db8::7]");
    expect(allowed("http://station-01.plant.example:5000", allow)).toBe(true);
    expect(allowed("http://STATION-01.Plant.Example.:5000", allow)).toBe(true);
    expect(allowed("http://a.station-01.plant.example:5000", allow)).toBe(false);
    expect(allowed("http://plant.example:5000", allow)).toBe(false);
    expect(allowed("http://203.0.113.7:5000", allow)).toBe(true);
    expect(allowed("http://3405803783:5000", allow)).toBe(true); // 203.0.113.7 in decimal
    expect(allowed("http://203.0.113.8:5000", allow)).toBe(false);
    expect(allowed("http://[2001:db8::7]:5000", allow)).toBe(true);
    expect(allowed("http://[2001:db8:0:0:0:0:0:7]:5000", allow)).toBe(true);
    expect(allowed("http://[2001:db8::8]:5000", allow)).toBe(false);
  });

  it("matches a .suffix entry for that name and every name under it", () => {
    const allow = parseApiHostAllowList(".plant.example");
    expect(allowed("http://plant.example:5000", allow)).toBe(true);
    expect(allowed("http://station-01.plant.example:5000", allow)).toBe(true);
    expect(allowed("http://a.b.plant.example.:5000", allow)).toBe(true);
    expect(allowed("http://evilplant.example:5000", allow)).toBe(false);
    expect(allowed("http://plant.example.evil.com:5000", allow)).toBe(false);
  });

  it("never matches an IP literal by suffix", () => {
    // A numeric last label makes the URL parser read an IPv4 address
    // (".113.7" -> 113.0.0.7), so such suffix entries are dropped.
    const allow = parseApiHostAllowList(".113.7, .7, .10, .a.10");
    expect(allowed("http://203.0.113.7:5000", allow)).toBe(false);
    expect(allowed("http://192.168.0.10:5000", allow)).toBe(true); // built in, not by suffix
    expect(allow.suffixes).toEqual([]);
  });

  it("matches an IPv4 CIDR entry whatever spelling the URL uses", () => {
    const allow = parseApiHostAllowList("203.0.113.0/24");
    expect(allowed("http://203.0.113.200:5000", allow)).toBe(true);
    expect(allowed("http://0xCB007199:5000", allow)).toBe(true); // 203.0.113.153 in hex
    expect(allowed("http://203.0.114.1:5000", allow)).toBe(false);
  });

  it("combines every source and ignores entries that are not hosts", () => {
    const allow = parseApiHostAllowList(
      " , station-a.plant.example ,",
      null,
      undefined,
      "STATION-B.plant.example, http://x.example, x.example:5000, user@y.example, " +
        "z .example, 10.0.0.0/33, 300.1.1.1/8, ..plant.example",
    );
    expect(allowed("http://station-a.plant.example:5000", allow)).toBe(true);
    expect(allowed("http://station-b.plant.example:5000", allow)).toBe(true);
    expect([...allow.exact].sort()).toEqual(["station-a.plant.example", "station-b.plant.example"]);
    expect(allow.suffixes).toEqual([]);
    expect(allow.ranges).toEqual([]);
  });
});
