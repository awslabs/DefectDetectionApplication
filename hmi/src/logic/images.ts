/**
 * Image-pair selection from a run's results inventory (Requirement 5).
 *
 * `GET .../results` returns an `images` list with an optional base
 * `{kind: "output"}` entry followed by `{kind: "node", nodeId, port}`
 * entries, sorted deterministically by the backend (nodeId ascending, port
 * `in` before `reference` before unknown ports). This pure module selects,
 * from that list in its returned order, which entries the Live_View shows:
 *
 * 1. Reference := the first node entry with port `reference`, if any
 *    (5.1, 5.7 — "first listed node with a reference" is exactly the first
 *    reference entry under the backend's ordering).
 * 2. Captured := the `in` entry of the reference's node when present, else
 *    the first `in` entry in list order (5.2), else the `output` entry
 *    (5.3), else none (per-panel placeholder, 5.6).
 * 3. `hasMoreNodes` := node entries span more than one distinct nodeId
 *    (5.7 more-nodes indicator).
 * 4. No reference entry → single-panel layout: the captured frame takes the
 *    combined width (5.4).
 *
 * URL building for the selected entries lives in `api/routes.ts`; this
 * module has no token or execution context.
 */

import type { ResultImage } from "../api/types";

/** Live_View image-area layout (5.1 side-by-side, 5.4 single-panel). */
export type ImagePairLayout = "side-by-side" | "single-panel";

/** The outcome of image-pair selection over one run's results inventory. */
export interface ImagePairSelection {
  /** The first `reference`-port node entry, or null when none exists. */
  reference: ResultImage | null;
  /**
   * The Captured_Frame entry: same-node `in`, else first `in` in list
   * order, else the `output` entry, else null (no viewable image, 5.6).
   */
  captured: ResultImage | null;
  /** True iff node entries from more than one distinct nodeId exist (5.7). */
  hasMoreNodes: boolean;
  /** "single-panel" iff there is no reference entry (5.4). */
  layout: ImagePairLayout;
}

function isNodeEntry(image: ResultImage): boolean {
  return image.kind === "node";
}

/**
 * Selects the Reference_Image / Captured_Frame pair from a results `images`
 * list, operating on the list in its returned order (deterministic per the
 * backend's sorting). Pure; never throws.
 */
export function selectImagePair(images: ResultImage[]): ImagePairSelection {
  // Step 1: the first reference-port node entry, if any (5.1).
  const reference =
    images.find((img) => isNodeEntry(img) && img.port === "reference") ?? null;

  // Step 2: captured-frame fallback chain (5.2, 5.3).
  let captured: ResultImage | null = null;
  if (reference !== null && reference.nodeId !== undefined) {
    captured =
      images.find(
        (img) =>
          isNodeEntry(img) &&
          img.port === "in" &&
          img.nodeId === reference.nodeId,
      ) ?? null;
  }
  if (captured === null) {
    captured = images.find((img) => isNodeEntry(img) && img.port === "in") ?? null;
  }
  if (captured === null) {
    captured = images.find((img) => img.kind === "output") ?? null;
  }

  // Step 3: more-nodes indicator (5.7).
  const nodeIds = new Set<string | undefined>(
    images.filter(isNodeEntry).map((img) => img.nodeId),
  );
  const hasMoreNodes = nodeIds.size > 1;

  // Step 4: no reference → single-panel layout (5.4).
  const layout: ImagePairLayout =
    reference !== null ? "side-by-side" : "single-panel";

  return { reference, captured, hasMoreNodes, layout };
}
