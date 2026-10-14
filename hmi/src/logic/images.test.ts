import { describe, expect, it } from "vitest";

import type { ResultImage } from "../api/types";
import { selectImagePair } from "./images";

/**
 * Unit tests for image-pair selection (Requirements 5.1, 5.2, 5.3, 5.4,
 * 5.7). Property 11 coverage lives in the separate property-test task.
 */

function output(): ResultImage {
  return { kind: "output", hasOverlay: false };
}

function node(nodeId: string, port: string): ResultImage {
  return { kind: "node", nodeId, port, hasOverlay: false };
}

describe("selectImagePair", () => {
  it("pairs the reference with the same-node in entry (5.1, 5.2)", () => {
    const images = [
      output(),
      node("vlm1", "in"),
      node("vlm1", "reference"),
    ];
    const pair = selectImagePair(images);
    expect(pair.reference).toEqual(node("vlm1", "reference"));
    expect(pair.captured).toEqual(node("vlm1", "in"));
    expect(pair.layout).toBe("side-by-side");
    expect(pair.hasMoreNodes).toBe(false);
  });

  it("selects the first reference entry in list order (5.1, 5.7)", () => {
    const images = [
      node("a", "reference"),
      node("b", "reference"),
    ];
    const pair = selectImagePair(images);
    expect(pair.reference).toEqual(node("a", "reference"));
  });

  it("falls back to the first in entry when the reference's node has none (5.2)", () => {
    const images = [
      node("a", "in"),
      node("b", "in"),
      node("c", "reference"),
    ];
    const pair = selectImagePair(images);
    expect(pair.reference).toEqual(node("c", "reference"));
    expect(pair.captured).toEqual(node("a", "in"));
  });

  it("falls back to the output entry when no in entry exists (5.3)", () => {
    const images = [output(), node("vlm1", "reference")];
    const pair = selectImagePair(images);
    expect(pair.captured).toEqual(output());
  });

  it("yields no captured entry when no in or output entry exists (5.6)", () => {
    const pair = selectImagePair([node("vlm1", "reference")]);
    expect(pair.captured).toBeNull();
    expect(pair.reference).toEqual(node("vlm1", "reference"));
  });

  it("uses single-panel layout without a reference entry (5.4)", () => {
    const pair = selectImagePair([output(), node("cv1", "in")]);
    expect(pair.reference).toBeNull();
    expect(pair.captured).toEqual(node("cv1", "in"));
    expect(pair.layout).toBe("single-panel");
  });

  it("handles an empty inventory", () => {
    const pair = selectImagePair([]);
    expect(pair).toEqual({
      reference: null,
      captured: null,
      hasMoreNodes: false,
      layout: "single-panel",
    });
  });

  it("sets hasMoreNodes iff entries span more than one nodeId (5.7)", () => {
    const oneNode = selectImagePair([
      node("a", "in"),
      node("a", "reference"),
    ]);
    expect(oneNode.hasMoreNodes).toBe(false);

    const twoNodes = selectImagePair([
      node("a", "in"),
      node("a", "reference"),
      node("b", "in"),
    ]);
    expect(twoNodes.hasMoreNodes).toBe(true);
  });

  it("ignores the output entry for the more-nodes indicator (5.7)", () => {
    const pair = selectImagePair([output(), node("a", "in")]);
    expect(pair.hasMoreNodes).toBe(false);
  });

  it("displays exactly one node's pair when multiple nodes exist (5.7)", () => {
    // Backend order: nodeId ascending, `in` before `reference`.
    const images = [
      output(),
      node("bedrock1", "in"),
      node("bedrock1", "reference"),
      node("vlm2", "in"),
      node("vlm2", "reference"),
    ];
    const pair = selectImagePair(images);
    expect(pair.reference).toEqual(node("bedrock1", "reference"));
    expect(pair.captured).toEqual(node("bedrock1", "in"));
    expect(pair.hasMoreNodes).toBe(true);
  });
});
