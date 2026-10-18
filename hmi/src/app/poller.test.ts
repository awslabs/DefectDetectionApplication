import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClient } from "../api/client";
import {
  executionMetadataUrl,
  executionResultsUrl,
  registrationExecutionsUrl,
} from "../api/routes";
import { parseResultImages, type Execution } from "../api/types";
import { selectImagePair } from "../logic/images";
import { initialState, reduce, type AppEvent, type AppState } from "./machine";
import {
  EXECUTIONS_LIMIT,
  POLL_INTERVAL_MS,
  createPoller,
  type Poller,
} from "./poller";

/**
 * Live run data of the single-inspection poller (Requirement 4.1).
 *
 * Driven like `triple/poller.test.ts`: vitest fake timers over a scripted
 * `fetch` (the `configureApiClient` seam), with the real `app/machine.ts`
 * reducer as the dispatch target. What is asserted is which run-data routes
 * are requested, how many times, and what reaches the Live_View.
 */

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const REGISTRATION_ID = "reg-1";
const EXECUTIONS_URL = registrationExecutionsUrl(REGISTRATION_ID, EXECUTIONS_LIMIT);

function execution(executionId: string, overrides: Partial<Execution> = {}): Execution {
  return {
    executionId,
    registrationId: REGISTRATION_ID,
    status: "completed",
    startedAt: 100,
    finishedAt: 200,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: `cap-${executionId}`,
    ...overrides,
  };
}

/** A `/results` body in the LocalServer's order: output, then node entries. */
const RESULTS_BODY = {
  hasImageResults: true,
  captureId: "cap-run-a",
  images: [
    { kind: "output", hasOverlay: false },
    { kind: "node", nodeId: "n2", port: "in", hasOverlay: false },
    { kind: "node", nodeId: "n2", port: "reference", hasOverlay: false },
  ],
};

/** The image pair the Live_View shows for RESULTS_BODY. */
const RESULTS_PAIR = selectImagePair(parseResultImages(RESULTS_BODY));

// --------------------------------------------------------------------------
// Scripted LocalServer
// --------------------------------------------------------------------------

interface Reply {
  status: number;
  body?: unknown;
}

interface Script {
  executions: Reply;
  results: Reply;
  metadata: Reply;
}

interface Harness {
  poller: Poller;
  script: Script;
  getState: () => AppState;
  /** How many times `url` was requested. */
  count: (url: string) => number;
}

function harness(script: Partial<Script> = {}): Harness {
  const requests: string[] = [];
  const scripted: Script = {
    executions: { status: 200, body: [] },
    results: { status: 200, body: RESULTS_BODY },
    metadata: { status: 200, body: { is_anomalous: false, confidence: 0.9 } },
    ...script,
  };

  const fetchFn: typeof fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    const reply = url.endsWith("/results")
      ? scripted.results
      : url.endsWith("/metadata")
        ? scripted.metadata
        : scripted.executions;
    return new Response(JSON.stringify(reply.body ?? {}), {
      status: reply.status,
      headers: { "Content-Type": "application/json" },
    });
  };
  configureApiClient({ fetchFn });

  // In the app with the registration selected: where `main.ts` bootstrap
  // stands before its first `pollNow()`.
  let state = reduce(initialState("app"), {
    type: "registration-selected",
    registrationId: REGISTRATION_ID,
  });
  const poller = createPoller({
    getState: () => state,
    dispatch: (event: AppEvent) => {
      state = reduce(state, event);
    },
  });

  return {
    poller,
    script: scripted,
    getState: () => state,
    count: (url) => requests.filter((seen) => seen === url).length,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  resetApiClient();
});

// --------------------------------------------------------------------------
// Results for a completed run in live mode (4.1)
// --------------------------------------------------------------------------

describe("live run data for a completed run (4.1)", () => {
  it("fetches /results for a run that completes while the page is open", async () => {
    const h = harness({
      executions: {
        status: 200,
        body: [execution("run-a", { status: "running", finishedAt: null })],
      },
    });
    await h.poller.pollNow();
    h.poller.start();
    expect(h.count(executionResultsUrl("run-a"))).toBe(0);

    // The run completes while the page is open.
    h.script.executions = { status: 200, body: [execution("run-a")] };
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);

    expect(h.count(executionMetadataUrl("run-a"))).toBe(1);
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    expect(h.getState().live.mode).toBe("live");
    expect(h.getState().live.displayedRun?.executionId).toBe("run-a");
    expect(h.getState().live.images).toEqual(RESULTS_PAIR);

    // Later cycles reuse the cached inventory: one request per new run.
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    h.poller.stop();
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
  });

  it("fetches /results for the latest completed run on the first poll", async () => {
    const h = harness({ executions: { status: 200, body: [execution("run-a")] } });

    await h.poller.pollNow();

    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    expect(h.getState().live.displayedRun?.executionId).toBe("run-a");
    expect(h.getState().live.images).toEqual(RESULTS_PAIR);
  });
});

// --------------------------------------------------------------------------
// A completed run without results: bounded, never once per cycle
// --------------------------------------------------------------------------

describe("a completed run without results", () => {
  it("requests an empty inventory once and shows it", async () => {
    const h = harness({
      executions: { status: 200, body: [execution("run-a", { hasImageResults: false })] },
      results: {
        status: 200,
        body: { hasImageResults: false, captureId: null, images: [] },
      },
    });

    await h.poller.pollNow();
    h.poller.start();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 10);
    // A forced refresh (return to live) reuses the cached inventory too.
    await h.poller.pollNow();
    h.poller.stop();

    expect(h.count(EXECUTIONS_URL)).toBe(12);
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    // "No viewable image" rather than a placeholder that never stops loading.
    expect(h.getState().live.images).toEqual(selectImagePair([]));
  });

  it("asks again after a failed /results only when the run is loaded again", async () => {
    const h = harness({
      executions: { status: 200, body: [execution("run-a")] },
      results: { status: 503 },
    });

    await h.poller.pollNow();
    h.poller.start();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 10);

    // One request when the run became the latest; ten cycles add none.
    expect(h.count(EXECUTIONS_URL)).toBe(11);
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    expect(h.getState().live.images).toBeNull();

    // The failure is not final: the next forced refresh asks once more and
    // shows the inventory the route now returns.
    h.script.results = { status: 200, body: RESULTS_BODY };
    await h.poller.pollNow();
    h.poller.stop();

    expect(h.count(executionResultsUrl("run-a"))).toBe(2);
    expect(h.getState().live.images).toEqual(RESULTS_PAIR);
  });
});
