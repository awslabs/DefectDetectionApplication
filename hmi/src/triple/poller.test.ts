import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_TIMEOUT_MS,
  configureApiClient,
  resetApiClient,
} from "../api/client";
import {
  executionMetadataUrl,
  executionResultsUrl,
  registrationExecutionsUrl,
  registrationsUrl,
} from "../api/routes";
import {
  SESSION_STORAGE_KEY,
  clearRetainedCredentials,
  type SessionStorageLike,
} from "../auth/session";
import type { Execution, Registration } from "../api/types";
import { IMAGE_TIMEOUT_MS } from "./images";
import {
  initialTripleState,
  reduce,
  type TripleAppState,
  type TripleEvent,
} from "./machine";
import {
  EXECUTIONS_LIMIT,
  POLL_INTERVAL_MS,
  REGISTRATIONS_REFRESH_EVERY,
  RETRY_INTERVAL_MS,
  RUN_DATA_ATTEMPTS,
  createTriplePoller,
  type TriplePoller,
} from "./poller";

/**
 * Unit tests for the Triple_HMI polling loop (task 11.2).
 *
 * The poller is the effectful timer shell around the pure reducer, so these
 * tests drive it with vitest fake timers over a scripted `fetch` (the
 * `configureApiClient` seam `api/client.test.ts` uses) and a real
 * `triple/machine.ts` reducer as the dispatch target. What is asserted is the
 * *wiring*: which route is requested, when, how many times, and which events
 * reach the reducer.
 *
 * _Requirements: 2.1, 2.4, 3.1, 4.1, 4.5, 4.8, 4.9, 8.2, 8.4, 8.6, 8.7, 8.8_
 *
 * The 10-second **per-image** timeout of Requirement 4.5 lives in
 * `triple/images.ts` (`IMAGE_TIMEOUT_MS`) and is covered by
 * `triple/images.test.ts`; it is referenced here rather than re-tested, while
 * the 10-second bound on the poller's own JSON requests (the `/results` and
 * `/metadata` fetches of Requirement 4.9) is exercised below through the
 * shared client's timeout.
 */

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const TARGET = "blue-plate-detection-guided-inspection";
const REGISTRATION_ID = "reg-1";
const EXECUTIONS_URL = registrationExecutionsUrl(REGISTRATION_ID, EXECUTIONS_LIMIT);

function registration(overrides: Partial<Registration> = {}): Registration {
  return {
    registrationId: REGISTRATION_ID,
    workflowId: "wf-1",
    name: TARGET,
    version: "1.0.0",
    status: "registered",
    registeredAt: 1_000,
    ...overrides,
  };
}

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

/** A minimal `/results` inventory yielding one Inspection. */
const RESULTS_BODY = {
  images: [
    { kind: "node", nodeId: "inspect-1", port: "original", hasOverlay: false },
    { kind: "node", nodeId: "inspect-1", port: "annotated", hasOverlay: false },
  ],
};

const METADATA_BODY = { is_anomalous: false, confidence: 0.9 };

// --------------------------------------------------------------------------
// Scripted LocalServer
// --------------------------------------------------------------------------

/** A scripted route outcome: a response, a rejected fetch, or a stall. */
type Reply = { status: number; body?: unknown } | "network-error" | "never";

interface Script {
  registrations: Reply;
  executions: Reply;
  results: Reply;
  metadata: Reply;
}

/** In-memory session storage holding an unexpired Session_Token. */
function makeStorage(): SessionStorageLike {
  const data = new Map<string, string>([
    [SESSION_STORAGE_KEY, JSON.stringify({ token: "tok", expiresAt: 9e9 })],
  ]);
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
}

interface Harness {
  poller: TriplePoller;
  script: Script;
  requests: string[];
  events: TripleEvent[];
  getState: () => TripleAppState;
  /** How many times `url` was requested. */
  count: (url: string) => number;
}

function harness(initial: TripleAppState, script: Partial<Script> = {}): Harness {
  const requests: string[] = [];
  const events: TripleEvent[] = [];
  let state = initial;

  const scripted: Script = {
    registrations: { status: 200, body: [registration()] },
    executions: { status: 200, body: [] },
    results: { status: 200, body: RESULTS_BODY },
    metadata: { status: 200, body: METADATA_BODY },
    ...script,
  };

  function replyFor(url: string): Reply {
    if (url === registrationsUrl()) return scripted.registrations;
    if (url.endsWith("/results")) return scripted.results;
    if (url.endsWith("/metadata")) return scripted.metadata;
    return scripted.executions;
  }

  const fetchFn: typeof fetch = async (input, init) => {
    const url = String(input);
    requests.push(url);
    const reply = replyFor(url);
    if (reply === "network-error") {
      throw new TypeError("fetch failed");
    }
    if (reply === "never") {
      // Settles only when the client's own 10-second timeout aborts it.
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    }
    return new Response(JSON.stringify(reply.body ?? {}), {
      status: reply.status,
      headers: { "Content-Type": "application/json" },
    });
  };

  configureApiClient({ fetchFn, storage: makeStorage() });

  const poller = createTriplePoller({
    getState: () => state,
    dispatch: (event) => {
      events.push(event);
      state = reduce(state, event);
    },
  });

  return {
    poller,
    script: scripted,
    requests,
    events,
    getState: () => state,
    count: (url) => requests.filter((seen) => seen === url).length,
  };
}

/** Bound + connected: one registrations payload with an active name match. */
function boundState(): TripleAppState {
  return reduce(initialTripleState("app", TARGET), {
    type: "registrations-loaded",
    registrations: [registration()],
  });
}

/** Not deployed: a registrations payload with no active name match (2.4). */
function notDeployedState(): TripleAppState {
  return reduce(initialTripleState("app", TARGET), {
    type: "registrations-loaded",
    registrations: [registration({ name: "other-workflow" })],
  });
}

function eventsOfType<T extends TripleEvent["type"]>(
  events: readonly TripleEvent[],
  type: T,
): Extract<TripleEvent, { type: T }>[] {
  return events.filter((event) => event.type === type) as Extract<
    TripleEvent,
    { type: T }
  >[];
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2025-01-01T00:00:00Z"));
  clearRetainedCredentials();
});

afterEach(() => {
  vi.useRealTimers();
  resetApiClient();
  clearRetainedCredentials();
});

// --------------------------------------------------------------------------
// Registrations after the token (2.1)
// --------------------------------------------------------------------------

describe("registrations fetch (2.1)", () => {
  it("retrieves the registrations and binds the Target_Workflow", async () => {
    const h = harness(initialTripleState("app", TARGET));

    const registrations = await h.poller.refreshRegistrations();

    expect(h.requests).toEqual([registrationsUrl()]);
    expect(registrations).toEqual([registration()]);
    expect(h.getState().binding).toEqual({
      state: "bound",
      registration: registration(),
    });
    // Any 2xx also reports the connected state (8.3, 8.4).
    expect(h.getState().connection.state).toBe("connected");
    expect(h.getState().connection.lastSuccessfulUpdate).toBe(Date.now());
  });

  it("reports the failure and binds nothing when the request fails", async () => {
    const h = harness(initialTripleState("app", TARGET), {
      registrations: "network-error",
    });

    expect(await h.poller.refreshRegistrations()).toBeNull();
    expect(h.getState().binding).toEqual({ state: "pending" });
    expect(h.getState().connection.state).toBe("disconnected");
  });
});

// --------------------------------------------------------------------------
// Poll cadence (3.1) and connected steady state (8.4)
// --------------------------------------------------------------------------

describe("connected poll cadence (3.1, 8.4)", () => {
  it("polls the bounded executions route once every 2 seconds", async () => {
    const h = harness(boundState());
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS - 1);
    expect(h.count(EXECUTIONS_URL)).toBe(0);

    await vi.advanceTimersByTimeAsync(1);
    expect(h.count(EXECUTIONS_URL)).toBe(1);

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(h.count(EXECUTIONS_URL)).toBe(2);

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(h.count(EXECUTIONS_URL)).toBe(3);

    h.poller.stop();
    // The polled route is the additive bounded one, with limit=10 (3.1).
    expect(EXECUTIONS_URL).toContain("limit=10");
    for (const url of h.requests) expect(url).toBe(EXECUTIONS_URL);
  });

  it("keeps refreshing while connected and re-checks registrations periodically", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
    });
    h.poller.start();

    // One executions poll per cycle, and the periodic registrations refresh
    // exactly once in the first REGISTRATIONS_REFRESH_EVERY cycles (8.5).
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * REGISTRATIONS_REFRESH_EVERY);
    h.poller.stop();

    expect(h.count(EXECUTIONS_URL)).toBe(REGISTRATIONS_REFRESH_EVERY);
    expect(h.count(registrationsUrl())).toBe(1);
    // Steady state: connected throughout, no staleness accounting (8.4).
    expect(h.getState().connection.state).toBe("connected");
    expect(h.getState().connection.consecutivePollFailures).toBe(0);
    expect(h.getState().connection.lastSuccessfulUpdate).toBe(Date.now());
    expect(h.getState().live.displayed?.execution.executionId).toBe("run-a");
  });
});

// --------------------------------------------------------------------------
// Not-deployed re-check cadence and automatic re-bind (2.4, 8.8)
// --------------------------------------------------------------------------

describe("not-deployed re-check and automatic re-bind (2.4, 8.8)", () => {
  it("re-checks registrations each cycle and resumes the Live_View on a match", async () => {
    const h = harness(notDeployedState(), {
      registrations: { status: 200, body: [registration({ name: "other-workflow" })] },
      executions: { status: 200, body: [execution("run-a")] },
    });
    h.poller.start();

    // While not deployed the cycle spends itself on registrations, never on
    // the executions route of a workflow it is not bound to (2.4).
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2);
    expect(h.count(registrationsUrl())).toBe(2);
    expect(h.count(EXECUTIONS_URL)).toBe(0);
    expect(h.getState().binding).toEqual({ state: "not-deployed" });

    // The workflow is deployed again: the next re-check binds it and resumes
    // the Live_View in the same cycle, with no operator interaction (8.8).
    h.script.registrations = { status: 200, body: [registration()] };
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    h.poller.stop();

    expect(h.count(registrationsUrl())).toBe(3);
    expect(h.count(EXECUTIONS_URL)).toBe(1);
    expect(h.getState().binding).toEqual({
      state: "bound",
      registration: registration(),
    });
    expect(h.getState().live.displayed?.execution.executionId).toBe("run-a");
  });
});

// --------------------------------------------------------------------------
// Disconnected retry cadence (8.2) and reconnect refresh (8.6, 8.7)
// --------------------------------------------------------------------------

describe("disconnected retry cadence (8.2)", () => {
  it("probes the registrations route every 10 seconds with no upper limit", async () => {
    const h = harness(boundState(), {
      executions: { status: 500 },
      registrations: "network-error",
    });
    h.poller.start();

    // The failing poll disconnects (8.1) and counts toward staleness (3.9).
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(h.getState().connection.state).toBe("disconnected");
    expect(h.count(registrationsUrl())).toBe(0);

    // No probe before the 10-second retry interval.
    await vi.advanceTimersByTimeAsync(RETRY_INTERVAL_MS - 1);
    expect(h.count(registrationsUrl())).toBe(0);

    await vi.advanceTimersByTimeAsync(1);
    expect(h.count(registrationsUrl())).toBe(1);

    // Failing probes keep retrying, unlimited, at the same interval.
    await vi.advanceTimersByTimeAsync(RETRY_INTERVAL_MS);
    expect(h.count(registrationsUrl())).toBe(2);
    await vi.advanceTimersByTimeAsync(RETRY_INTERVAL_MS);
    expect(h.count(registrationsUrl())).toBe(3);

    h.poller.stop();
    // No executions poll is attempted while disconnected.
    expect(h.count(EXECUTIONS_URL)).toBe(1);
    expect(h.getState().connection.state).toBe("disconnected");
  });
});

describe("reconnect refresh (8.6, 8.7)", () => {
  it("refreshes the Live_View and history unconditionally when nothing changed", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
    });
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(eventsOfType(h.events, "run-data-loaded")).toHaveLength(1);

    // Connectivity drops, and the payload is unchanged throughout.
    h.script.executions = "network-error";
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(h.getState().connection.state).toBe("disconnected");

    h.script.executions = { status: 200, body: [execution("run-a")] };
    await vi.advanceTimersByTimeAsync(RETRY_INTERVAL_MS);
    h.poller.stop();

    // The 2xx probe reconnects and the same cycle re-polls the executions
    // route, so the history summary is refreshed (8.7)...
    expect(h.getState().connection.state).toBe("connected");
    expect(h.count(registrationsUrl())).toBe(1);
    expect(h.count(EXECUTIONS_URL)).toBe(3);
    // ...and the Live_View data is pushed again even though the latest
    // terminal run never changed (8.6).
    const loaded = eventsOfType(h.events, "run-data-loaded");
    expect(loaded).toHaveLength(2);
    expect(loaded[1]!.executionId).toBe("run-a");
    // Cached immutable run data costs no extra requests.
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    expect(h.count(executionMetadataUrl("run-a"))).toBe(1);
    expect(h.getState().live.history.map((entry) => entry.executionId)).toEqual([
      "run-a",
    ]);
  });

  it("picks up runs that completed during the disconnected period", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
    });
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(h.getState().live.displayed?.execution.executionId).toBe("run-a");

    h.script.executions = { status: 503 };
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(h.getState().connection.state).toBe("disconnected");

    // A newer run completed while the kiosk was offline.
    h.script.executions = {
      status: 200,
      body: [execution("run-b", { startedAt: 300, finishedAt: 400 }), execution("run-a")],
    };
    await vi.advanceTimersByTimeAsync(RETRY_INTERVAL_MS);
    h.poller.stop();

    expect(h.getState().connection.state).toBe("connected");
    expect(h.getState().live.displayed?.execution.executionId).toBe("run-b");
    expect(h.count(executionResultsUrl("run-b"))).toBe(1);
    expect(h.count(executionMetadataUrl("run-b"))).toBe(1);
    expect(h.getState().live.history.map((entry) => entry.executionId)).toEqual([
      "run-b",
      "run-a",
    ]);
  });
});

// --------------------------------------------------------------------------
// Run data on a new terminal run (4.1)
// --------------------------------------------------------------------------

describe("run data on a new terminal run (4.1)", () => {
  it("fetches /results and /metadata once per new terminal run", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
    });
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    expect(h.count(executionMetadataUrl("run-a"))).toBe(1);

    // An unchanged latest terminal run costs no further run-data requests.
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    expect(h.count(executionMetadataUrl("run-a"))).toBe(1);

    // A new terminal run triggers exactly one pair of requests for it.
    h.script.executions = {
      status: 200,
      body: [execution("run-a"), execution("run-b", { startedAt: 300, finishedAt: 400 })],
    };
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    h.poller.stop();

    expect(h.count(executionResultsUrl("run-b"))).toBe(1);
    expect(h.count(executionMetadataUrl("run-b"))).toBe(1);
    const displayed = h.getState().live.displayed;
    expect(displayed?.execution.executionId).toBe("run-b");
    expect(displayed?.dataPending).toBe(false);
    expect(displayed?.resultsUnavailable).toBe(false);
    expect(displayed?.metadataUnavailable).toBe(false);
  });

  it("requests no run data for a failed run", async () => {
    const h = harness(boundState(), {
      executions: {
        status: 200,
        body: [execution("run-x", { status: "failed", error: "camera offline" })],
      },
    });
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    h.poller.stop();

    expect(h.count(executionResultsUrl("run-x"))).toBe(0);
    expect(h.count(executionMetadataUrl("run-x"))).toBe(0);
    expect(h.getState().live.displayed?.execution.status).toBe("failed");
  });
});

// --------------------------------------------------------------------------
// Retry-once wiring (4.8, 4.9) and the 10-second request bound (4.5)
// --------------------------------------------------------------------------

describe("run-data retry-once wiring (4.8, 4.9)", () => {
  it("retries a failing /metadata exactly once and reports verdicts unavailable (4.8)", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
      metadata: { status: 503 },
    });
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    h.poller.stop();

    // The initial attempt plus exactly one retry, and no more.
    expect(h.count(executionMetadataUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS);
    expect(RUN_DATA_ATTEMPTS).toBe(2);
    const loaded = eventsOfType(h.events, "run-data-loaded");
    expect(loaded).toHaveLength(1);
    expect(loaded[0]!.metadata).toBeNull();
    expect(loaded[0]!.images).not.toBeNull();
    // The run's images and status still render, flagged verdict-unavailable.
    const displayed = h.getState().live.displayed;
    expect(displayed?.metadataUnavailable).toBe(true);
    expect(displayed?.resultsUnavailable).toBe(false);
  });

  it("retries a failing /results exactly once and reports inspection data unavailable (4.9)", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
      results: "network-error",
    });
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    h.poller.stop();

    expect(h.count(executionResultsUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS);
    const loaded = eventsOfType(h.events, "run-data-loaded");
    expect(loaded).toHaveLength(1);
    expect(loaded[0]!.images).toBeNull();
    expect(loaded[0]!.metadata).not.toBeNull();
    expect(h.getState().live.displayed?.resultsUnavailable).toBe(true);
  });

  it("reports a run-data failure when both routes fail after their retry (4.8, 4.9)", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
      results: { status: 503 },
      metadata: "network-error",
    });
    h.poller.start();

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    h.poller.stop();

    expect(h.count(executionResultsUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS);
    expect(h.count(executionMetadataUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS);
    expect(eventsOfType(h.events, "run-data-loaded")).toHaveLength(0);
    expect(eventsOfType(h.events, "run-data-failed")).toHaveLength(1);
    expect(h.getState().live.historicalDataError).toBe(true);
  });

  it("re-attempts a cached failure for an operator's historical selection (7.7)", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
      results: { status: 503 },
    });
    h.poller.start();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    h.poller.stop();
    expect(h.count(executionResultsUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS);

    // The route recovers; re-selecting the run retries rather than serving
    // the cached failure.
    h.script.results = { status: 200, body: RESULTS_BODY };
    await h.poller.loadHistoricalRun(execution("run-a"));

    expect(h.count(executionResultsUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS + 1);
    expect(h.getState().live.displayed?.resultsUnavailable).toBe(false);
  });
});

describe("10-second request timeout (4.5, 4.9)", () => {
  it("bounds each run-data attempt by the shared client's 10-second timeout", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
      results: "never",
    });
    h.poller.start();

    // Attempt 1 is still outstanding one tick before its timeout expires.
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + DEFAULT_TIMEOUT_MS - 1);
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);

    // At the 10-second bound it times out and the single retry starts.
    await vi.advanceTimersByTimeAsync(1);
    expect(h.count(executionResultsUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS);

    // The retry stalls too and is bounded the same way.
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS);
    h.poller.stop();

    expect(DEFAULT_TIMEOUT_MS).toBe(10_000);
    expect(h.count(executionResultsUrl("run-a"))).toBe(RUN_DATA_ATTEMPTS);
    expect(h.getState().live.displayed?.resultsUnavailable).toBe(true);
    // A timeout is a connection loss, so the retry cadence takes over (8.1).
    expect(h.getState().connection.state).toBe("disconnected");
  });

  it("uses the same 10-second bound for image requests, covered in images.test.ts (4.5)", () => {
    // The per-image timeout lives in `triple/images.ts` and is exercised by
    // `triple/images.test.ts`; this only pins the two bounds together.
    expect(IMAGE_TIMEOUT_MS).toBe(DEFAULT_TIMEOUT_MS);
    expect(IMAGE_TIMEOUT_MS).toBe(10_000);
  });
});

// --------------------------------------------------------------------------
// Immediate poll on demand (2.2, 8.8)
// --------------------------------------------------------------------------

describe("pollNow (2.2, 8.8)", () => {
  it("performs an immediate unconditional Live_View refresh", async () => {
    const h = harness(boundState(), {
      executions: { status: 200, body: [execution("run-a")] },
    });

    await h.poller.pollNow();
    await h.poller.pollNow();

    expect(h.count(EXECUTIONS_URL)).toBe(2);
    // Both polls pushed the run's data, cached after the first fetch.
    expect(eventsOfType(h.events, "run-data-loaded")).toHaveLength(2);
    expect(h.count(executionResultsUrl("run-a"))).toBe(1);
    expect(h.getState().live.displayed?.execution.executionId).toBe("run-a");
  });
});
