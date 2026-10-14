/**
 * Unit tests for API_Base resolution (detached-hosting support).
 *
 * The critical property is that the DEFAULT is unchanged: with nothing
 * configured every builder must still emit the root-relative, same-origin URLs
 * the LocalServer's `/hmi` mount has always served. The rest covers the
 * precedence order and the accepted value forms.
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  apiBaseFromMeta,
  apiBaseFromSearch,
  getApiBase,
  normalizeApiBase,
  resetApiBase,
  resolveApiBase,
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

function fakeDoc(content: string | null): Document {
  return {
    querySelector: (_sel: string) =>
      content === null
        ? null
        : ({ getAttribute: (_a: string) => content } as unknown as Element),
  } as unknown as Document;
}

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
});

describe("resolveApiBase precedence", () => {
  it("defaults to same-origin when nothing is configured", () => {
    expect(
      resolveApiBase({ search: "", doc: fakeDoc(null), buildTime: null }),
    ).toBe("");
  });

  it("prefers the query parameter over the meta tag and the build-time value", () => {
    expect(
      resolveApiBase({
        search: "?api=http://from-query:1",
        doc: fakeDoc("http://from-meta:2"),
        buildTime: "http://from-build:3",
      }),
    ).toBe("http://from-query:1");
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
