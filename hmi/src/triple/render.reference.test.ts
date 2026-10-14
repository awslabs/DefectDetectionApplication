/**
 * Left-panel Reference_Image rendering (hmi-payload-reference-visibility).
 *
 * When an Inspection produced no Annotated_Image — the "nothing found, no
 * objects" answer observed on adlink-dlap-701 — the left panel shows the
 * Reference_Image the node compared against, captioned as the reference so it
 * can never be mistaken for an annotation. An Annotated_Image, when present,
 * still wins; with neither image the pre-existing placeholder is unchanged
 * (Requirement 4.10 preserved: no substitute *annotation* is ever shown).
 *
 * @vitest-environment jsdom
 */

import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import type { Execution, Registration, ResultImage } from "../api/types";
import { createPanelImageLoader, panelImageUrl } from "./images";
import { initialTripleState, reduce, type TripleAppState } from "./machine";
import { TRIPLE_MESSAGES, createTripleRenderer, type TripleRenderer } from "./render";

const NAME = "blue-plate-detection-guided-inspection";
const TOKEN = "T0";
const NODE = "bedrock_1";

function registration(): Registration {
  return {
    registrationId: "reg-1",
    workflowId: "wf-1",
    name: NAME,
    version: "29",
    status: "registered",
    registeredAt: 1_700_000_000,
  };
}

function execution(): Execution {
  return {
    executionId: "exec-1",
    registrationId: "reg-1",
    status: "completed",
    startedAt: 1_700_000_100,
    finishedAt: 1_700_000_110,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: "cap-1",
  };
}

const node = (port: string): ResultImage =>
  ({ kind: "node" as const, nodeId: NODE, port, hasOverlay: false });

function stateWith(images: readonly ResultImage[]): TripleAppState {
  const bound = reduce(initialTripleState("app", NAME), {
    type: "registrations-loaded",
    registrations: [registration()],
  });
  const run = execution();
  const polled = reduce(bound, {
    type: "executions-polled",
    executions: [run],
    atEpochMs: 1_700_000_115_000,
  });
  return reduce(polled, {
    type: "run-data-loaded",
    executionId: run.executionId,
    images,
    metadata: {},
  });
}

interface Harness {
  root: HTMLElement;
  renderer: TripleRenderer;
}

function mount(): Harness {
  document.body.replaceChildren();
  const root = document.createElement("div");
  document.body.append(root);
  return {
    root,
    renderer: createTripleRenderer(
      root,
      {
        onLoginSubmit: () => undefined,
        onHistorySelect: () => undefined,
        onReturnToLive: () => undefined,
      },
      { loadImage: createPanelImageLoader({ token: () => TOKEN }) },
    ),
  };
}

/** The left (first) panel of slot 1. */
function leftPanel(root: HTMLElement): HTMLElement {
  const slot = root.querySelectorAll<HTMLElement>(".slot")[0];
  if (slot === undefined) throw new Error("no slot 1");
  const panel = slot.querySelectorAll<HTMLElement>(".image-panel")[0];
  if (panel === undefined) throw new Error("no left panel");
  return panel;
}

function label(panel: HTMLElement): string {
  return panel.querySelector(".image-label")?.textContent ?? "";
}

function img(panel: HTMLElement): HTMLImageElement {
  const found = panel.querySelector("img");
  if (found === null) throw new Error("panel has no img");
  return found;
}

function placeholderVisible(panel: HTMLElement): boolean {
  const p = panel.querySelector<HTMLElement>(".image-placeholder");
  if (p === null) throw new Error("panel has no placeholder");
  return !p.classList.contains("hidden");
}

describe("left panel Reference_Image fallback", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the reference image, captioned as the reference, when no annotation exists", () => {
    const harness = mount();
    harness.renderer.render(stateWith([node("in"), node("original"), node("reference")]));

    const panel = leftPanel(harness.root);
    expect(label(panel)).toBe(TRIPLE_MESSAGES.referenceLabel);
    expect(label(panel)).not.toBe(TRIPLE_MESSAGES.annotatedLabel);
    expect(img(panel).getAttribute("src")).toBe(
      panelImageUrl("exec-1", { nodeId: NODE, port: "reference" }, TOKEN),
    );
    expect(placeholderVisible(panel)).toBe(false);
  });

  it("prefers the annotated image and its caption when both exist", () => {
    const harness = mount();
    harness.renderer.render(
      stateWith([node("in"), node("original"), node("reference"), node("annotated")]),
    );

    const panel = leftPanel(harness.root);
    expect(label(panel)).toBe(TRIPLE_MESSAGES.annotatedLabel);
    expect(img(panel).getAttribute("src")).toBe(
      panelImageUrl("exec-1", { nodeId: NODE, port: "annotated" }, TOKEN),
    );
  });

  it("keeps the no-annotated-image placeholder when neither image exists", () => {
    const harness = mount();
    harness.renderer.render(stateWith([node("in"), node("original")]));

    const panel = leftPanel(harness.root);
    expect(label(panel)).toBe(TRIPLE_MESSAGES.annotatedLabel);
    expect(placeholderVisible(panel)).toBe(true);
    expect(img(panel).getAttribute("src")).toBeNull();
  });

  it("relabels back to ANNOTATED when a later run does have an annotation", () => {
    const harness = mount();
    harness.renderer.render(stateWith([node("original"), node("reference")]));
    expect(label(leftPanel(harness.root))).toBe(TRIPLE_MESSAGES.referenceLabel);

    harness.renderer.render(stateWith([node("original"), node("annotated")]));
    expect(label(leftPanel(harness.root))).toBe(TRIPLE_MESSAGES.annotatedLabel);
  });
});
