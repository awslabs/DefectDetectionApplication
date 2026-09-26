/*
 * Tests for the Detection_List helpers (run-detection-visibility spec).
 *
 * Covers design Property 2 (detection-list parsing is total and faithful)
 * and the display formatters.
 */

import fc from "fast-check";

import {
  UNLABELLED_DETECTION,
  detectionLabelSummary,
  formatBox,
  formatConfidence,
  runDetections,
} from "./detections";
import type { RunDetection } from "./detections";

const NUM_RUNS = 100;

const finite = fc.double({ noNaN: true, noDefaultInfinity: true });

/** A well-formed executor Detection_List entry and the RunDetection it maps to. */
const wellFormedEntryArb: fc.Arbitrary<{
  entry: Record<string, unknown>;
  expected: RunDetection;
}> = fc
  .record({
    id: fc.hexaString({ minLength: 8, maxLength: 8 }),
    label: fc.string({ minLength: 1 }),
    confidence: fc.double({ min: 0, max: 1, noNaN: true }),
    x_min: finite,
    y_min: finite,
    x_max: finite,
    y_max: finite,
  })
  .map((entry) => ({
    entry,
    expected: {
      id: entry.id,
      label: entry.label,
      confidence: entry.confidence,
      box: [entry.x_min, entry.y_min, entry.x_max, entry.y_max],
    },
  }));

/** Entries runDetections must skip: not objects, or no finite confidence. */
const malformedEntryArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.constant(null),
  fc.string(),
  fc.integer(),
  fc.array(fc.integer(), { maxLength: 3 }),
  fc.record({ label: fc.string() }),
  fc.record({
    label: fc.string(),
    confidence: fc.oneof(
      fc.constant(Number.NaN),
      fc.constant(Number.POSITIVE_INFINITY),
      fc.string(),
      fc.boolean(),
      fc.constant(null),
    ),
  }),
);

describe("Property 2: Detection-list parsing is total and faithful", () => {
  // **Validates: Requirements 2.1, 2.6, 2.8**
  it("never throws, and returns null exactly when `detections` is not an array", () => {
    fc.assert(
      fc.property(fc.jsonValue(), (value) => {
        const result = runDetections(value as Record<string, unknown>);
        const isPlainObject =
          value !== null && typeof value === "object" && !Array.isArray(value);
        const hasList =
          isPlainObject &&
          Array.isArray((value as Record<string, unknown>).detections);
        expect(result === null).toBe(!hasList);
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("never throws for any detections array, and keeps only entries with a finite confidence", () => {
    fc.assert(
      fc.property(fc.array(fc.jsonValue(), { maxLength: 12 }), (raw) => {
        const result = runDetections({ detections: raw });
        expect(result).not.toBeNull();
        for (const detection of result ?? []) {
          expect(Number.isFinite(detection.confidence)).toBe(true);
          expect(typeof detection.label).toBe("string");
          expect(detection.label.length).toBeGreaterThan(0);
        }
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("returns well-formed entries in order, unchanged", () => {
    fc.assert(
      fc.property(fc.array(wellFormedEntryArb, { maxLength: 15 }), (pairs) => {
        const result = runDetections({
          detections: pairs.map((pair) => pair.entry),
          detection_count: pairs.length,
        });
        expect(result).toEqual(pairs.map((pair) => pair.expected));
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("drops malformed entries without disturbing the others", () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.oneof(
            wellFormedEntryArb.map((pair) => ({ good: true as const, pair })),
            malformedEntryArb.map((entry) => ({ good: false as const, entry })),
          ),
          { maxLength: 15 },
        ),
        (items) => {
          const raw = items.map((item) =>
            item.good ? item.pair.entry : item.entry,
          );
          const expected = items.flatMap((item) =>
            item.good ? [item.pair.expected] : [],
          );
          expect(runDetections({ detections: raw })).toEqual(expected);
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });
});

describe("runDetections details", () => {
  it("distinguishes 'no Detection_List' (null) from 'nothing detected' ([])", () => {
    expect(runDetections(undefined)).toBeNull();
    expect(runDetections({})).toBeNull();
    expect(runDetections({ is_anomalous: 1, confidence: 0.7 })).toBeNull();
    expect(runDetections({ detections: {} })).toBeNull();
    expect(runDetections({ detections: [] })).toEqual([]);
  });

  it("labels an entry without a usable label as 'object'", () => {
    const result = runDetections({
      detections: [
        { confidence: 0.5 },
        { label: "", confidence: 0.6 },
        { label: 3, confidence: 0.7 },
      ],
    });
    expect(result?.map((d) => d.label)).toEqual([
      UNLABELLED_DETECTION,
      UNLABELLED_DETECTION,
      UNLABELLED_DETECTION,
    ]);
  });

  it("omits the box unless all four coordinates are finite numbers", () => {
    const result = runDetections({
      detections: [
        { label: "a", confidence: 0.5, x_min: 1, y_min: 2, x_max: 3 },
        { label: "b", confidence: 0.5, x_min: 1, y_min: 2, x_max: 3, y_max: "4" },
        { label: "c", confidence: 0.5, x_min: 1, y_min: 2, x_max: 3, y_max: 4 },
      ],
    });
    expect(result?.map((d) => d.box)).toEqual([undefined, undefined, [1, 2, 3, 4]]);
  });

  it("parses the executor's Detection_List as recorded on jetson-thor1", () => {
    const result = runDetections({
      is_anomalous: 1,
      confidence: 0.963534,
      detection_count: 2,
      detections: [
        {
          label: "vest",
          confidence: 0.8004248738288879,
          x_min: 53.6887451171875,
          y_min: 279.109423828125,
          x_max: 206.8390380859375,
          y_max: 520.489697265625,
          id: "7b693d25",
        },
        {
          label: "helmet",
          confidence: 0.9354003071784973,
          x_min: 92.18385009765625,
          y_min: 201.9081787109375,
          x_max: 169.87398681640624,
          y_max: 255.1636962890625,
          id: "de604231",
        },
      ],
    });
    expect(result).toEqual([
      {
        id: "7b693d25",
        label: "vest",
        confidence: 0.8004248738288879,
        box: [53.6887451171875, 279.109423828125, 206.8390380859375, 520.489697265625],
      },
      {
        id: "de604231",
        label: "helmet",
        confidence: 0.9354003071784973,
        box: [92.18385009765625, 201.9081787109375, 169.87398681640624, 255.1636962890625],
      },
    ]);
  });
});

describe("formatters", () => {
  it("formats confidence as a percentage with one decimal", () => {
    expect(formatConfidence(0.9354003071784973)).toBe("93.5%");
    expect(formatConfidence(0.2511110305786133)).toBe("25.1%");
    expect(formatConfidence(0)).toBe("0.0%");
    expect(formatConfidence(1)).toBe("100.0%");
  });

  it("formats confidence for any value in [0, 1] as N.N%", () => {
    fc.assert(
      fc.property(fc.double({ min: 0, max: 1, noNaN: true }), (value) => {
        expect(formatConfidence(value)).toMatch(/^\d{1,3}\.\d%$/);
      }),
      { numRuns: NUM_RUNS },
    );
  });

  it("formats a box as integer pixel ranges, and a missing box as '-'", () => {
    expect(
      formatBox([92.18385009765625, 201.9081787109375, 169.87398681640624, 255.1636962890625]),
    ).toBe("x 92–170, y 202–255");
    expect(formatBox(undefined)).toBe("-");
  });

  it("summarizes label counts ordered by label", () => {
    const detections: RunDetection[] = [
      "vest",
      "helmet",
      "human",
      "human",
      "no-helmet",
      "human",
      "human",
      "human",
      "helmet",
      "vest",
    ].map((label) => ({ label, confidence: 0.5 }));
    expect(detectionLabelSummary(detections)).toBe(
      "helmet 2 · human 5 · no-helmet 1 · vest 2",
    );
    expect(detectionLabelSummary([])).toBe("");
  });
});
