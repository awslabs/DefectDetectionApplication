import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { Inspection } from "./inspections";
import { SLOT_COUNT } from "./inspections";
import {
  BEDROCK_METADATA_KEY,
  deriveVerdicts,
  formatConfidence,
  verdictLabel,
  type InspectionSlotVM,
  type VerdictMetadata,
} from "./verdicts";

/**
 * Property test for the Triple_HMI's per-Inspection verdict derivation.
 *
 * **Feature: imts-triple-inspection-hmi, Property 11: Per-inspection verdict derivation**
 *
 * **Validates: Requirements 5.5, 5.7, 5.10, 5.12**
 *
 * Scope: what a *slot* shows for its own Inspection. A slot's verdict is FAIL
 * iff its `bedrock.{nodeId}.is_anomalous` is boolean `true`, PASS iff boolean
 * `false`, and no verdict when the value is absent or non-boolean — decided
 * independently per slot (Requirements 5.5, 5.12); any displayed `confidence`
 * renders rounded to exactly 2 decimal places (Requirement 5.7); and a
 * completed run whose metadata lacks all verdict fields yields no verdict
 * content and no error state (Requirement 5.10).
 *
 * Deliberately out of scope: the flat-vs-nested placement split (Property 12,
 * `verdicts.placement.test.ts`) and the failed-run view model (Property 13,
 * `verdicts.failed.test.ts`), so each property keeps exactly one test. Slot
 * numbering and clamping belong to Properties 9 and 10 (`inspections*.test.ts`)
 * and are only used here, not re-asserted.
 *
 * "No verdict" is read from the view model as the absence of a rendered
 * pass/fail: either an explicit `{ state: "no-verdict" }` or an absent
 * `verdict` (the state the module uses when the run carries no per-Inspection
 * verdict data at all). Both render the same no-verdict indication, and
 * `history.ts` treats them identically.
 */

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/**
 * Filename-safe node ids that are their own sanitized form and pairwise
 * distinct under sanitization, so a metadata key matches exactly the
 * Inspection whose `nodeId` it spells. Raw-vs-sanitized matching is exercised
 * by the worked examples below.
 */
const NODE_IDS = ["bedrock_1", "bedrock_2", "bedrock_3", "bedrock_4", "a.b-c"];

/** Confidence values, including the non-finite and non-numeric shapes. */
const confidenceValue: fc.Arbitrary<unknown> = fc.oneof(
  fc.double({ min: 0, max: 1, noNaN: true }),
  fc.double({ min: -5, max: 5, noNaN: true }),
  fc.constantFrom(Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY),
  fc.constantFrom("0.75", null, true),
);

/** One `bedrock.{nodeId}` value: verdict-bearing, verdict-less, or not a record. */
const bedrockValue: fc.Arbitrary<unknown> = fc.oneof(
  // Boolean is_anomalous, with and without a confidence field.
  fc.record(
    { is_anomalous: fc.boolean(), confidence: confidenceValue, text: fc.string() },
    { requiredKeys: ["is_anomalous"] },
  ),
  // Non-boolean is_anomalous → no verdict for this slot only (R5.12).
  fc.record({
    is_anomalous: fc.constantFrom<unknown>("true", 1, 0, null, [], {}),
    confidence: confidenceValue,
  }),
  // Present record with the verdict field absent entirely (R5.12).
  fc.record({ text: fc.string() }),
  // Values that are not records at all.
  fc.constantFrom<unknown>("anomalous", 3, null, true, [{ is_anomalous: true }]),
);

/** A `bedrock` map keyed by node ids, some of which may not be displayed. */
const bedrockMap: fc.Arbitrary<Record<string, unknown>> = fc
  .uniqueArray(fc.constantFrom(...NODE_IDS), { maxLength: NODE_IDS.length })
  .chain((keys) =>
    fc
      .tuple(...keys.map(() => bedrockValue))
      .map((values) =>
        Object.fromEntries(keys.map((key, index) => [key, values[index]])),
      ),
  );

/**
 * Inspections in derivation order (ascending `nodeId`), 0..5 of them, each
 * carrying its own image references.
 */
const inspections: fc.Arbitrary<Inspection[]> = fc
  .uniqueArray(fc.constantFrom(...NODE_IDS), { maxLength: NODE_IDS.length })
  .map((nodeIds) =>
    [...nodeIds].sort().map((nodeId) => ({
      nodeId,
      original: { nodeId, port: "original" },
      annotated: { nodeId, port: "annotated" },
    })),
  );

/**
 * Metadata payloads: with and without the nested `bedrock` map, and with and
 * without the flat run-level fields, so slot derivation is exercised against
 * payloads that also carry run-level data.
 */
const metadata: fc.Arbitrary<VerdictMetadata> = fc.oneof(
  fc.constant<VerdictMetadata>({}),
  bedrockMap.map((bedrock) => ({ [BEDROCK_METADATA_KEY]: bedrock })),
  bedrockMap.chain((bedrock) =>
    fc
      .record(
        { is_anomalous: fc.boolean(), confidence: confidenceValue },
        { requiredKeys: [] },
      )
      .map((flat) => ({ ...flat, [BEDROCK_METADATA_KEY]: bedrock })),
  ),
  fc.record(
    { is_anomalous: fc.boolean(), confidence: confidenceValue },
    { requiredKeys: [] },
  ),
);

// --------------------------------------------------------------------------
// Oracle helpers
// --------------------------------------------------------------------------

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/** The `bedrock.{nodeId}` record a slot's verdict must come from. */
function sourceRecord(
  payload: VerdictMetadata,
  nodeId: string,
): Record<string, unknown> | undefined {
  const bedrock = asRecord(asRecord(payload)?.[BEDROCK_METADATA_KEY]);
  return bedrock === undefined ? undefined : asRecord(bedrock[nodeId]);
}

/** The rendered verdict of a slot, with an absent verdict read as no-verdict. */
function displayedState(
  slot: InspectionSlotVM,
): "pass" | "fail" | "no-verdict" {
  return slot.verdict?.state ?? "no-verdict";
}

/** What a slot actually shows: its state plus any rendered confidence. */
function displayedVerdict(slot: InspectionSlotVM): {
  state: "pass" | "fail" | "no-verdict";
  confidenceText?: string;
} {
  const verdict = slot.verdict;
  const confidenceText =
    verdict !== undefined && verdict.state !== "no-verdict"
      ? verdict.confidenceText
      : undefined;
  return { state: displayedState(slot), confidenceText };
}

// --------------------------------------------------------------------------
// Properties
// --------------------------------------------------------------------------

describe("Property 11: Per-inspection verdict derivation", () => {
  it("derives each slot's verdict from its own bedrock is_anomalous value only", () => {
    fc.assert(
      fc.property(inspections, metadata, (derived, payload) => {
        const { slots } = deriveVerdicts(
          { status: "completed" },
          payload,
          derived,
        );

        expect(slots).toHaveLength(SLOT_COUNT);

        slots.forEach((slot) => {
          if (slot.inspection === undefined) {
            // An empty slot renders the no-inspection-data placeholder, never
            // a verdict borrowed from elsewhere.
            expect(slot.verdict).toBeUndefined();
            return;
          }

          const record = sourceRecord(payload, slot.inspection.nodeId);
          const isAnomalous = record?.is_anomalous;

          // FAIL iff boolean true, PASS iff boolean false, no verdict when
          // absent or non-boolean — for this slot alone (R5.5, R5.12).
          if (isAnomalous === true) {
            expect(displayedState(slot)).toBe("fail");
          } else if (isAnomalous === false) {
            expect(displayedState(slot)).toBe("pass");
          } else {
            expect(displayedState(slot)).toBe("no-verdict");
          }
        });
      }),
    );
  });

  it("renders every displayed confidence rounded to exactly 2 decimal places", () => {
    fc.assert(
      fc.property(inspections, metadata, (derived, payload) => {
        const { slots } = deriveVerdicts(
          { status: "completed" },
          payload,
          derived,
        );

        slots.forEach((slot) => {
          const verdict = slot.verdict;
          if (verdict === undefined || verdict.state === "no-verdict") {
            // A no-verdict slot carries no confidence to render (R5.7).
            expect((verdict as { confidenceText?: string })?.confidenceText).toBeUndefined();
            return;
          }

          const confidence = slot.inspection === undefined
            ? undefined
            : sourceRecord(payload, slot.inspection.nodeId)?.confidence;
          const finite =
            typeof confidence === "number" && Number.isFinite(confidence);

          if (!finite) {
            // Nothing renderable — the verdict shows without a confidence
            // rather than with NaN.
            expect(verdict.confidenceText).toBeUndefined();
            return;
          }

          expect(verdict.confidenceText).toBe(
            (confidence as number).toFixed(2),
          );
          // Exactly two decimals, trailing zeros retained (R5.7).
          expect(verdict.confidenceText).toMatch(/^-?\d+\.\d{2}$/);
        });
      }),
    );
  });

  it("decides each slot independently: changing one node's record leaves the others' verdicts identical", () => {
    fc.assert(
      fc.property(
        inspections,
        bedrockMap,
        fc.constantFrom(...NODE_IDS),
        bedrockValue,
        (derived, bedrock, mutatedNodeId, replacement) => {
          const before = deriveVerdicts(
            { status: "completed" },
            { [BEDROCK_METADATA_KEY]: bedrock },
            derived,
          );
          const after = deriveVerdicts(
            { status: "completed" },
            { [BEDROCK_METADATA_KEY]: { ...bedrock, [mutatedNodeId]: replacement } },
            derived,
          );

          before.slots.forEach((slot, index) => {
            const mutatedSlot = after.slots[index] as InspectionSlotVM;
            if (
              slot.inspection === undefined ||
              slot.inspection.nodeId === mutatedNodeId
            ) {
              return;
            }
            // Untouched slots keep their own outcome, whatever happened to
            // the mutated node's data (R5.12). Compared as displayed, since
            // an absent verdict and an explicit no-verdict render alike.
            expect(displayedVerdict(mutatedSlot)).toEqual(
              displayedVerdict(slot),
            );
            expect(mutatedSlot.inspection).toBe(slot.inspection);
          });
        },
      ),
    );
  });

  it("yields no verdict content and no error state for a completed run lacking all verdict fields", () => {
    const verdictLessMetadata: fc.Arbitrary<VerdictMetadata> = fc.oneof(
      fc.constant<VerdictMetadata>({}),
      fc.constant<VerdictMetadata>({ generated_text: "no defects" }),
      fc.constant<VerdictMetadata>({ [BEDROCK_METADATA_KEY]: {} }),
      fc.record({ detection_count: fc.nat(), confidence: fc.double({ min: 0, max: 1, noNaN: true }) }),
      bedrockMap.map((bedrock) => ({
        // Records stripped of every verdict field: present, but verdict-less.
        [BEDROCK_METADATA_KEY]: Object.fromEntries(
          Object.keys(bedrock).map((key) => [key, { text: "answer" }]),
        ),
      })),
    );

    fc.assert(
      fc.property(inspections, verdictLessMetadata, (derived, payload) => {
        const result = deriveVerdicts({ status: "completed" }, payload, derived);

        // Images and status still display; no verdict, no failure state,
        // no unavailable-metadata flag (R5.10).
        result.slots.forEach((slot, index) => {
          expect(slot.inspection).toBe(derived[index]);
          expect(displayedState(slot)).toBe("no-verdict");
        });
        expect(result.runLevelVerdict).toBeUndefined();
        expect(result.failedRun).toBeUndefined();
        expect(result.metadataUnavailable).toBe(false);
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Worked examples
// --------------------------------------------------------------------------

describe("per-inspection verdict examples", () => {
  const three: Inspection[] = ["bedrock_1", "bedrock_2", "bedrock_3"].map(
    (nodeId) => ({
      nodeId,
      original: { nodeId, port: "original" },
      annotated: { nodeId, port: "annotated" },
    }),
  );

  it("maps true → fail, false → pass, and a non-boolean → no verdict per slot (5.5, 5.12)", () => {
    const { slots } = deriveVerdicts(
      { status: "completed" },
      {
        bedrock: {
          bedrock_1: { is_anomalous: true, confidence: 0.9 },
          bedrock_2: { is_anomalous: false, confidence: 0.5 },
          bedrock_3: { is_anomalous: "true", confidence: 0.7 },
        },
      },
      three,
    );

    expect(slots.map((slot) => slot.verdict?.state)).toEqual([
      "fail",
      "pass",
      "no-verdict",
    ]);
    expect(slots[2].verdict).toEqual({ state: "no-verdict" });
  });

  it("shows a no-verdict indication only in the slots whose data is missing (5.12)", () => {
    const { slots } = deriveVerdicts(
      { status: "completed" },
      { bedrock: { bedrock_2: { is_anomalous: false } } },
      three,
    );

    expect(slots.map((slot) => slot.verdict?.state)).toEqual([
      "no-verdict",
      "pass",
      "no-verdict",
    ]);
  });

  it("matches a raw metadata node id against the Inspection's sanitized id (5.5)", () => {
    const nodeId = "bedrock_1";
    const { slots } = deriveVerdicts(
      { status: "completed" },
      { bedrock: { "bedrock 1": { is_anomalous: true, confidence: 0.42 } } },
      [{ nodeId, original: { nodeId, port: "original" } }],
    );

    expect(slots[0].verdict).toEqual({ state: "fail", confidenceText: "0.42" });
  });

  it("rounds confidence to exactly 2 decimals, keeping trailing zeros (5.7)", () => {
    expect(formatConfidence(0.5)).toBe("0.50");
    expect(formatConfidence(0.999)).toBe("1.00");
    expect(formatConfidence(1)).toBe("1.00");
    expect(formatConfidence(0.12345)).toBe("0.12");
    expect(formatConfidence(Number.NaN)).toBeUndefined();
    expect(formatConfidence("0.5")).toBeUndefined();
  });

  it("labels pass and fail with distinct words, not color alone (5.5)", () => {
    expect(verdictLabel("pass")).not.toBe(verdictLabel("fail"));
    expect(verdictLabel("pass")).toContain("PASS");
    expect(verdictLabel("fail")).toContain("FAIL");
    expect(verdictLabel("no-verdict")).toContain("NO VERDICT");
  });

  it("renders images and status with no verdict content for an empty metadata object (5.10)", () => {
    const result = deriveVerdicts({ status: "completed" }, {}, three);

    expect(result.slots.map((slot) => slot.inspection?.nodeId)).toEqual([
      "bedrock_1",
      "bedrock_2",
      "bedrock_3",
    ]);
    expect(result.slots.every((slot) => slot.verdict === undefined)).toBe(true);
    expect(result.runLevelVerdict).toBeUndefined();
    expect(result.failedRun).toBeUndefined();
  });
});
