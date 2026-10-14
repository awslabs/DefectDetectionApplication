import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { Inspection } from "./inspections";
import { SLOT_COUNT } from "./inspections";
import {
  deriveVerdicts,
  NO_ERROR_DETAILS_MESSAGE,
  verdictLabel,
  VERDICT_PRESENTATION,
  type VerdictRunSource,
} from "./verdicts";

/**
 * Property test for the Triple_HMI's failed-run view model.
 *
 * **Feature: imts-triple-inspection-hmi, Property 13: Failed-run view model**
 *
 * **Validates: Requirements 5.9**
 *
 * Scope: only what a `failed` run derives. For any error-field content
 * (present, empty, whitespace-only, or absent) the view model is the
 * run-level failure state whose summary is the run's error text when
 * non-empty and `NO_ERROR_DETAILS_MESSAGE` otherwise; all three slots carry
 * placeholders; and no image reference from any prior run appears in any
 * slot — even when the caller hands the failed run the previously displayed
 * run's Inspections, which is exactly how a stale reference could leak.
 *
 * Verdict derivation for non-failed runs is Properties 11 and 12's subject
 * (`verdicts.test.ts`, `verdicts.placement.test.ts`) and is not re-asserted
 * here.
 */

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/**
 * Error-field content spanning every case Requirement 5.9 distinguishes:
 * absent (`null`/`undefined`), empty or whitespace-only (which must fall back
 * to the no-details message), and arbitrary present text.
 */
const errorField: fc.Arbitrary<string | null | undefined> = fc.oneof(
  { arbitrary: fc.constantFrom(null, undefined), weight: 1 },
  { arbitrary: fc.constantFrom("", " ", "   ", "\n", "\t \n"), weight: 1 },
  { arbitrary: fc.string(), weight: 2 },
  {
    arbitrary: fc.constantFrom(
      "node bedrock_2 raised RuntimeError: model timeout",
      "  padded error text  ",
      "Bedrock invocation failed (throttled)",
    ),
    weight: 2,
  },
);

const failedRun: fc.Arbitrary<VerdictRunSource> = fc
  .record({ error: errorField })
  .map(({ error }) => {
    const run: VerdictRunSource = { status: "failed" };
    // `undefined` is modeled as an absent field, not an explicit undefined.
    if (error !== undefined) run.error = error;
    return run;
  });

/**
 * Inspections of a *prior*, successfully displayed run. Their node ids and
 * ports are marked so any leak into the failed run's slots is detectable by
 * value, not just by identity.
 */
const priorInspections: fc.Arbitrary<Inspection[]> = fc.uniqueArray(
  fc
    .constantFrom("prior_bedrock_1", "prior_bedrock_2", "prior_bedrock_3", "prior_z")
    .chain((nodeId) =>
      fc.record({
        nodeId: fc.constant(nodeId),
        original: fc.option(fc.constant({ nodeId, port: "original" }), {
          nil: undefined,
        }),
        annotated: fc.option(fc.constant({ nodeId, port: "annotated" }), {
          nil: undefined,
        }),
      }),
    )
    .map(({ nodeId, original, annotated }) => {
      const inspection: Inspection = { nodeId };
      if (original !== undefined) inspection.original = original;
      if (annotated !== undefined) inspection.annotated = annotated;
      return inspection;
    }),
  { selector: (inspection) => inspection.nodeId, maxLength: 5 },
);

/**
 * Metadata payloads a failed run might still carry: absent (fetch failed),
 * empty, verdict-bearing at run level, and verdict-bearing per Inspection —
 * none of which may displace the failure state.
 */
const metadata = fc.oneof(
  fc.constantFrom(null, undefined, {} as Record<string, unknown>),
  fc.record({
    is_anomalous: fc.boolean(),
    confidence: fc.double({ min: 0, max: 1, noNaN: true }),
  }),
  fc.record({
    bedrock: fc.dictionary(
      fc.constantFrom("prior_bedrock_1", "prior_bedrock_2", "prior_z"),
      fc.record({
        is_anomalous: fc.boolean(),
        confidence: fc.double({ min: 0, max: 1, noNaN: true }),
      }),
    ),
  }),
);

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe("Property 13: Failed-run view model", () => {
  it("derives the failure state, three placeholders, and no prior-run image reference", () => {
    fc.assert(
      fc.property(
        failedRun,
        metadata,
        priorInspections,
        (run, meta, inspections) => {
          const derivation = deriveVerdicts(run, meta, inspections);

          // The run-level failure state is always present for a failed run,
          // with the error text when non-empty and the no-details message
          // otherwise (R5.9).
          const expectedSummary =
            typeof run.error === "string" && run.error.trim() !== ""
              ? run.error.trim()
              : NO_ERROR_DETAILS_MESSAGE;
          expect(derivation.failedRun).toEqual({
            errorSummary: expectedSummary,
          });
          expect(derivation.failedRun?.errorSummary).not.toBe("");

          // The failure state stands alone: no run-level pass/fail verdict is
          // derived beside it, and no more-inspections indicator (R5.9).
          expect(derivation.runLevelVerdict).toBeUndefined();
          expect(derivation.moreInspections).toBe(false);

          // All three slots are placeholders: still three slots identified
          // 1..3, but carrying no Inspection and no verdict content (R5.9).
          expect(derivation.slots).toHaveLength(SLOT_COUNT);
          expect(derivation.slots.map((slot) => slot.slotNumber)).toEqual([
            1, 2, 3,
          ]);
          for (const slot of derivation.slots) {
            expect(slot.inspection).toBeUndefined();
            expect(slot.verdict).toBeUndefined();
          }

          // No image reference from the prior run survives anywhere in the
          // slots — checked by value, so neither a copied Inspection nor a
          // bare (nodeId, port) pair can slip through (R5.9).
          const rendered = JSON.stringify(derivation.slots);
          for (const inspection of inspections) {
            expect(rendered).not.toContain(inspection.nodeId);
          }
        },
      ),
    );
  });

  it("leaves the prior run's Inspections unmutated", () => {
    fc.assert(
      fc.property(failedRun, priorInspections, (run, inspections) => {
        const snapshot = JSON.stringify(inspections);
        deriveVerdicts(run, {}, inspections);
        expect(JSON.stringify(inspections)).toBe(snapshot);
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Worked examples
// --------------------------------------------------------------------------

describe("failed-run view model examples", () => {
  const priorRunInspections: Inspection[] = [
    {
      nodeId: "bedrock_1",
      original: { nodeId: "bedrock_1", port: "original" },
      annotated: { nodeId: "bedrock_1", port: "annotated" },
    },
    {
      nodeId: "bedrock_2",
      original: { nodeId: "bedrock_2", port: "original" },
    },
  ];

  it("uses the run's error text as the summary (5.9)", () => {
    const derivation = deriveVerdicts(
      { status: "failed", error: "node bedrock_3 failed: connection reset" },
      {},
      [],
    );
    expect(derivation.failedRun).toEqual({
      errorSummary: "node bedrock_3 failed: connection reset",
    });
  });

  it("falls back to the no-details message for empty and absent errors (5.9)", () => {
    for (const run of [
      { status: "failed" as const },
      { status: "failed" as const, error: null },
      { status: "failed" as const, error: "" },
      { status: "failed" as const, error: "   " },
    ]) {
      expect(deriveVerdicts(run, {}, []).failedRun?.errorSummary).toBe(
        NO_ERROR_DETAILS_MESSAGE,
      );
    }
  });

  it("excludes the prior run's images from all three slots (5.9)", () => {
    const derivation = deriveVerdicts(
      { status: "failed", error: "boom" },
      { is_anomalous: false, confidence: 0.99 },
      priorRunInspections,
    );
    expect(derivation.slots.map((slot) => slot.inspection)).toEqual([
      undefined,
      undefined,
      undefined,
    ]);
    expect(JSON.stringify(derivation.slots)).not.toContain("bedrock_");
  });

  it("renders the failure state with its own distinct label (5.9)", () => {
    expect(verdictLabel("failed-run")).toBe("⚠ ERROR");
    expect(VERDICT_PRESENTATION["failed-run"].word).not.toBe(
      VERDICT_PRESENTATION.fail.word,
    );
  });

  it("flags metadata unavailability independently of the failure state (5.9, 4.8)", () => {
    const withMetadata = deriveVerdicts({ status: "failed" }, {}, []);
    expect(withMetadata.metadataUnavailable).toBe(false);
    expect(withMetadata.failedRun).toBeDefined();

    const withoutMetadata = deriveVerdicts({ status: "failed" }, null, []);
    expect(withoutMetadata.metadataUnavailable).toBe(true);
    expect(withoutMetadata.failedRun).toBeDefined();
  });
});
