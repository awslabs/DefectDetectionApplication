/**
 * Rendering unit tests for the Triple_HMI Kiosk_Display (task 10.3).
 *
 * These exercise `createTripleRenderer` against real DOM nodes, driving it
 * only with states the pure reducer produced (`initialTripleState` + `reduce`),
 * so nothing here re-implements verdict, ordering, or selection semantics —
 * the tests assert what the operator sees for a given state.
 *
 * Covered: three labeled Inspection_Slots (5.1, 5.2), automatic slot
 * replacement on a new run (3.6), verdict states differing by icon + word
 * (5.5), an `img` error placing a placeholder in that panel only (4.11, 5.8),
 * the empty states — no runs recorded (2.6), no terminal runs (3.7), zero
 * history (7.6) — the historical indicator, return control and tile selection
 * (7.3), the historical fetch failure (7.7), and the header contents (6.3).
 *
 * @vitest-environment jsdom
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Execution, Registration, ResultImage } from "../api/types";
import { formatDateTime, formatTime } from "../logic/format";
import { IMAGE_UNAVAILABLE_TEXT, createPanelImageLoader, panelImageUrl } from "./images";
import { initialTripleState, reduce, type TripleAppState } from "./machine";
import { TRIPLE_MESSAGES, createTripleRenderer, type TripleRenderer } from "./render";
import type { VerdictMetadata } from "./verdicts";

const NAME = "blue-plate-detection-guided-inspection";
const TOKEN = "T0";
const STARTED_AT = 1_700_000_100;
const FINISHED_AT = 1_700_000_110;
const POLLED_AT_MS = 1_700_000_115_000;

// --------------------------------------------------------------------------
// Payload builders
// --------------------------------------------------------------------------

function registration(overrides: Partial<Registration> = {}): Registration {
  return {
    registrationId: "reg-1",
    workflowId: "wf-1",
    name: NAME,
    version: "1.0.0",
    status: "registered",
    registeredAt: 1_700_000_000,
    ...overrides,
  };
}

function execution(overrides: Partial<Execution> = {}): Execution {
  return {
    executionId: "exec-1",
    registrationId: "reg-1",
    status: "completed",
    startedAt: STARTED_AT,
    finishedAt: FINISHED_AT,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: "cap-1",
    ...overrides,
  };
}

/** An `annotated` + `original` node entry per Inspection. */
function nodeImages(nodeIds: readonly string[]): ResultImage[] {
  return nodeIds.flatMap((nodeId) => [
    { kind: "node" as const, nodeId, port: "annotated", hasOverlay: false },
    { kind: "node" as const, nodeId, port: "original", hasOverlay: false },
  ]);
}

const NODE_IDS = ["node-a", "node-b", "node-c"] as const;

// --------------------------------------------------------------------------
// State builders (reducer only — no hand-written states)
// --------------------------------------------------------------------------

function boundState(): TripleAppState {
  return reduce(initialTripleState("app", NAME), {
    type: "registrations-loaded",
    registrations: [registration()],
  });
}

function polled(
  state: TripleAppState,
  executions: readonly Execution[],
  atEpochMs = POLLED_AT_MS,
): TripleAppState {
  return reduce(state, { type: "executions-polled", executions, atEpochMs });
}

function loaded(
  state: TripleAppState,
  executionId: string,
  images: readonly ResultImage[] | null,
  metadata: VerdictMetadata = {},
): TripleAppState {
  return reduce(state, { type: "run-data-loaded", executionId, images, metadata });
}

/** A displayed completed run with the three Inspections loaded. */
function displayedRunState(
  run: Execution = execution(),
  metadata: VerdictMetadata = {},
): TripleAppState {
  return loaded(
    polled(boundState(), [run]),
    run.executionId,
    nodeImages(NODE_IDS),
    metadata,
  );
}

// --------------------------------------------------------------------------
// DOM harness and query helpers
// --------------------------------------------------------------------------

interface Harness {
  root: HTMLElement;
  renderer: TripleRenderer;
  selected: string[];
  returnedToLive: number;
}

function mount(): Harness {
  document.body.replaceChildren();
  const root = document.createElement("div");
  document.body.append(root);

  const selected: string[] = [];
  const harness: Harness = {
    root,
    selected,
    returnedToLive: 0,
    renderer: createTripleRenderer(
      root,
      {
        onLoginSubmit: () => undefined,
        onHistorySelect: (executionId) => selected.push(executionId),
        onReturnToLive: () => {
          harness.returnedToLive += 1;
        },
      },
      // The real loader, so the panels' URLs and their error handling are the
      // ones production uses (Requirements 4.5, 4.11, 5.8).
      { loadImage: createPanelImageLoader({ token: () => TOKEN }) },
    ),
  } as Harness;
  return harness;
}

function one(scope: ParentNode, selector: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(selector);
  if (found === null) throw new Error(`no element matching ${selector}`);
  return found;
}

function all(scope: ParentNode, selector: string): HTMLElement[] {
  return [...scope.querySelectorAll<HTMLElement>(selector)];
}

function at<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) throw new Error(`no item at index ${index}`);
  return item;
}

function panelImg(panel: ParentNode): HTMLImageElement {
  const img = panel.querySelector("img");
  if (img === null) throw new Error("panel has no img");
  return img;
}

function visible(node: HTMLElement): boolean {
  return !node.classList.contains("hidden");
}

function text(node: HTMLElement): string {
  return node.textContent ?? "";
}

/** The [annotated, original] panels of one slot, in rendered order. */
function panels(slot: HTMLElement): HTMLElement[] {
  return all(slot, ".image-panel");
}

describe("createTripleRenderer", () => {
  beforeEach(() => {
    // The panel loader arms a 10 s timeout per image; fake timers keep those
    // out of the test run's tail.
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders three Inspection_Slots with labeled annotated and original panels (5.1, 5.2)", () => {
    const harness = mount();
    harness.renderer.render(displayedRunState());

    const slots = all(harness.root, ".slot");
    expect(slots).toHaveLength(3);
    expect(slots.every(visible)).toBe(true);

    slots.forEach((slot, index) => {
      expect(text(one(slot, ".slot-label"))).toBe(`SLOT ${index + 1}`);

      const [annotated, original] = [at(panels(slot), 0), at(panels(slot), 1)];
      expect(text(one(annotated, ".image-label"))).toBe(TRIPLE_MESSAGES.annotatedLabel);
      expect(text(one(original, ".image-label"))).toBe(TRIPLE_MESSAGES.originalLabel);

      // Each panel points at its own Inspection's own port of this run.
      const nodeId = at(NODE_IDS, index);
      expect(panelImg(annotated).getAttribute("src")).toBe(
        panelImageUrl("exec-1", { nodeId, port: "annotated" }, TOKEN),
      );
      expect(panelImg(original).getAttribute("src")).toBe(
        panelImageUrl("exec-1", { nodeId, port: "original" }, TOKEN),
      );
      expect(visible(panelImg(annotated))).toBe(true);
      expect(visible(one(annotated, ".image-placeholder"))).toBe(false);
    });
  });

  it("replaces every slot's content when a newer run is displayed (3.6)", () => {
    const harness = mount();
    const first = execution({ executionId: "exec-1" });
    harness.renderer.render(displayedRunState(first));

    const second = execution({
      executionId: "exec-2",
      startedAt: STARTED_AT + 60,
      finishedAt: FINISHED_AT + 60,
    });
    let state = polled(boundState(), [second, first]);
    state = loaded(state, "exec-2", nodeImages(NODE_IDS));
    // No operator interaction: the same renderer just receives the new state.
    harness.renderer.render(state);

    const sources = all(harness.root, ".image-panel").map((panel) =>
      panelImg(panel).getAttribute("src") ?? "",
    );
    expect(sources).toHaveLength(6);
    expect(sources.every((src) => src.includes("exec-2"))).toBe(true);
    expect(sources.some((src) => src.includes("exec-1"))).toBe(false);
    expect(harness.selected).toEqual([]);
    expect(harness.returnedToLive).toBe(0);
  });

  it("distinguishes verdict states by icon and word, not color alone (5.5)", () => {
    const harness = mount();
    harness.renderer.render(
      displayedRunState(execution(), {
        bedrock: {
          "node-a": { is_anomalous: true, confidence: 0.876 },
          "node-b": { is_anomalous: false, confidence: 0.5 },
          // node-c has no record at all -> NO VERDICT in that slot only.
        },
      }),
    );

    const labels = all(harness.root, ".slot .slot-verdict .verdict-label").map(text);
    expect(labels).toEqual(["✘ FAIL", "✔ PASS", "— NO VERDICT"]);
    // Each state's word is distinct on its own, before any styling applies.
    expect(new Set(labels.map((label) => label.split(" ").slice(1).join(" ")))).toEqual(
      new Set(["FAIL", "PASS", "NO VERDICT"]),
    );

    const verdicts = all(harness.root, ".slot .slot-verdict");
    expect(verdicts.map((node) => visible(node))).toEqual([true, true, true]);
    expect(at(verdicts, 0).classList.contains("verdict-fail")).toBe(true);
    expect(at(verdicts, 1).classList.contains("verdict-pass")).toBe(true);
    expect(at(verdicts, 2).classList.contains("verdict-none")).toBe(true);

    // Confidence accompanies a displayed verdict, rounded to 2 decimals (5.7).
    expect(text(one(at(verdicts, 0), ".verdict-confidence"))).toBe("conf 0.88");
    expect(text(one(at(verdicts, 2), ".verdict-confidence"))).toBe("");
  });

  it("places the unavailable placeholder in the failing panel only (4.11, 5.8)", () => {
    const harness = mount();
    harness.renderer.render(displayedRunState());

    const slots = all(harness.root, ".slot");
    const failing = at(panels(at(slots, 0)), 0);
    const failingImg = panelImg(failing);
    failingImg.dispatchEvent(new Event("error"));

    expect(text(one(failing, ".image-placeholder"))).toBe(IMAGE_UNAVAILABLE_TEXT);
    expect(visible(one(failing, ".image-placeholder"))).toBe(true);
    expect(visible(failingImg)).toBe(false);
    // Nothing from another Inspection, port, or run takes its place.
    expect(failingImg.getAttribute("src")).toBeNull();

    const others = all(harness.root, ".image-panel").filter(
      (panel) => panel !== failing,
    );
    expect(others).toHaveLength(5);
    for (const panel of others) {
      expect(visible(panelImg(panel))).toBe(true);
      expect(panelImg(panel).getAttribute("src")).not.toBeNull();
      expect(visible(one(panel, ".image-placeholder"))).toBe(false);
    }
  });

  it("shows the no-runs-recorded message and the zero-history message (2.6, 7.6)", () => {
    const harness = mount();
    harness.renderer.render(polled(boundState(), []));

    const message = one(harness.root, ".main-message");
    expect(visible(message)).toBe(true);
    expect(text(one(message, ".message-title"))).toBe(TRIPLE_MESSAGES.noRunsTitle);
    expect(all(harness.root, ".slot").every(visible)).toBe(false);
    // The workflow identity stays on screen in place of run content (2.6).
    expect(text(one(harness.root, ".workflow-name"))).toBe(NAME);

    expect(all(harness.root, ".history-tile")).toHaveLength(0);
    const empty = one(harness.root, ".history-empty");
    expect(visible(empty)).toBe(true);
    expect(text(empty)).toBe(TRIPLE_MESSAGES.historyEmpty);
  });

  it("shows the no-completed-runs placeholder in all three slots (3.7)", () => {
    const harness = mount();
    const running = execution({ status: "running", finishedAt: null });
    harness.renderer.render(polled(boundState(), [running]));

    expect(visible(one(harness.root, ".main-message"))).toBe(false);
    const slots = all(harness.root, ".slot");
    expect(slots).toHaveLength(3);
    for (const slot of slots) {
      expect(text(one(slot, ".slot-note"))).toBe(TRIPLE_MESSAGES.noCompletedRuns);
      for (const panel of panels(slot)) {
        const placeholder = one(panel, ".image-placeholder");
        expect(visible(placeholder)).toBe(true);
        expect(text(placeholder)).toBe(TRIPLE_MESSAGES.noCompletedRuns);
        expect(panelImg(panel).getAttribute("src")).toBeNull();
      }
    }
    // A run is in progress, so the indicator accompanies the placeholders.
    expect(visible(one(harness.root, ".in-progress-badge"))).toBe(true);
  });

  it("renders the historical indicator, selection, and return control (7.3)", () => {
    const harness = mount();
    const older = execution({ executionId: "exec-1" });
    const newer = execution({
      executionId: "exec-2",
      startedAt: STARTED_AT + 60,
      finishedAt: FINISHED_AT + 60,
    });

    const live = polled(boundState(), [newer, older]);
    harness.renderer.render(live);
    expect(visible(one(harness.root, ".history-banner"))).toBe(false);
    expect(visible(one(harness.root, ".return-to-live"))).toBe(false);
    expect(
      all(harness.root, ".history-tile").some((tile) =>
        tile.classList.contains("selected"),
      ),
    ).toBe(false);

    const historical = reduce(live, { type: "history-run-selected", run: older });
    harness.renderer.render(historical);

    expect(visible(one(harness.root, ".history-banner"))).toBe(true);
    expect(visible(one(harness.root, ".return-to-live"))).toBe(true);
    const tiles = all(harness.root, ".history-tile");
    expect(tiles.map((tile) => tile.dataset["executionId"])).toEqual([
      "exec-2",
      "exec-1",
    ]);
    expect(at(tiles, 1).classList.contains("selected")).toBe(true);
    expect(at(tiles, 0).classList.contains("selected")).toBe(false);

    at(tiles, 0).click();
    one(harness.root, ".return-to-live").click();
    expect(harness.selected).toEqual(["exec-2"]);
    expect(harness.returnedToLive).toBe(1);
  });

  it("indicates a historical fetch failure while keeping history and return (7.7)", () => {
    const harness = mount();
    const older = execution({ executionId: "exec-1" });
    const newer = execution({
      executionId: "exec-2",
      startedAt: STARTED_AT + 60,
      finishedAt: FINISHED_AT + 60,
    });

    let state = polled(boundState(), [newer, older]);
    state = reduce(state, { type: "history-run-selected", run: older });
    state = reduce(state, { type: "run-data-failed", executionId: "exec-1" });
    harness.renderer.render(state);

    const note = one(harness.root, ".run-note");
    expect(visible(note)).toBe(true);
    expect(text(note)).toContain(TRIPLE_MESSAGES.historicalDataError);
    // The history strip and the return-to-live control stay available.
    expect(all(harness.root, ".history-tile")).toHaveLength(2);
    expect(visible(one(harness.root, ".return-to-live"))).toBe(true);
    // Every slot reports the inventory as unavailable rather than showing
    // another run's images (4.9).
    for (const slot of all(harness.root, ".slot")) {
      expect(text(one(slot, ".slot-note"))).toBe(
        TRIPLE_MESSAGES.inspectionDataUnavailable,
      );
    }
  });

  it("renders the workflow name and the run's local start and finish times (6.3)", () => {
    const harness = mount();
    harness.renderer.render(displayedRunState());

    expect(text(one(harness.root, ".workflow-name"))).toBe(NAME);
    const timing = text(one(harness.root, ".run-timing"));
    expect(timing).toContain(formatDateTime(STARTED_AT));
    expect(timing).toContain(formatTime(FINISHED_AT));

    // A terminal run without a finish time omits it rather than erroring (6.9).
    harness.renderer.render(
      displayedRunState(execution({ executionId: "exec-9", finishedAt: null })),
    );
    const withoutFinish = text(one(harness.root, ".run-timing"));
    expect(withoutFinish).toContain(formatDateTime(STARTED_AT));
    expect(withoutFinish).not.toContain("Finished");
  });
});
