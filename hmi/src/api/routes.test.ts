import fc from "fast-check";
import { describe, expect, it } from "vitest";
import {
  executionMetadataUrl,
  executionResultsUrl,
  loginUrl,
  nodeImageUrl,
  outputImageUrl,
  registrationExecutionsUrl,
  registrationsUrl,
} from "./routes";

// Unit tests for the pure URL builders, plus the image-URL token property
// (Property 3) whose port generators cover the Triple_HMI's additive
// `original` / `annotated` node-image ports.
// _Requirements: 1.3, 5.5, 4.5_

describe("static routes", () => {
  it("builds the login and registrations URLs", () => {
    expect(loginUrl()).toBe("/local-auth/login");
    expect(registrationsUrl()).toBe("/workflows/registrations");
  });
});

describe("registrationExecutionsUrl", () => {
  it("targets the bounded recent-executions route with the default limit", () => {
    expect(registrationExecutionsUrl("reg-1")).toBe(
      "/workflows/registrations/reg-1/executions?limit=10",
    );
  });

  it("carries an explicit limit", () => {
    expect(registrationExecutionsUrl("reg-1", 50)).toBe(
      "/workflows/registrations/reg-1/executions?limit=50",
    );
  });

  it("encodes the registration id path segment", () => {
    expect(registrationExecutionsUrl("a/b c?", 10)).toBe(
      "/workflows/registrations/a%2Fb%20c%3F/executions?limit=10",
    );
  });
});

describe("execution data routes", () => {
  it("builds the results and metadata URLs", () => {
    expect(executionResultsUrl("exec-1")).toBe(
      "/workflows/executions/exec-1/results",
    );
    expect(executionMetadataUrl("exec-1")).toBe(
      "/workflows/executions/exec-1/metadata",
    );
  });

  it("encodes the execution id path segment", () => {
    expect(executionResultsUrl("e/1")).toBe(
      "/workflows/executions/e%2F1/results",
    );
  });
});

describe("image routes (token in query, Requirements 1.3, 5.5)", () => {
  it("outputImageUrl carries the Session_Token as the token query parameter", () => {
    const url = outputImageUrl("exec-1", "tok-123");
    expect(url).toBe("/workflows/executions/exec-1/output-image?token=tok-123");
    const parsed = new URL(url, "https://device:5443");
    expect(parsed.searchParams.get("token")).toBe("tok-123");
  });

  it("nodeImageUrl carries nodeId, port, and the encoded token", () => {
    const url = nodeImageUrl("exec-1", "vlm_node", "reference", "a+b/c=");
    const parsed = new URL(url, "https://device:5443");
    expect(parsed.pathname).toBe("/workflows/executions/exec-1/node-image");
    expect(parsed.searchParams.get("nodeId")).toBe("vlm_node");
    expect(parsed.searchParams.get("port")).toBe("reference");
    // URL decoding round-trips the token exactly.
    expect(parsed.searchParams.get("token")).toBe("a+b/c=");
    // The raw token is percent-encoded in the URL string itself.
    expect(url).toContain("token=a%2Bb%2Fc%3D");
  });

  it("encodes reserved characters so arbitrary values cannot escape their URL part", () => {
    const url = nodeImageUrl("e&1", "n?x", "in out", "t&k=v#f");
    const parsed = new URL(url, "https://device:5443");
    expect(parsed.searchParams.get("nodeId")).toBe("n?x");
    expect(parsed.searchParams.get("port")).toBe("in out");
    expect(parsed.searchParams.get("token")).toBe("t&k=v#f");
    expect(parsed.hash).toBe("");
  });
});

/**
 * Property test for the token-in-query image URL builders.
 *
 * **Feature: imts-triple-inspection-hmi, Property 3: Image URLs carry the token in query**
 *
 * **Validates: Requirements 1.3, 4.5**
 *
 * The port generator covers the ports the single-inspection HMI uses (`in`,
 * `reference`) *and* the two additive ports the Triple_HMI reads
 * (`original`, `annotated`), confirming the existing builder is port-generic
 * — `api/routes.ts` is unchanged.
 */

/** Reserved characters that must never escape their own URL part. */
const reservedish = fc.constantFrom(
  "a/b",
  "a&b",
  "n?x",
  "e#1",
  "x y",
  "p+q",
  "100%",
  "tok=v",
  "a+b/c=",
  "..%2F..",
  "\t",
  "ünïcødé",
);

/**
 * Path/query values: realistic ids, arbitrary strings, and reserved-character
 * cases. Bare dot segments (`.` / `..`) are excluded because the URL parser
 * resolves them as relative path segments before any builder sees them —
 * dot-segment resolution is the URL grammar's behavior, not this builder's.
 */
const urlValue: fc.Arbitrary<string> = fc
  .oneof(
    { arbitrary: fc.stringMatching(/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,30}$/), weight: 3 },
    { arbitrary: fc.string(), weight: 2 },
    { arbitrary: reservedish, weight: 2 },
  )
  .filter((value) => !/^\.{1,2}$/.test(value));

/** Node-image ports: the existing two, the additive two, and arbitrary ones. */
const portValue: fc.Arbitrary<string> = fc.oneof(
  {
    arbitrary: fc.constantFrom("in", "reference", "original", "annotated"),
    weight: 4,
  },
  { arbitrary: urlValue, weight: 1 },
);

/** Session_Tokens: JWT-ish shapes plus arbitrary and reserved-character text. */
const tokenValue: fc.Arbitrary<string> = fc.oneof(
  { arbitrary: fc.stringMatching(/^[A-Za-z0-9_.-]{8,60}$/), weight: 3 },
  { arbitrary: fc.string(), weight: 2 },
  { arbitrary: reservedish, weight: 2 },
);

const ORIGIN = "https://device:5443";

describe("Property 3: Image URLs carry the token in query", () => {
  it("nodeImageUrl targets the node-image route and carries exactly the token", () => {
    fc.assert(
      fc.property(
        urlValue,
        urlValue,
        portValue,
        tokenValue,
        (executionId, nodeId, port, token) => {
          const url = nodeImageUrl(executionId, nodeId, port, token);
          const parsed = new URL(url, ORIGIN);

          // Root-relative, same-origin as the /hmi mount.
          expect(url.startsWith("/workflows/executions/")).toBe(true);
          expect(parsed.origin).toBe(ORIGIN);

          // The route targets exactly this execution; no dynamic value leaks
          // into the path beyond its own encoded segment.
          expect(parsed.pathname).toBe(
            `/workflows/executions/${encodeURIComponent(executionId)}/node-image`,
          );

          // Every dynamic value round-trips out of its own query parameter,
          // and no extra parameters or fragment appear.
          expect([...parsed.searchParams.keys()]).toEqual([
            "nodeId",
            "port",
            "token",
          ]);
          expect(parsed.searchParams.get("nodeId")).toBe(nodeId);
          expect(parsed.searchParams.get("port")).toBe(port);
          expect(parsed.searchParams.getAll("token")).toEqual([token]);
          expect(parsed.hash).toBe("");
        },
      ),
    );
  });

  it("outputImageUrl targets the output-image route and carries exactly the token", () => {
    fc.assert(
      fc.property(urlValue, tokenValue, (executionId, token) => {
        const url = outputImageUrl(executionId, token);
        const parsed = new URL(url, ORIGIN);

        expect(parsed.pathname).toBe(
          `/workflows/executions/${encodeURIComponent(executionId)}/output-image`,
        );
        expect([...parsed.searchParams.keys()]).toEqual(["token"]);
        expect(parsed.searchParams.getAll("token")).toEqual([token]);
        expect(parsed.hash).toBe("");
      }),
    );
  });

  it("is deterministic and independent of port identity", () => {
    fc.assert(
      fc.property(
        urlValue,
        urlValue,
        portValue,
        portValue,
        tokenValue,
        (executionId, nodeId, portA, portB, token) => {
          const a = nodeImageUrl(executionId, nodeId, portA, token);
          expect(nodeImageUrl(executionId, nodeId, portA, token)).toBe(a);

          // Two ports of the same Inspection differ only in the port value —
          // the token and node stay intact for every port (4.5).
          const b = nodeImageUrl(executionId, nodeId, portB, token);
          const parsedA = new URL(a, ORIGIN);
          const parsedB = new URL(b, ORIGIN);
          expect(parsedB.pathname).toBe(parsedA.pathname);
          expect(parsedB.searchParams.get("nodeId")).toBe(
            parsedA.searchParams.get("nodeId"),
          );
          expect(parsedB.searchParams.get("token")).toBe(
            parsedA.searchParams.get("token"),
          );
          expect(parsedB.searchParams.get("port")).toBe(portB);
        },
      ),
    );
  });
});

describe("additive node-image ports (Requirement 4.5)", () => {
  it("builds original and annotated URLs for the same Inspection", () => {
    expect(nodeImageUrl("exec-1", "bedrock_1", "original", "tok")).toBe(
      "/workflows/executions/exec-1/node-image?nodeId=bedrock_1&port=original&token=tok",
    );
    expect(nodeImageUrl("exec-1", "bedrock_1", "annotated", "tok")).toBe(
      "/workflows/executions/exec-1/node-image?nodeId=bedrock_1&port=annotated&token=tok",
    );
  });
});
