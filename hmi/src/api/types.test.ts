import { describe, expect, it } from "vitest";
import {
  parseExecution,
  parseExecutions,
  parseRegistration,
  parseRegistrations,
  parseResultImage,
  parseResultImages,
  parseRunMetadata,
} from "./types";

// Unit tests for the defensive parse boundary (design "Data Models").
// _Requirements: 2.6, 4.7_

describe("parseRegistration", () => {
  it("mirrors a well-formed payload", () => {
    expect(
      parseRegistration({
        registrationId: "reg-1",
        workflowId: "wf-1",
        name: "IMTS - Swagfactory",
        version: "1.0.0",
        status: "registered",
        registeredAt: 1736950000,
      }),
    ).toEqual({
      registrationId: "reg-1",
      workflowId: "wf-1",
      name: "IMTS - Swagfactory",
      version: "1.0.0",
      status: "registered",
      registeredAt: 1736950000,
    });
  });

  it("ignores unknown fields and defaults missing ones", () => {
    const parsed = parseRegistration({
      registrationId: "reg-2",
      unexpected: { nested: true },
    });
    expect(parsed).toEqual({
      registrationId: "reg-2",
      workflowId: "",
      name: null,
      version: "",
      status: "",
      registeredAt: 0,
    });
    expect("unexpected" in parsed).toBe(false);
  });

  it("treats a null name as null (backend allows null workflowName)", () => {
    expect(parseRegistration({ name: null }).name).toBeNull();
    expect(parseRegistration({ name: 42 }).name).toBeNull();
  });

  it("defaults everything for non-object input", () => {
    expect(parseRegistration(null).registrationId).toBe("");
    expect(parseRegistration("junk").status).toBe("");
  });
});

describe("parseRegistrations", () => {
  it("parses each entry and yields [] for non-arrays", () => {
    expect(parseRegistrations([{ registrationId: "a" }, {}])).toHaveLength(2);
    expect(parseRegistrations({ detail: "oops" })).toEqual([]);
    expect(parseRegistrations(undefined)).toEqual([]);
  });
});

describe("parseExecution", () => {
  it("mirrors a well-formed payload", () => {
    expect(
      parseExecution({
        executionId: "ex-1",
        registrationId: "reg-1",
        status: "completed",
        startedAt: 100,
        finishedAt: 105,
        failingNodeId: null,
        error: null,
        hasImageResults: true,
        captureId: "cap-1",
      }),
    ).toEqual({
      executionId: "ex-1",
      registrationId: "reg-1",
      status: "completed",
      startedAt: 100,
      finishedAt: 105,
      failingNodeId: null,
      error: null,
      hasImageResults: true,
      captureId: "cap-1",
    });
  });

  it("keeps all four known statuses", () => {
    for (const status of ["pending", "running", "completed", "failed"]) {
      expect(parseExecution({ status }).status).toBe(status);
    }
  });

  it("defaults unknown statuses to pending (never a displayable terminal run)", () => {
    expect(parseExecution({ status: "archived" }).status).toBe("pending");
    expect(parseExecution({}).status).toBe("pending");
  });

  it("defaults missing/malformed fields", () => {
    const parsed = parseExecution({ finishedAt: "not-a-number", extra: 1 });
    expect(parsed.finishedAt).toBeNull();
    expect(parsed.startedAt).toBe(0);
    expect(parsed.hasImageResults).toBe(false);
    expect(parsed.captureId).toBeNull();
    expect("extra" in parsed).toBe(false);
  });
});

describe("parseExecutions", () => {
  it("yields [] for non-array payloads", () => {
    expect(parseExecutions({})).toEqual([]);
    expect(parseExecutions(null)).toEqual([]);
  });
});

describe("parseResultImage", () => {
  it("parses node entries with nodeId and port", () => {
    expect(
      parseResultImage({ kind: "node", nodeId: "n1", port: "reference", hasOverlay: true }),
    ).toEqual({ kind: "node", nodeId: "n1", port: "reference", hasOverlay: true });
  });

  it("treats anything not explicitly node as output and drops node fields", () => {
    expect(parseResultImage({ kind: "output" })).toEqual({ kind: "output", hasOverlay: false });
    expect(parseResultImage({ kind: "banana", nodeId: "n1" })).toEqual({
      kind: "output",
      hasOverlay: false,
    });
  });

  it("omits nodeId/port when not strings", () => {
    const parsed = parseResultImage({ kind: "node", nodeId: 7, port: null });
    expect(parsed.nodeId).toBeUndefined();
    expect(parsed.port).toBeUndefined();
  });
});

describe("parseResultImages", () => {
  const images = [
    { kind: "output", hasOverlay: false },
    { kind: "node", nodeId: "n1", port: "in", hasOverlay: false },
    { kind: "node", nodeId: "n1", port: "reference", hasOverlay: false },
  ];

  it("accepts the images array directly, preserving order", () => {
    const parsed = parseResultImages(images);
    expect(parsed.map((i) => i.kind)).toEqual(["output", "node", "node"]);
    expect(parsed[1]?.port).toBe("in");
    expect(parsed[2]?.port).toBe("reference");
  });

  it("accepts the whole results object", () => {
    expect(parseResultImages({ hasImageResults: true, captureId: "c", images })).toHaveLength(3);
  });

  it("yields [] when images are missing or malformed", () => {
    expect(parseResultImages({})).toEqual([]);
    expect(parseResultImages({ images: "nope" })).toEqual([]);
    expect(parseResultImages(null)).toEqual([]);
  });
});

describe("parseRunMetadata", () => {
  it("keeps correctly-typed verdict fields", () => {
    expect(
      parseRunMetadata({ is_anomalous: true, confidence: 0.97, generated_text: "ok" }),
    ).toEqual({ is_anomalous: true, confidence: 0.97, generated_text: "ok" });
  });

  it("parses {} and non-objects to {} without error (Requirement 4.7)", () => {
    expect(parseRunMetadata({})).toEqual({});
    expect(parseRunMetadata(null)).toEqual({});
    expect(parseRunMetadata([1, 2])).toEqual({});
  });

  it("drops wrongly-typed and unknown fields", () => {
    expect(
      parseRunMetadata({
        is_anomalous: "yes",
        confidence: "0.9",
        generated_text: 3,
        other_tag: "kept-out",
      }),
    ).toEqual({});
    expect(parseRunMetadata({ confidence: Number.NaN })).toEqual({});
  });

  it("keeps partial verdicts (any subset of the three fields)", () => {
    expect(parseRunMetadata({ is_anomalous: false })).toEqual({ is_anomalous: false });
    expect(parseRunMetadata({ generated_text: "" })).toEqual({ generated_text: "" });
  });
});
