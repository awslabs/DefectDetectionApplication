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

// Unit tests for the pure URL builders. The image-URL token property
// (Property 3) is covered by its own fast-check test (task 4.3).
// _Requirements: 1.3, 5.5_

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
