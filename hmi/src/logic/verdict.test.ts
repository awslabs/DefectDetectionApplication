import { describe, expect, it } from "vitest";

import type { Execution, RunMetadata } from "../api/types";
import {
  GENERATED_TEXT_LIMIT,
  NO_ERROR_DETAILS_MESSAGE,
  deriveVerdict,
  formatConfidence,
} from "./verdict";

/**
 * Unit tests for verdict derivation (Requirements 4.2, 4.3, 4.4, 4.5, 4.7,
 * 4.8, 4.9). Property 9 coverage lives in the separate property-test task.
 */

function makeExecution(overrides: Partial<Execution> = {}): Execution {
  return {
    executionId: "exec-1",
    registrationId: "reg-1",
    status: "completed",
    startedAt: 1736951527,
    finishedAt: 1736951529,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: null,
    ...overrides,
  };
}

describe("deriveVerdict — pass/fail mapping (4.2)", () => {
  it("maps is_anomalous=true to fail", () => {
    const vm = deriveVerdict(makeExecution(), { is_anomalous: true });
    expect(vm.state).toBe("fail");
  });

  it("maps is_anomalous=false to pass", () => {
    const vm = deriveVerdict(makeExecution(), { is_anomalous: false });
    expect(vm.state).toBe("pass");
  });
});

describe("deriveVerdict — confidence rounding (4.3)", () => {
  it("rounds to at most 2 decimal places", () => {
    const vm = deriveVerdict(makeExecution(), { confidence: 0.96789 });
    expect(vm.confidenceText).toBe("0.97");
  });

  it("does not pad short values", () => {
    expect(deriveVerdict(makeExecution(), { confidence: 0.9 }).confidenceText).toBe("0.9");
    expect(deriveVerdict(makeExecution(), { confidence: 1 }).confidenceText).toBe("1");
  });

  it("omits confidence when the field is absent", () => {
    const vm = deriveVerdict(makeExecution(), { is_anomalous: false });
    expect(vm.confidenceText).toBeUndefined();
  });
});

describe("formatConfidence", () => {
  it("never renders more than 2 decimal places", () => {
    for (const value of [0.123456, 0.005, 12.999, 0.1 + 0.2]) {
      const text = formatConfidence(value);
      const decimals = text.split(".")[1] ?? "";
      expect(decimals.length).toBeLessThanOrEqual(2);
    }
  });
});

describe("deriveVerdict — generated_text truncation (4.4)", () => {
  it("passes short text through untruncated", () => {
    const vm = deriveVerdict(makeExecution(), { generated_text: "looks fine" });
    expect(vm.generatedText).toBe("looks fine");
    expect(vm.generatedTextTruncated).toBe(false);
  });

  it("keeps text exactly at the limit untruncated", () => {
    const text = "x".repeat(GENERATED_TEXT_LIMIT);
    const vm = deriveVerdict(makeExecution(), { generated_text: text });
    expect(vm.generatedText).toBe(text);
    expect(vm.generatedTextTruncated).toBe(false);
  });

  it("truncates text over the limit and sets the flag", () => {
    const text = "x".repeat(GENERATED_TEXT_LIMIT + 1);
    const vm = deriveVerdict(makeExecution(), { generated_text: text });
    expect(vm.generatedText).toBe(text.slice(0, GENERATED_TEXT_LIMIT));
    expect(vm.generatedText?.length).toBe(GENERATED_TEXT_LIMIT);
    expect(vm.generatedTextTruncated).toBe(true);
  });

  it("keeps empty text with the flag off", () => {
    const vm = deriveVerdict(makeExecution(), { generated_text: "" });
    expect(vm.generatedText).toBe("");
    expect(vm.generatedTextTruncated).toBe(false);
  });
});

describe("deriveVerdict — no-verdict completed runs (4.7)", () => {
  it("yields no-verdict for empty metadata without error", () => {
    const vm = deriveVerdict(makeExecution(), {});
    expect(vm.state).toBe("no-verdict");
    expect(vm.confidenceText).toBeUndefined();
    expect(vm.generatedText).toBeUndefined();
    expect(vm.metadataUnavailable).toBe(false);
  });

  it("yields no-verdict when only non-verdict fields exist", () => {
    const metadata: RunMetadata = {};
    metadata["some_other_tag"] = 42;
    const vm = deriveVerdict(makeExecution(), metadata);
    expect(vm.state).toBe("no-verdict");
  });

  it("still renders confidence and text without is_anomalous", () => {
    const vm = deriveVerdict(makeExecution(), {
      confidence: 0.5,
      generated_text: "partial",
    });
    expect(vm.state).toBe("no-verdict");
    expect(vm.confidenceText).toBe("0.5");
    expect(vm.generatedText).toBe("partial");
  });
});

describe("deriveVerdict — failed runs (4.5, 4.8)", () => {
  it("uses the run's error field as the summary", () => {
    const vm = deriveVerdict(
      makeExecution({ status: "failed", error: "camera timeout" }),
      null,
    );
    expect(vm.state).toBe("failed-run");
    expect(vm.errorSummary).toContain("camera timeout");
  });

  it("includes the failing node when present", () => {
    const vm = deriveVerdict(
      makeExecution({ status: "failed", error: "boom", failingNodeId: "node-3" }),
      null,
    );
    expect(vm.errorSummary).toContain("boom");
    expect(vm.errorSummary).toContain("node-3");
  });

  it("uses the no-details message when error fields are absent", () => {
    const vm = deriveVerdict(makeExecution({ status: "failed" }), null);
    expect(vm.errorSummary).toBe(NO_ERROR_DETAILS_MESSAGE);
  });

  it("uses the no-details message when error fields are blank", () => {
    const vm = deriveVerdict(
      makeExecution({ status: "failed", error: "   ", failingNodeId: "" }),
      null,
    );
    expect(vm.errorSummary).toBe(NO_ERROR_DETAILS_MESSAGE);
  });

  it("ignores metadata for failed runs", () => {
    const vm = deriveVerdict(
      makeExecution({ status: "failed", error: "boom" }),
      { is_anomalous: false, confidence: 0.99 },
    );
    expect(vm.state).toBe("failed-run");
    expect(vm.confidenceText).toBeUndefined();
    expect(vm.metadataUnavailable).toBe(false);
  });
});

describe("deriveVerdict — metadata unavailable (4.9)", () => {
  it("flags metadataUnavailable when metadata is null for a completed run", () => {
    const vm = deriveVerdict(makeExecution(), null);
    expect(vm.state).toBe("no-verdict");
    expect(vm.metadataUnavailable).toBe(true);
    expect(vm.errorSummary).toBeUndefined();
  });
});
