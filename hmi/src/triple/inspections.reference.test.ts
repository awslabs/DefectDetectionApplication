/**
 * Reference_Image derivation (hmi-payload-reference-visibility).
 *
 * The `reference` port is what the Inspection compared the crop against
 * (decoded from the trigger payload by the Bedrock node). It is derived
 * independently of `annotated`, so a "nothing found" answer — which produces
 * no Annotated_Image — still yields a displayable comparison image.
 */
import { describe, expect, it } from "vitest";

import { deriveInspections, REFERENCE_PORT } from "./inspections";
import type { ResultImage } from "../api/types";

const node = (nodeId: string, port: string): ResultImage =>
  ({ kind: "node", nodeId, port, hasOverlay: false }) as ResultImage;

/** The single derived Inspection, asserted to exist (strict indexed access). */
function only(images: readonly ResultImage[]) {
  const [inspection, ...rest] = deriveInspections(images);
  if (inspection === undefined) throw new Error("no inspection derived");
  if (rest.length > 0) throw new Error("expected exactly one inspection");
  return inspection;
}

describe("Reference_Image derivation", () => {
  it("derives the reference ref alongside original and annotated", () => {
    const inspection = only([
      node("bedrock_1", "in"),
      node("bedrock_1", "original"),
      node("bedrock_1", "annotated"),
      node("bedrock_1", REFERENCE_PORT),
    ]);
    expect(inspection.original).toEqual({ nodeId: "bedrock_1", port: "original" });
    expect(inspection.annotated).toEqual({ nodeId: "bedrock_1", port: "annotated" });
    expect(inspection.reference).toEqual({ nodeId: "bedrock_1", port: REFERENCE_PORT });
  });

  it("derives the reference ref when the answer produced no annotation", () => {
    // The real shape of the observed run: in + original + reference, no
    // annotated entry because the answer carried no objects list.
    const inspection = only([
      node("bedrock_2", "in"),
      node("bedrock_2", "original"),
      node("bedrock_2", REFERENCE_PORT),
    ]);
    expect(inspection.annotated).toBeUndefined();
    expect(inspection.reference).toEqual({ nodeId: "bedrock_2", port: REFERENCE_PORT });
  });

  it("leaves reference absent when the inventory has no reference entry", () => {
    const inspection = only([
      node("bedrock_3", "in"),
      node("bedrock_3", "original"),
    ]);
    expect(inspection.reference).toBeUndefined();
  });

  it("never confuses one Inspection's reference with another's", () => {
    const inspections = deriveInspections([
      node("bedrock_2", REFERENCE_PORT),
      node("bedrock_1", REFERENCE_PORT),
    ]);
    expect(inspections.map((i) => i.reference?.nodeId)).toEqual(["bedrock_1", "bedrock_2"]);
  });
});
