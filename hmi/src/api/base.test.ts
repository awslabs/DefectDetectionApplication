/**
 * Unit tests for API_Base resolution (detached-hosting support).
 *
 * The critical property is that the DEFAULT is unchanged: with nothing
 * configured every builder must still emit the root-relative, same-origin URLs
 * the LocalServer's `/hmi` mount has always served. The rest covers the
 * precedence order, the accepted value forms, the `?api=` host policy's place
 * in the precedence (the policy itself is `hosts.test.ts`), and the API origin
 * a session is bound to.
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  API_ALLOW_HOSTS_META_NAME,
  API_BASE_META_NAME,
  apiAllowHostsFromMeta,
  apiBaseFromMeta,
  apiBaseFromSearch,
  apiBaseNoticeText,
  getApiAddress,
  getApiBase,
  getApiBaseNotice,
  getApiBaseRejection,
  getApiOrigin,
  normalizeApiBase,
  requestOrigin,
  resetApiBase,
  resolveApiBase,
  resolveApiBaseDetailed,
  setApiBase,
} from "./base";
import {
  executionMetadataUrl,
  localAuthStatusUrl,
  loginUrl,
  nodeImageUrl,
  outputImageUrl,
  registrationExecutionsUrl,
  registrationsUrl,
} from "./routes";

/** A host document with a `dda-api-base` and, optionally, a `dda-api-allow-hosts` tag. */
function fakeDoc(content: string | null, allowHosts: string | null = null): Document {
  return {
    querySelector: (selector: string) => {
      const value = selector.includes(`"${API_ALLOW_HOSTS_META_NAME}"`)
        ? allowHosts
        : selector.includes(`"${API_BASE_META_NAME}"`)
          ? content
          : null;
      return value === null
        ? null
        : ({ getAttribute: (_a: string) => value } as unknown as Element);
    },
  } as unknown as Document;
}

/** A kiosk page on the plant network, as `location` would describe it. */
const PAGE = { pageProtocol: "http:", pageHostname: "192.168.8.10" };

beforeEach(() => {
  resetApiBase();
});

describe("normalizeApiBase", () => {
  it("treats empty / missing / non-string as same-origin", () => {
    expect(normalizeApiBase("")).toBe("");
    expect(normalizeApiBase("   ")).toBe("");
    expect(normalizeApiBase(undefined)).toBe("");
    expect(normalizeApiBase(null)).toBe("");
    expect(normalizeApiBase(5000 as unknown as string)).toBe("");
  });

  it("expands a bare port against the page's scheme and host", () => {
    expect(normalizeApiBase("5000", "http:", "192.168.8.224")).toBe(
      "http://192.168.8.224:5000",
    );
    expect(normalizeApiBase("5443", "https:", "device.local")).toBe(
      "https://device.local:5443",
    );
  });

  it("rejects an out-of-range port rather than building a bad origin", () => {
    expect(normalizeApiBase("0", "http:", "h")).toBe("");
    expect(normalizeApiBase("70000", "http:", "h")).toBe("");
  });

  it("accepts absolute and protocol-relative origins, stripping trailing slashes", () => {
    expect(normalizeApiBase("http://host:5000")).toBe("http://host:5000");
    expect(normalizeApiBase("https://host")).toBe("https://host");
    expect(normalizeApiBase("//host:5000")).toBe("//host:5000");
    expect(normalizeApiBase("http://host:5000/")).toBe("http://host:5000");
    expect(normalizeApiBase("http://host:5000///")).toBe("http://host:5000");
  });

  it("rejects a value that is neither absolute nor protocol-relative", () => {
    // A bare hostname or a path would silently produce broken request URLs.
    expect(normalizeApiBase("host:5000")).toBe("");
    expect(normalizeApiBase("/api")).toBe("");
    expect(normalizeApiBase("////")).toBe("");
  });
});

describe("readers", () => {
  it("reads the api query parameter", () => {
    expect(apiBaseFromSearch("?api=http://h:5000")).toBe("http://h:5000");
    expect(apiBaseFromSearch("?other=1")).toBeNull();
    expect(apiBaseFromSearch("")).toBeNull();
    expect(apiBaseFromSearch(undefined)).toBeNull();
  });

  it("reads the dda-api-base meta tag", () => {
    expect(apiBaseFromMeta(fakeDoc("http://h:5000"))).toBe("http://h:5000");
    expect(apiBaseFromMeta(fakeDoc(null))).toBeNull();
  });

  it("reads the dda-api-allow-hosts meta tag", () => {
    expect(apiAllowHostsFromMeta(fakeDoc(null, ".plant.example"))).toBe(".plant.example");
    expect(apiAllowHostsFromMeta(fakeDoc("http://h:5000"))).toBeNull();
  });
});

describe("resolveApiBase precedence", () => {
  it("defaults to same-origin when nothing is configured", () => {
    expect(
      resolveApiBase({ search: "", doc: fakeDoc(null), buildTime: null }),
    ).toBe("");
  });

  it("prefers an allowed query parameter over the meta tag and the build-time value", () => {
    // Another station on the plant network, by IP address: still per-URL.
    expect(
      resolveApiBaseDetailed({
        search: "?api=http://192.168.8.224:5000",
        doc: fakeDoc("http://from-meta:2"),
        buildTime: "http://from-build:3",
        ...PAGE,
      }),
    ).toEqual({ base: "http://192.168.8.224:5000", rejected: null });
  });

  it("ignores a query parameter naming any other host, falling back to the meta tag", () => {
    // Before the host policy this won, and the token and credentials followed.
    expect(
      resolveApiBaseDetailed({
        search: "?api=https://other.example",
        doc: fakeDoc("http://from-meta:2"),
        buildTime: "http://from-build:3",
        ...PAGE,
      }),
    ).toEqual({
      base: "http://from-meta:2",
      rejected: { reason: "host-not-allowed", host: "other.example" },
    });
  });

  it("falls back to the build-time value, then same-origin, after an ignored query parameter", () => {
    const rejected = { reason: "host-not-allowed", host: "other.example" };
    expect(
      resolveApiBaseDetailed({
        search: "?api=http://other.example:5000",
        doc: fakeDoc(""),
        buildTime: "http://from-build:3",
        ...PAGE,
      }),
    ).toEqual({ base: "http://from-build:3", rejected });
    expect(
      resolveApiBaseDetailed({
        search: "?api=//other.example:5000",
        doc: fakeDoc(null),
        buildTime: null,
        ...PAGE,
      }),
    ).toEqual({ base: "", rejected });
  });

  it("prefers the meta tag over the build-time value", () => {
    expect(
      resolveApiBase({
        search: "",
        doc: fakeDoc("http://from-meta:2"),
        buildTime: "http://from-build:3",
      }),
    ).toBe("http://from-meta:2");
  });

  it("falls through an empty meta tag to the build-time value", () => {
    // The shipped HTML carries content="" as a placeholder; it must not veto
    // a build-time base.
    expect(
      resolveApiBase({
        search: "",
        doc: fakeDoc(""),
        buildTime: "http://from-build:3",
      }),
    ).toBe("http://from-build:3");
  });

  it("falls through an unusable candidate to the next one", () => {
    expect(
      resolveApiBase({
        search: "?api=not-a-valid-origin",
        doc: fakeDoc("http://from-meta:2"),
        buildTime: null,
      }),
    ).toBe("http://from-meta:2");
    // A value normalizeApiBase cannot use falls through silently, as before.
    expect(
      resolveApiBaseDetailed({
        search: "?api=not-a-valid-origin",
        doc: fakeDoc("http://from-meta:2"),
        buildTime: null,
      }).rejected,
    ).toBeNull();
  });

  it("does not host-check the deploy-time sources", () => {
    // The meta tag, VITE_API_BASE and the global behave exactly as before.
    expect(resolveApiBase({ search: "", doc: fakeDoc("https://api.example.com"), buildTime: null, ...PAGE })).toBe(
      "https://api.example.com",
    );
    expect(resolveApiBase({ search: "", doc: fakeDoc(null), buildTime: "https://api.example.com", ...PAGE })).toBe(
      "https://api.example.com",
    );
  });
});

describe("?api= host policy in the precedence", () => {
  it("keeps the bare port on the page's own host", () => {
    expect(resolveApiBaseDetailed({ search: "?api=5000", doc: fakeDoc(null), buildTime: null, ...PAGE })).toEqual({
      base: "http://192.168.8.10:5000",
      rejected: null,
    });
  });

  it("accepts the page's own host on another port, even as a public name", () => {
    expect(
      resolveApiBase({
        search: "?api=https://hmi.example.com:5443",
        doc: fakeDoc(null),
        buildTime: null,
        pageProtocol: "https:",
        pageHostname: "hmi.example.com",
      }),
    ).toBe("https://hmi.example.com:5443");
  });

  it("accepts hosts from the meta-tag and the build-time allow lists", () => {
    const inputs = {
      doc: fakeDoc(null, ".plant.example"),
      buildTime: null,
      buildTimeAllowHosts: "100.64.0.0/10",
      ...PAGE,
    };
    expect(resolveApiBase({ ...inputs, search: "?api=http://station-01.plant.example:5000" })).toBe(
      "http://station-01.plant.example:5000",
    );
    expect(resolveApiBase({ ...inputs, search: "?api=http://100.100.1.2:5000" })).toBe(
      "http://100.100.1.2:5000",
    );
    expect(resolveApiBaseDetailed({ ...inputs, search: "?api=http://station-01.other.example" }).rejected).toEqual({
      reason: "host-not-allowed",
      host: "station-01.other.example",
    });
  });

  it("reports user info and unparseable values", () => {
    expect(
      resolveApiBaseDetailed({ search: "?api=http://op@192.168.8.224:5000", doc: fakeDoc(null), buildTime: null, ...PAGE })
        .rejected,
    ).toEqual({ reason: "userinfo", host: "192.168.8.224" });
    expect(
      resolveApiBaseDetailed({ search: "?api=http://exa%20mple.com", doc: fakeDoc(null), buildTime: null, ...PAGE })
        .rejected,
    ).toEqual({ reason: "invalid", host: null });
  });
});

describe("ignored ?api= notice", () => {
  it("names the ignored host, the reason, and where the HMI connects instead", () => {
    expect(apiBaseNoticeText({ reason: "host-not-allowed", host: "other.example" }, "192.168.8.10:5000")).toBe(
      'Ignored ?api= host "other.example": it is not this page\'s host, a private-network address ' +
        "or an allowed host. Using 192.168.8.10:5000 instead.",
    );
    expect(apiBaseNoticeText({ reason: "userinfo", host: "192.168.8.224" }, "")).toBe(
      'Ignored ?api= host "192.168.8.224": addresses with a user name or password are not accepted.',
    );
    expect(apiBaseNoticeText({ reason: "invalid", host: null }, "")).toBe(
      "Ignored ?api=: the value is not a valid address.",
    );
  });

  it("is absent when nothing was ignored", () => {
    setApiBase("http://192.168.8.224:5000");
    expect(getApiBaseRejection()).toBeNull();
    expect(getApiBaseNotice()).toBeNull();
  });
});

describe("API origin and address", () => {
  it("resolves a request URL's origin the way fetch does", () => {
    expect(requestOrigin("/local-auth/login", "http://192.168.8.10:8081/hmi/triple.html")).toBe(
      "http://192.168.8.10:8081",
    );
    expect(requestOrigin("//192.168.8.224:5000/x", "https://kiosk/")).toBe("https://192.168.8.224:5000");
    expect(requestOrigin("http://3232235786:5000/x", undefined)).toBe("http://192.168.1.10:5000");
    // Undeterminable: relative with no page, or unparseable.
    expect(requestOrigin("/local-auth/login", undefined)).toBe("");
    expect(requestOrigin("http://exa mple.com/x", undefined)).toBe("");
  });

  it("is the API_Base's origin, with host:port always spelled out for the login form", () => {
    setApiBase("http://192.168.8.224:5000/");
    expect(getApiOrigin()).toBe("http://192.168.8.224:5000");
    expect(getApiAddress()).toBe("192.168.8.224:5000");
    setApiBase("https://station-01.local");
    expect(getApiAddress()).toBe("station-01.local:443");
    setApiBase("http://[FE80::1]:5000");
    expect(getApiAddress()).toBe("[fe80::1]:5000");
  });

  it("is undeterminable for a same-origin base without a page location", () => {
    setApiBase("");
    expect(getApiOrigin()).toBe("");
    expect(getApiAddress()).toBe("");
  });
});

describe("route builders honour the base", () => {
  it("emit the original root-relative URLs when no base is set", () => {
    setApiBase("");
    expect(loginUrl()).toBe("/local-auth/login");
    expect(registrationsUrl()).toBe("/workflows/registrations");
    expect(registrationExecutionsUrl("r1", 10)).toBe(
      "/workflows/registrations/r1/executions?limit=10",
    );
    expect(executionMetadataUrl("e1")).toBe("/workflows/executions/e1/metadata");
    expect(outputImageUrl("e1", "t")).toBe(
      "/workflows/executions/e1/output-image?token=t",
    );
    expect(nodeImageUrl("e1", "n1", "p", "t")).toBe(
      "/workflows/executions/e1/node-image?nodeId=n1&port=p&token=t",
    );
  });

  it("prefix every URL, including the image routes, once a base is set", () => {
    setApiBase("http://192.168.8.224:5000");
    expect(loginUrl()).toBe("http://192.168.8.224:5000/local-auth/login");
    expect(registrationsUrl()).toBe(
      "http://192.168.8.224:5000/workflows/registrations",
    );
    expect(executionMetadataUrl("e1")).toBe(
      "http://192.168.8.224:5000/workflows/executions/e1/metadata",
    );
    // The <img>-loaded routes must be absolute too, or the kiosk shows broken
    // images while the JSON data works.
    expect(outputImageUrl("e1", "t")).toBe(
      "http://192.168.8.224:5000/workflows/executions/e1/output-image?token=t",
    );
    expect(nodeImageUrl("e1", "n1", "p", "t")).toBe(
      "http://192.168.8.224:5000/workflows/executions/e1/node-image?nodeId=n1&port=p&token=t",
    );
  });

  it("keeps encoding dynamic segments after the base is applied", () => {
    setApiBase("http://h:5000");
    expect(nodeImageUrl("e/1", "n 1", "p&x", "t?y")).toBe(
      "http://h:5000/workflows/executions/e%2F1/node-image?nodeId=n%201&port=p%26x&token=t%3Fy",
    );
  });

  it("prefixes the local-auth status probe (regression: detached login wall)", () => {
    // This URL used to be a local constant in the entry points, bypassing the
    // base. Detached, the probe then hit the STATIC server, 404'd, and the
    // login form stayed up on a device that issues no tokens at all — while
    // the login POST (which did carry the base) reached the device and
    // answered 403 "local login is disabled". Exactly the symptom reported.
    setApiBase("");
    expect(localAuthStatusUrl()).toBe("/local-auth/status");
    resetApiBase();
    setApiBase("http://192.168.8.224:5000");
    expect(localAuthStatusUrl()).toBe(
      "http://192.168.8.224:5000/local-auth/status",
    );
  });

  it("keeps the status probe and the login POST on the SAME origin", () => {
    // The pair disagreeing is what produced the confusing state above.
    setApiBase("http://192.168.8.224:5000");
    const status = new URL(localAuthStatusUrl(), "http://page.example");
    const login = new URL(loginUrl(), "http://page.example");
    expect(status.origin).toBe(login.origin);
  });

  it("caches the resolved base so two requests cannot disagree", () => {
    setApiBase("http://first:1");
    const a = getApiBase();
    const b = getApiBase();
    expect(a).toBe("http://first:1");
    expect(b).toBe(a);
  });
});
