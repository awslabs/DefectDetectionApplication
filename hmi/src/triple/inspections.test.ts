import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { ResultImage } from "../api/types";
import {
  ANNOTATED_PORT,
  ORIGINAL_FALLBACK_PORT,
  ORIGINAL_PORT,
  assignSlots,
  deriveInspections,
  type ImageRef,
  type Inspection,
} from "./inspections";

/**
 * Property test for the Triple_HMI's Inspection derivation and slot assignment.
 *
 * **Feature: imts-triple-inspection-hmi, Property 9: Deterministic inspection derivation and stable slot assignment**
 *
 * **Validates: Requirements 4.2, 4.3, 4.10, 4.11, 5.4**
 *
 * The derivation is a pure function of the results inventory *set*: node
 * entries group by `nodeId` (4.2), groups order lexicographically ascending
 * so a given `nodeId` keeps its slot identifier across runs with identical
 * inventory keys (4.3, 5.4), the Original_Image falls back from `original` to
 * `in` while the Annotated_Image has no fallback whatsoever (4.10), and every
 * image reference carries its own Inspection's `nodeId` and its own `port`, so
 * cross-inspection or cross-port substitution cannot occur (4.11).
 */

/** Node ids the inventory draws from: collisions, case, and ordering edges. */
const NODE_IDS = [
  "bedrock-a",
  "bedrock-b",
  "Bedrock-A",
  "detect-1",
  "detect-10",
  "detect-2",
  "zeta",
  "0-node",
  "",
] as const;

/** Ports the inventory draws from: the three meaningful ones plus noise. */
const PORTS = [
  ORIGINAL_PORT,
  ORIGINAL_FALLBACK_PORT,
  ANNOTATED_PORT,
  "reference",
  "out",
  "Original",
  "ANNOTATED",
  "",
] as const;

/**
 * One inventory entry. `nodeId` and `port` are optional keys, so node entries
 * without a `nodeId` (dropped) and node entries without a `port` (a group with
 * neither an Original_Image nor an Annotated_Image) both occur.
 */
const resultImage: fc.Arbitrary<ResultImage> = fc.record(
  {
    // Weighted towards node entries — output entries never participate.
    kind: fc.constantFrom<"output" | "node">("node", "node", "node", "output"),
    nodeId: fc.constantFrom(...NODE_IDS),
    port: fc.constantFrom(...PORTS),
    hasOverlay: fc.boolean(),
  },
  { requiredKeys: ["kind", "hasOverlay"] },
);

/** A small inventory keeps duplicate (nodeId, port) pairs frequent. */
const inventory = fc.array(resultImage, { maxLength: 12 });

/** An inventory paired with a full permutation of itself. */
const inventoryWithPermutation: fc.Arbitrary<[ResultImage[], ResultImage[]]> =
  inventory.chain((images) =>
    fc.tuple(
      fc.constant(images),
      images.length === 0
        ? fc.constant<ResultImage[]>([])
        : fc.shuffledSubarray(images, {
            minLength: images.length,
            maxLength: images.length,
          }),
    ),
  );

// --------------------------------------------------------------------------
// Independent oracles
// --------------------------------------------------------------------------

function lexicographic(a: string, b: string): number {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

/** The node entries that participate, i.e. those carrying a `nodeId`. */
function participating(images: readonly ResultImage[]): ResultImage[] {
  return images.filter(
    (image) => image.kind === "node" && typeof image.nodeId === "string",
  );
}

/** The distinct participating node ids, lexicographically ascending. */
function expectedNodeIds(images: readonly ResultImage[]): string[] {
  return [
    ...new Set(participating(images).map((image) => image.nodeId as string)),
  ].sort(lexicographic);
}

/** The ports present in one node's group. */
function groupPorts(
  images: readonly ResultImage[],
  nodeId: string,
): (string | undefined)[] {
  return participating(images)
    .filter((image) => image.nodeId === nodeId)
    .map((image) => image.port);
}

// --------------------------------------------------------------------------
// Properties
// --------------------------------------------------------------------------

describe("Property 9: Deterministic inspection derivation and stable slot assignment", () => {
  it("groups node entries by nodeId in lexicographic ascending order", () => {
    fc.assert(
      fc.property(inventory, (images) => {
        const inspections = deriveInspections(images);

        // One Inspection per distinct participating nodeId, no more, no
        // fewer, in lexicographic ascending order (4.2).
        expect(inspections.map((i) => i.nodeId)).toEqual(
          expectedNodeIds(images),
        );
      }),
    );
  });

  it("pairs original with its `in` fallback and annotated with no fallback", () => {
    fc.assert(
      fc.property(inventory, (images) => {
        for (const inspection of deriveInspections(images)) {
          const ports = groupPorts(images, inspection.nodeId);

          // Original_Image: the group's own `original` entry, falling back to
          // its own `in` entry, and nothing else (4.2).
          const expectedOriginalPort = ports.includes(ORIGINAL_PORT)
            ? ORIGINAL_PORT
            : ports.includes(ORIGINAL_FALLBACK_PORT)
              ? ORIGINAL_FALLBACK_PORT
              : undefined;
          expect(inspection.original?.port).toBe(expectedOriginalPort);

          // Annotated_Image: the group's own `annotated` entry with no
          // fallback of any kind — its absence stays absent (4.10).
          const expectedAnnotatedPort = ports.includes(ANNOTATED_PORT)
            ? ANNOTATED_PORT
            : undefined;
          expect(inspection.annotated?.port).toBe(expectedAnnotatedPort);
        }
      }),
    );
  });

  it("references only entries belonging to the Inspection's own nodeId and port", () => {
    fc.assert(
      fc.property(inventory, (images) => {
        const entries = participating(images);

        for (const inspection of deriveInspections(images)) {
          const refs: ImageRef[] = [
            inspection.original,
            inspection.annotated,
          ].filter((ref): ref is ImageRef => ref !== undefined);

          for (const ref of refs) {
            // The reference carries the Inspection's own nodeId, so no other
            // Inspection's image can be substituted (4.11).
            expect(ref.nodeId).toBe(inspection.nodeId);

            // ...and an entry with exactly that (nodeId, port) pair exists in
            // this very inventory, so no other port can be substituted.
            expect(
              entries.some(
                (entry) =>
                  entry.nodeId === ref.nodeId && entry.port === ref.port,
              ),
            ).toBe(true);
          }
        }
      }),
    );
  });

  it("is invariant under permutation of the inventory, for inspections and slots alike", () => {
    fc.assert(
      fc.property(inventoryWithPermutation, ([images, permuted]) => {
        const snapshot = JSON.stringify(images);

        const inspections = deriveInspections(images);
        const permutedInspections = deriveInspections(permuted);

        // Identical entry sets in any order yield identical Inspection lists
        // and identical slot assignments (4.2, 4.3, 5.4).
        expect(permutedInspections).toEqual(inspections);
        expect(assignSlots(permutedInspections)).toEqual(
          assignSlots(inspections),
        );

        // Pure: the inventory is never mutated.
        expect(JSON.stringify(images)).toBe(snapshot);
      }),
    );
  });

  it("keeps each nodeId in the same slot across runs with identical inventory keys", () => {
    fc.assert(
      fc.property(inventoryWithPermutation, ([images, permuted]) => {
        // A second run of the same workflow: same (nodeId, port) keys in a
        // different order, unrelated fields differing.
        const nextRun = permuted.map((image) => ({
          ...image,
          hasOverlay: !image.hasOverlay,
        }));

        const slotOf = (inv: readonly ResultImage[]) => {
          const byNodeId = new Map<string, number>();
          for (const slot of assignSlots(deriveInspections(inv)).slots) {
            if (slot.inspection !== undefined) {
              byNodeId.set(slot.inspection.nodeId, slot.slotNumber);
            }
          }
          return byNodeId;
        };

        // The slot identifier displayed beside a given Inspection is stable
        // from run to run (4.3, 5.4).
        expect(slotOf(nextRun)).toEqual(slotOf(images));
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Worked examples
// --------------------------------------------------------------------------

describe("deriveInspections examples", () => {
  function node(nodeId: string | undefined, port?: string): ResultImage {
    const image: ResultImage = { kind: "node", hasOverlay: false };
    if (nodeId !== undefined) image.nodeId = nodeId;
    if (port !== undefined) image.port = port;
    return image;
  }

  it("pairs the original and annotated ports of one node", () => {
    expect(
      deriveInspections([
        node("a", ANNOTATED_PORT),
        node("a", ORIGINAL_PORT),
        { kind: "output", hasOverlay: true },
      ]),
    ).toEqual<Inspection[]>([
      {
        nodeId: "a",
        original: { nodeId: "a", port: ORIGINAL_PORT },
        annotated: { nodeId: "a", port: ANNOTATED_PORT },
      },
    ]);
  });

  it("falls back to the `in` port for the original only (4.2)", () => {
    expect(deriveInspections([node("a", ORIGINAL_FALLBACK_PORT)])).toEqual<
      Inspection[]
    >([{ nodeId: "a", original: { nodeId: "a", port: ORIGINAL_FALLBACK_PORT } }]);
  });

  it("prefers the original port over the `in` fallback", () => {
    const inspections = deriveInspections([
      node("a", ORIGINAL_FALLBACK_PORT),
      node("a", ORIGINAL_PORT),
    ]);
    expect(inspections).toHaveLength(1);
    expect(inspections[0]?.original).toEqual({
      nodeId: "a",
      port: ORIGINAL_PORT,
    });
  });

  it("never substitutes anything for a missing annotated image (4.10)", () => {
    const inspections = deriveInspections([
      node("a", ORIGINAL_PORT),
      node("a", "reference"),
    ]);
    expect(inspections).toHaveLength(1);
    expect(inspections[0]?.annotated).toBeUndefined();
  });

  it("keeps a group for a node entry with no port", () => {
    expect(deriveInspections([node("a")])).toEqual<Inspection[]>([
      { nodeId: "a" },
    ]);
  });

  it("drops node entries without a nodeId", () => {
    expect(deriveInspections([node(undefined, ORIGINAL_PORT)])).toEqual([]);
  });

  it("orders groups lexicographically, not by payload order (4.3, 5.4)", () => {
    const inspections = deriveInspections([
      node("detect-2", ORIGINAL_PORT),
      node("detect-10", ORIGINAL_PORT),
      node("Detect-1", ORIGINAL_PORT),
    ]);
    expect(inspections.map((i) => i.nodeId)).toEqual([
      "Detect-1",
      "detect-10",
      "detect-2",
    ]);
  });
});
