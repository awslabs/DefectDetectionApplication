/**
 * Inspection derivation and slot assignment for the Triple_HMI
 * (Requirements 4.2, 4.3, 4.6, 4.7, 4.10, 4.11, 5.4).
 *
 * Pure module (no DOM, no network, no token): the caller supplies the parsed
 * `images` inventory of `GET /workflows/executions/{id}/results` and this
 * module derives the run's Inspections and their fixed screen slots.
 *
 * Derivation (Requirement 4.2):
 *   1. Only `kind === "node"` entries participate; entries without a `nodeId`
 *      are dropped, since a group key — and therefore any image reference —
 *      cannot exist without one.
 *   2. Entries are grouped by `nodeId`; each group is one Inspection.
 *   3. Groups are ordered by lexicographic ascending `nodeId`, and the entries
 *      within a group by lexicographic ascending `port`. The HMI sorts the
 *      inventory itself rather than trusting the server's order, so the
 *      derivation is a deterministic function of the inventory *set*: two
 *      inventories with identical entry sets — in any order — yield identical
 *      Inspection lists and identical slot assignments.
 *   4. Ports are paired within a group: `original` is the group's
 *      `original`-port entry, falling back to its `in`-port entry (a run that
 *      predates the additive executor artifacts still shows what the camera
 *      saw); `annotated` is the group's `annotated`-port entry with **no
 *      fallback of any kind** — its absence renders the no-annotated-image
 *      placeholder rather than any substitute image (Requirement 4.10).
 *
 * Slot assignment (Requirements 4.3, 4.6, 4.7, 5.4): slots 1..3 take the first
 * three Inspections in derivation order, so a given `nodeId` keeps the same
 * slot — and therefore the same slot identifier — across runs with identical
 * inventory keys. Fewer than three Inspections leaves the remaining slots
 * empty (the no-inspection-data placeholder); more than three sets the
 * more-inspections indicator.
 *
 * Every image reference carries its own Inspection's `nodeId` and its own
 * `port`, and URLs are built from the displayed run's own `executionId`, so
 * substituting an image from a different Inspection, port, or run is
 * impossible by construction (Requirements 4.11, 5.8).
 */

import type { ResultImage } from "../api/types";

// --------------------------------------------------------------------------
// Port names and slot geometry
// --------------------------------------------------------------------------

/** Node-image port holding an Inspection's Original_Image (the crop). */
export const ORIGINAL_PORT = "original";

/**
 * Fallback port for the Original_Image: the pre-existing per-node input
 * frame, used only when no `original`-port entry exists.
 */
export const ORIGINAL_FALLBACK_PORT = "in";

/** Node-image port holding an Inspection's Annotated_Image (no fallback). */
export const ANNOTATED_PORT = "annotated";

/** The Triple_HMI's fixed number of Inspection_Slots. */
export const SLOT_COUNT = 3;

// --------------------------------------------------------------------------
// View models
// --------------------------------------------------------------------------

/** A reference to one servable node image of one Inspection. */
export interface ImageRef {
  nodeId: string;
  port: string;
}

/** One per-part inspection result of a run (design "Triple view models"). */
export interface Inspection {
  nodeId: string;
  /** `original` port, falling back to `in`; absent → per-panel placeholder. */
  original?: ImageRef;
  /** `annotated` port only — no fallback (Requirement 4.10). */
  annotated?: ImageRef;
}

/** Slot identifier displayed beside each Inspection (Requirement 5.4). */
export type SlotNumber = 1 | 2 | 3;

/**
 * One of the three fixed screen regions. An absent `inspection` is the
 * no-inspection-data placeholder state (Requirement 4.6). Verdict content is
 * layered on separately (`triple/verdicts.ts`), keeping this module free of
 * any metadata dependency.
 */
export interface InspectionSlot {
  slotNumber: SlotNumber;
  inspection?: Inspection;
}

/** The three slots in fixed order. */
export type InspectionSlotTriple = [InspectionSlot, InspectionSlot, InspectionSlot];

/** The outcome of slot assignment over a run's derived Inspections. */
export interface SlotAssignment {
  slots: InspectionSlotTriple;
  /** True iff the inventory yielded more than three Inspections (R4.7). */
  moreInspections: boolean;
}

// --------------------------------------------------------------------------
// Derivation
// --------------------------------------------------------------------------

/**
 * Lexicographic (code-unit) ascending comparison — locale-independent, so the
 * ordering is identical in every browser and test environment.
 */
function compareLexicographic(a: string, b: string): number {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

function firstEntryWithPort(
  entries: readonly ResultImage[],
  port: string,
): ResultImage | undefined {
  return entries.find((entry) => entry.port === port);
}

function toImageRef(nodeId: string, entry: ResultImage): ImageRef {
  // The reference carries the Inspection's own nodeId and the entry's own
  // port — never another Inspection's or another port's identity (R4.11).
  return { nodeId, port: entry.port as string };
}

function buildInspection(
  nodeId: string,
  groupEntries: readonly ResultImage[],
): Inspection {
  // Entries within a group are ordered by lexicographic ascending port, so
  // pairing is invariant under permutation of the inventory (R4.2).
  const sorted = [...groupEntries].sort((a, b) =>
    compareLexicographic(a.port ?? "", b.port ?? ""),
  );

  const originalEntry =
    firstEntryWithPort(sorted, ORIGINAL_PORT) ??
    firstEntryWithPort(sorted, ORIGINAL_FALLBACK_PORT);
  const annotatedEntry = firstEntryWithPort(sorted, ANNOTATED_PORT);

  const inspection: Inspection = { nodeId };
  if (originalEntry !== undefined) {
    inspection.original = toImageRef(nodeId, originalEntry);
  }
  if (annotatedEntry !== undefined) {
    inspection.annotated = toImageRef(nodeId, annotatedEntry);
  }
  return inspection;
}

/**
 * Derives the run's Inspections from its results inventory (Requirement 4.2).
 *
 * Pure and total: any inventory — empty, permuted, duplicated, or carrying
 * unknown ports — yields a deterministic Inspection list, ordered by
 * lexicographic ascending `nodeId`. Never throws.
 */
export function deriveInspections(images: readonly ResultImage[]): Inspection[] {
  const groups = new Map<string, ResultImage[]>();

  for (const image of images) {
    if (image.kind !== "node") continue;
    const nodeId = image.nodeId;
    // No nodeId → no group key and no servable (nodeId, port) pair.
    if (typeof nodeId !== "string") continue;
    // No port → the entry can be neither an Original_Image nor an
    // Annotated_Image, but its nodeId still constitutes an Inspection group.
    const group = groups.get(nodeId);
    if (group === undefined) {
      groups.set(nodeId, [image]);
    } else {
      group.push(image);
    }
  }

  return [...groups.keys()]
    .sort(compareLexicographic)
    .map((nodeId) => buildInspection(nodeId, groups.get(nodeId) as ResultImage[]));
}

/**
 * Assigns derived Inspections to the three fixed Inspection_Slots
 * (Requirements 4.3, 4.6, 4.7, 5.4).
 *
 * Slots 1..3 take the first three Inspections in derivation order; remaining
 * slots carry no Inspection (the no-inspection-data placeholder); a fourth or
 * later Inspection is not displayed and sets `moreInspections`.
 */
export function assignSlots(inspections: readonly Inspection[]): SlotAssignment {
  const slots = [1, 2, 3].map((slotNumber) => {
    const slot: InspectionSlot = { slotNumber: slotNumber as SlotNumber };
    const inspection = inspections[slotNumber - 1];
    if (inspection !== undefined) slot.inspection = inspection;
    return slot;
  }) as InspectionSlotTriple;

  return { slots, moreInspections: inspections.length > SLOT_COUNT };
}
