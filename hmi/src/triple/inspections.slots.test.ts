import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { ResultImage } from "../api/types";
import {
  assignSlots,
  deriveInspections,
  SLOT_COUNT,
  type Inspection,
} from "./inspections";

/**
 * Property test for the Triple_HMI's slot-count clamping.
 *
 * **Feature: imts-triple-inspection-hmi, Property 10: Slot-count clamping**
 *
 * **Validates: Requirements 4.6, 4.7**
 *
 * Scope: only the clamping of the derived Inspection list onto the three
 * fixed Inspection_Slots. Fewer than three Inspections → exactly the derived
 * Inspections occupy their assigned slots and every remaining slot carries the
 * no-inspection-data placeholder (an absent `inspection`, Requirement 4.6).
 * More than three → exactly the first three in derivation order are displayed
 * and the more-inspections indicator is set, and only then (Requirement 4.7).
 *
 * The derivation's grouping, ordering, and port pairing are Property 9's
 * subject (`inspections.test.ts`) and are deliberately not re-asserted here;
 * this property takes the derived list as given and pins only what slot
 * assignment does with its length.
 */

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/**
 * A nodeId pool small enough that group counts straddle the three-slot
 * boundary: inventories yielding 0, 1, 2, 3, and >3 Inspections all occur
 * frequently.
 */
const NODE_IDS = ["bedrock_1", "bedrock_2", "bedrock_3", "bedrock_4", "a", "z"];

const PORTS = ["annotated", "original", "in", "reference", "other"];

/** Node entries (the only kind that can yield an Inspection). */
const nodeEntry: fc.Arbitrary<ResultImage> = fc.record({
  kind: fc.constant<"node">("node"),
  nodeId: fc.constantFrom(...NODE_IDS),
  port: fc.constantFrom(...PORTS),
  hasOverlay: fc.boolean(),
});

/** Output entries and node entries without a nodeId yield no Inspection. */
const nonInspectionEntry: fc.Arbitrary<ResultImage> = fc.oneof(
  fc.record({
    kind: fc.constant<"output">("output"),
    hasOverlay: fc.boolean(),
  }),
  fc.record({
    kind: fc.constant<"node">("node"),
    port: fc.constantFrom(...PORTS),
    hasOverlay: fc.boolean(),
  }),
);

const inventory = fc.array(
  fc.oneof({ arbitrary: nodeEntry, weight: 4 }, { arbitrary: nonInspectionEntry, weight: 1 }),
  { maxLength: 20 },
);

/** Inspection lists fed straight to `assignSlots`, spanning 0..6 entries. */
const inspectionList = fc.uniqueArray(
  fc.constantFrom(...NODE_IDS).map((nodeId): Inspection => ({
    nodeId,
    original: { nodeId, port: "original" },
    annotated: { nodeId, port: "annotated" },
  })),
  { selector: (inspection) => inspection.nodeId, maxLength: 6 },
);

// --------------------------------------------------------------------------
// Shared oracle
// --------------------------------------------------------------------------

/** Asserts the clamping contract for one derived Inspection list. */
function expectClamped(inspections: readonly Inspection[]): void {
  const { slots, moreInspections } = assignSlots(inspections);

  // The screen always has exactly three slots, identified 1..3 (R4.6): a
  // short inventory shrinks the filled count, never the layout.
  expect(slots).toHaveLength(SLOT_COUNT);
  expect(slots.map((slot) => slot.slotNumber)).toEqual([1, 2, 3]);

  const displayed = Math.min(inspections.length, SLOT_COUNT);

  // Slots 1..displayed carry exactly the first `displayed` Inspections, in
  // derivation order and by identity — no copy, no substitute (R4.6, R4.7);
  // every remaining slot is the no-inspection-data placeholder (R4.6).
  slots.forEach((slot, index) => {
    if (index < displayed) {
      expect(slot.inspection).toBe(inspections[index]);
    } else {
      expect(slot.inspection).toBeUndefined();
    }
  });
  expect(slots.filter((slot) => slot.inspection !== undefined)).toHaveLength(
    displayed,
  );
  expect(slots.filter((slot) => slot.inspection === undefined)).toHaveLength(
    SLOT_COUNT - displayed,
  );

  // The more-inspections indicator is set iff the inventory yielded more
  // than three Inspections — never for three or fewer (R4.7).
  expect(moreInspections).toBe(inspections.length > SLOT_COUNT);

  // Nothing beyond the first three reaches the screen (R4.7).
  const shownNodeIds = slots
    .map((slot) => slot.inspection?.nodeId)
    .filter((nodeId): nodeId is string => nodeId !== undefined);
  for (const dropped of inspections.slice(SLOT_COUNT)) {
    expect(shownNodeIds).not.toContain(dropped.nodeId);
  }
  expect(shownNodeIds).toEqual(
    inspections.slice(0, SLOT_COUNT).map((inspection) => inspection.nodeId),
  );
}

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe("Property 10: Slot-count clamping", () => {
  it("clamps any results inventory to three slots with placeholders and the more-inspections indicator", () => {
    fc.assert(
      fc.property(inventory, (images) => {
        expectClamped(deriveInspections(images));
      }),
    );
  });

  it("clamps any Inspection list, and leaves it unmutated", () => {
    fc.assert(
      fc.property(inspectionList, (inspections) => {
        const snapshot = JSON.stringify(inspections);
        expectClamped(inspections);
        expect(JSON.stringify(inspections)).toBe(snapshot);
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Worked examples at the clamping boundary
// --------------------------------------------------------------------------

describe("slot-count clamping examples", () => {
  function node(nodeId: string, port: string): ResultImage {
    return { kind: "node", nodeId, port, hasOverlay: false };
  }

  function inspectionsFor(count: number): Inspection[] {
    const images: ResultImage[] = [];
    for (let index = 1; index <= count; index += 1) {
      images.push(node(`bedrock_${index}`, "original"));
      images.push(node(`bedrock_${index}`, "annotated"));
    }
    return deriveInspections(images);
  }

  it("fills all three slots for exactly three Inspections, no indicator (4.6, 4.7)", () => {
    const { slots, moreInspections } = assignSlots(inspectionsFor(3));
    expect(slots.map((slot) => slot.inspection?.nodeId)).toEqual([
      "bedrock_1",
      "bedrock_2",
      "bedrock_3",
    ]);
    expect(moreInspections).toBe(false);
  });

  it("placeholders the trailing slots for one Inspection (4.6)", () => {
    const { slots, moreInspections } = assignSlots(inspectionsFor(1));
    expect(slots[0].inspection?.nodeId).toBe("bedrock_1");
    expect(slots[1].inspection).toBeUndefined();
    expect(slots[2].inspection).toBeUndefined();
    expect(moreInspections).toBe(false);
  });

  it("placeholders all three slots for an empty inventory (4.6)", () => {
    const { slots, moreInspections } = assignSlots(deriveInspections([]));
    expect(slots.map((slot) => slot.inspection)).toEqual([
      undefined,
      undefined,
      undefined,
    ]);
    expect(moreInspections).toBe(false);
  });

  it("shows the first three and sets the indicator for four Inspections (4.7)", () => {
    const { slots, moreInspections } = assignSlots(inspectionsFor(4));
    expect(slots.map((slot) => slot.inspection?.nodeId)).toEqual([
      "bedrock_1",
      "bedrock_2",
      "bedrock_3",
    ]);
    expect(moreInspections).toBe(true);
  });
});
