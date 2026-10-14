import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { ExecutionStatus } from "../api/types";
import type { Inspection } from "./inspections";
import { SLOT_COUNT } from "./inspections";
import {
  BEDROCK_METADATA_KEY,
  deriveVerdicts,
  formatConfidence,
  type RunLevelVerdict,
  type SlotVerdict,
  type VerdictRunSource,
} from "./verdicts";

/**
 * Property test for the Triple_HMI's verdict placement.
 *
 * **Feature: imts-triple-inspection-hmi, Property 12: Verdict placement without conflation**
 *
 * **Validates: Requirements 5.6, 5.11**
 *
 * Scope: only *where* each verdict lands and *which* metadata fields it may
 * come from. The two sources are strictly separated:
 *
 *   - the run-level verdict is derived only from the flat `is_anomalous` /
 *     `confidence` fields, and
 *   - per-slot verdicts only from the nested `bedrock.{nodeId}` fields.
 *
 * When only flat fields exist, the verdict appears once at the run level and
 * in no slot (Requirement 5.6). When both exist, both appear in their own
 * positions with values traceable to their own source fields (Requirement
 * 5.11).
 *
 * Separation is pinned two ways: an oracle that computes each position from
 * its own source alone, and two invariance checks — rewriting the flat fields
 * cannot move a slot verdict, and rewriting the nested records cannot move
 * the run-level verdict.
 *
 * The pass/fail/no-verdict mapping itself (Requirements 5.5, 5.7, 5.10, 5.12)
 * is Property 11's subject (`verdicts.test.ts`) and the failed-run view model
 * (Requirement 5.9) is Property 13's (`verdicts.failed.test.ts`); neither is
 * re-asserted here, and failed runs stay out of this property's domain.
 */

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/**
 * Node ids of the target workflow's shape, so sanitization is the identity
 * and a metadata key matches an Inspection exactly when the strings are
 * equal. `bedrock_9` never appears as an Inspection, so generated bedrock
 * maps also cover records that belong to no displayed slot.
 */
const NODE_IDS = ["bedrock_1", "bedrock_2", "bedrock_3", "bedrock_4"];
const UNMATCHED_NODE_ID = "bedrock_9";

/**
 * Disjoint confidence pools: every flat value renders to a text no nested
 * value can render to, and vice versa, so any leak between the two positions
 * is visible in the rendered confidence alone.
 *
 *   flat   → "0.12", "0.50", "1.00"
 *   nested → "0.43", "0.88", "0.20"
 */
const FLAT_CONFIDENCES = [0.1234, 0.5, 0.999];
const NESTED_CONFIDENCES = [0.4321, 0.87654, 0.2];

/** Values that are not booleans, so they yield no verdict at their position. */
const NON_BOOLEAN: readonly unknown[] = ["true", "false", 0, 1, null, {}];

/** The flat run-level fields (never a `bedrock` key). */
const flatFields: fc.Arbitrary<Record<string, unknown>> = fc.record(
  {
    is_anomalous: fc.oneof(
      { arbitrary: fc.boolean() as fc.Arbitrary<unknown>, weight: 3 },
      { arbitrary: fc.constantFrom(...NON_BOOLEAN), weight: 1 },
    ),
    confidence: fc.constantFrom(...FLAT_CONFIDENCES),
    generated_text: fc.constant("run summary"),
  },
  { requiredKeys: [] },
);

/** One `bedrock.{nodeId}` record. */
const bedrockRecord: fc.Arbitrary<Record<string, unknown>> = fc.record(
  {
    is_anomalous: fc.oneof(
      { arbitrary: fc.boolean() as fc.Arbitrary<unknown>, weight: 3 },
      { arbitrary: fc.constantFrom(...NON_BOOLEAN), weight: 1 },
    ),
    confidence: fc.constantFrom(...NESTED_CONFIDENCES),
    text: fc.constant("bedrock answer"),
  },
  { requiredKeys: [] },
);

/** A `bedrock.{nodeId}` value: usually a record, sometimes not an object. */
const bedrockValue: fc.Arbitrary<unknown> = fc.oneof(
  { arbitrary: bedrockRecord as fc.Arbitrary<unknown>, weight: 4 },
  { arbitrary: fc.constantFrom<unknown>("not-an-object", 42, null, []), weight: 1 },
);

/** The nested `bedrock` map, absent as often as present. */
const bedrockMap: fc.Arbitrary<Record<string, unknown> | undefined> = fc.oneof(
  {
    arbitrary: fc.dictionary(
      fc.constantFrom(...NODE_IDS, UNMATCHED_NODE_ID),
      bedrockValue,
      { maxKeys: 5 },
    ),
    weight: 4,
  },
  { arbitrary: fc.constant(undefined), weight: 1 },
);

/** Inspections in derivation order, spanning 0..4 (the clamping boundary). */
const inspections: fc.Arbitrary<Inspection[]> = fc.uniqueArray(
  fc.constantFrom(...NODE_IDS).map(
    (nodeId): Inspection => ({
      nodeId,
      original: { nodeId, port: "original" },
      annotated: { nodeId, port: "annotated" },
    }),
  ),
  { selector: (inspection) => inspection.nodeId, maxLength: 4 },
);

/**
 * Non-failed runs only: a failed run's early return (no images, no verdicts)
 * belongs to Property 13.
 */
const run: fc.Arbitrary<VerdictRunSource> = fc
  .constantFrom<ExecutionStatus>("completed", "pending", "running")
  .map((status) => ({ status }));

// --------------------------------------------------------------------------
// Oracle — each position computed from its own source alone
// --------------------------------------------------------------------------

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function composeMetadata(
  flat: Record<string, unknown>,
  bedrock: Record<string, unknown> | undefined,
): Record<string, unknown> {
  const metadata: Record<string, unknown> = { ...flat };
  if (bedrock !== undefined) metadata[BEDROCK_METADATA_KEY] = bedrock;
  return metadata;
}

/** The record a displayed Inspection owns, or `undefined` when it owns none. */
function recordFor(
  bedrock: Record<string, unknown> | undefined,
  nodeId: string,
): Record<string, unknown> | undefined {
  const value = bedrock?.[nodeId];
  return isPlainObject(value) ? value : undefined;
}

/** The run-level verdict the flat fields alone imply. */
function expectedRunLevelVerdict(
  flat: Record<string, unknown>,
): RunLevelVerdict | undefined {
  if (typeof flat.is_anomalous !== "boolean") return undefined;
  const verdict: RunLevelVerdict = {
    state: flat.is_anomalous ? "fail" : "pass",
  };
  const confidenceText = formatConfidence(flat.confidence);
  if (confidenceText !== undefined) verdict.confidenceText = confidenceText;
  return verdict;
}

/** The slot verdict one Inspection's own record alone implies. */
function expectedSlotVerdict(
  record: Record<string, unknown> | undefined,
): SlotVerdict {
  if (typeof record?.is_anomalous !== "boolean") return { state: "no-verdict" };
  const verdict: SlotVerdict = {
    state: record.is_anomalous ? "fail" : "pass",
  };
  const confidenceText = formatConfidence(record.confidence);
  if (confidenceText !== undefined) verdict.confidenceText = confidenceText;
  return verdict;
}

/** Asserts the placement contract for one (run, flat, nested) combination. */
function expectPlacement(
  source: VerdictRunSource,
  flat: Record<string, unknown>,
  bedrock: Record<string, unknown> | undefined,
  inspectionList: readonly Inspection[],
): void {
  const { slots, runLevelVerdict } = deriveVerdicts(
    source,
    composeMetadata(flat, bedrock),
    inspectionList,
  );

  const displayed = inspectionList.slice(0, SLOT_COUNT);
  const records = displayed.map((inspection) =>
    recordFor(bedrock, inspection.nodeId),
  );
  const hasPerInspectionData = records.some((record) => record !== undefined);

  // The run level reads the flat fields and nothing else: present exactly
  // when the flat `is_anomalous` is boolean, with its own `confidence`
  // (R5.6, R5.11).
  expect(runLevelVerdict).toEqual(expectedRunLevelVerdict(flat));

  slots.forEach((slot, index) => {
    if (index >= displayed.length) {
      // An empty slot is a placeholder, never a home for the run-level
      // verdict (R5.6).
      expect(slot.inspection).toBeUndefined();
      expect(slot.verdict).toBeUndefined();
      return;
    }

    if (!hasPerInspectionData) {
      // Flat-only metadata: the verdict stays at the run level and is not
      // duplicated into any slot (R5.6).
      expect(slot.verdict).toBeUndefined();
      return;
    }

    // Both sources present: each slot renders its own Inspection's nested
    // record — never the flat fields (R5.11).
    expect(slot.verdict).toEqual(expectedSlotVerdict(records[index]));
  });

  // Values are traceable to their own source fields: the disjoint confidence
  // pools mean a slot can never render the run-level confidence text, nor
  // the run level a slot's (R5.11).
  const flatConfidenceText = formatConfidence(flat.confidence);
  for (const slot of slots) {
    const slotConfidenceText =
      slot.verdict !== undefined && "confidenceText" in slot.verdict
        ? slot.verdict.confidenceText
        : undefined;
    if (slotConfidenceText !== undefined) {
      expect(slotConfidenceText).not.toBe(flatConfidenceText);
      expect(NESTED_CONFIDENCES.map((value) => value.toFixed(2))).toContain(
        slotConfidenceText,
      );
    }
  }
  if (runLevelVerdict?.confidenceText !== undefined) {
    expect(FLAT_CONFIDENCES.map((value) => value.toFixed(2))).toContain(
      runLevelVerdict.confidenceText,
    );
  }
}

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe("Property 12: Verdict placement without conflation", () => {
  it("places each verdict at the position its own metadata source owns", () => {
    fc.assert(
      fc.property(
        run,
        flatFields,
        bedrockMap,
        inspections,
        (source, flat, bedrock, inspectionList) => {
          expectPlacement(source, flat, bedrock, inspectionList);
        },
      ),
    );
  });

  it("keeps slot verdicts unchanged when only the flat run-level fields change", () => {
    fc.assert(
      fc.property(
        run,
        flatFields,
        flatFields,
        bedrockMap,
        inspections,
        (source, flatA, flatB, bedrock, inspectionList) => {
          const withA = deriveVerdicts(
            source,
            composeMetadata(flatA, bedrock),
            inspectionList,
          );
          const withB = deriveVerdicts(
            source,
            composeMetadata(flatB, bedrock),
            inspectionList,
          );
          // No flat field can reach a slot (R5.6, R5.11).
          expect(withA.slots).toEqual(withB.slots);
        },
      ),
    );
  });

  it("keeps the run-level verdict unchanged when only the nested records change", () => {
    fc.assert(
      fc.property(
        run,
        flatFields,
        bedrockMap,
        bedrockMap,
        inspections,
        (source, flat, bedrockA, bedrockB, inspectionList) => {
          const withA = deriveVerdicts(
            source,
            composeMetadata(flat, bedrockA),
            inspectionList,
          );
          const withB = deriveVerdicts(
            source,
            composeMetadata(flat, bedrockB),
            inspectionList,
          );
          // No nested record can reach the run level (R5.6, R5.11).
          expect(withA.runLevelVerdict).toEqual(withB.runLevelVerdict);
        },
      ),
    );
  });
});

// --------------------------------------------------------------------------
// Worked examples at the placement boundary
// --------------------------------------------------------------------------

describe("verdict placement examples", () => {
  const threeInspections: Inspection[] = ["bedrock_1", "bedrock_2", "bedrock_3"].map(
    (nodeId) => ({
      nodeId,
      original: { nodeId, port: "original" },
      annotated: { nodeId, port: "annotated" },
    }),
  );

  const completed: VerdictRunSource = { status: "completed" };

  it("renders a flat-only verdict once at the run level and in no slot (5.6)", () => {
    const { slots, runLevelVerdict } = deriveVerdicts(
      completed,
      { is_anomalous: true, confidence: 0.917 },
      threeInspections,
    );

    expect(runLevelVerdict).toEqual({ state: "fail", confidenceText: "0.92" });
    expect(slots.map((slot) => slot.verdict)).toEqual([
      undefined,
      undefined,
      undefined,
    ]);
    // The slots still show their images — only verdict content is absent.
    expect(slots.map((slot) => slot.inspection?.nodeId)).toEqual([
      "bedrock_1",
      "bedrock_2",
      "bedrock_3",
    ]);
  });

  it("renders both sources in their own positions with their own values (5.11)", () => {
    const { slots, runLevelVerdict } = deriveVerdicts(
      completed,
      {
        is_anomalous: false,
        confidence: 0.1234,
        bedrock: {
          bedrock_1: { is_anomalous: true, confidence: 0.4321 },
          bedrock_2: { is_anomalous: false, confidence: 0.87654 },
          bedrock_3: { is_anomalous: true, confidence: 0.2 },
        },
      },
      threeInspections,
    );

    // Run level: the flat fields, unaffected by the three nested verdicts.
    expect(runLevelVerdict).toEqual({ state: "pass", confidenceText: "0.12" });
    // Slots: each Inspection's own nested record, never the flat pass/0.12.
    expect(slots.map((slot) => slot.verdict)).toEqual([
      { state: "fail", confidenceText: "0.43" },
      { state: "pass", confidenceText: "0.88" },
      { state: "fail", confidenceText: "0.20" },
    ]);
  });

  it("renders nested-only verdicts in the slots with no run-level verdict (5.11)", () => {
    const { slots, runLevelVerdict } = deriveVerdicts(
      completed,
      {
        bedrock: {
          bedrock_1: { is_anomalous: false, confidence: 0.4321 },
          bedrock_3: { is_anomalous: true },
        },
      },
      threeInspections,
    );

    expect(runLevelVerdict).toBeUndefined();
    expect(slots.map((slot) => slot.verdict)).toEqual([
      { state: "pass", confidenceText: "0.43" },
      { state: "no-verdict" },
      { state: "fail" },
    ]);
  });

  it("does not let a nested record supply the run-level verdict (5.6, 5.11)", () => {
    const { runLevelVerdict } = deriveVerdicts(
      completed,
      { bedrock: { bedrock_1: { is_anomalous: true, confidence: 0.99 } } },
      threeInspections,
    );

    expect(runLevelVerdict).toBeUndefined();
  });
});
