/**
 * The Triple_HMI polling loop — the effectful timer shell around the pure
 * reducer (design "Design Decision 4: Reuse the original polling and
 * resilience design unchanged").
 *
 * Responsibilities (task 11.1):
 *  - While connected and bound: one
 *    `GET /workflows/registrations/{id}/executions?limit=10` every 2 seconds,
 *    so a run reaching a terminal status is displayed within the 2-second
 *    Run_Detection_Latency bound (Requirements 3.1, 8.4).
 *  - Fetch `/results` + `/metadata` (each retried exactly once, Requirements
 *    4.8, 4.9) **only when the latest terminal run changes** (Requirement
 *    4.1) — an event-driven pair of requests per new run, not per cycle.
 *  - Every 15th connected cycle (~30 s), re-fetch
 *    `GET /workflows/registrations` so the reducer can re-run
 *    `bindTargetWorkflow` and notice a retired or replaced Target_Workflow
 *    (Requirements 2.7, 8.5).
 *  - While the Target_Workflow is not bound (never bound, or the
 *    not-deployed state), spend the cycle on a registrations re-check instead
 *    of an executions poll, and poll immediately once a payload binds, so the
 *    Live_View resumes within 2 seconds of that response without operator
 *    interaction (Requirements 2.4, 8.8).
 *  - While disconnected: one retry probe (`GET /workflows/registrations`)
 *    every 10 seconds, unlimited (Requirement 8.2). Any 2xx probe reconnects
 *    in the same step (the reducer's `request-succeeded`, Requirement 8.3)
 *    and is followed immediately by an executions poll with an
 *    **unconditional** Live_View and history refresh, whether or not anything
 *    changed while disconnected (Requirements 8.6, 8.7).
 *  - Per-execution LRU cache (capacity 20) of immutable terminal-run data, so
 *    re-displaying a run (a history tile selection, a reconnect refresh)
 *    costs no requests.
 *
 * The poller never mutates state itself: every observation is dispatched as a
 * `TripleEvent` into `triple/machine.ts`. `api/client.ts` is reused unchanged,
 * so the 10-second per-request timeout, the 5xx classification, and the single
 * re-login + single retry on 401 all come from there; this module only routes
 * the outcomes (Requirements 1.4, 8.1). Timers use the global `setTimeout`,
 * which the test suite drives with fake timers.
 */

import { apiFetch, type ApiErrorKind } from "../api/client";
import {
  executionMetadataUrl,
  executionResultsUrl,
  registrationExecutionsUrl,
  registrationsUrl,
} from "../api/routes";
import {
  parseExecutions,
  parseRegistrations,
  parseResultImages,
  type Execution,
  type Registration,
  type ResultImage,
} from "../api/types";
import { latestTerminalRun } from "../logic/runs";
import type { TripleAppState, TripleEvent } from "./machine";

// --------------------------------------------------------------------------
// Constants
// --------------------------------------------------------------------------

/** Connected poll cycle period (Requirement 3.1). */
export const POLL_INTERVAL_MS = 2_000;
/** Disconnected retry probe period (Requirement 8.2). */
export const RETRY_INTERVAL_MS = 10_000;
/** Registrations refresh every Nth connected cycle (~30 s, Requirement 8.5). */
export const REGISTRATIONS_REFRESH_EVERY = 15;
/** Executions poll payload bound (Requirement 3.1; ≥ history capacity). */
export const EXECUTIONS_LIMIT = 10;
/** Attempts per run-data request: the initial one plus one retry (4.8, 4.9). */
export const RUN_DATA_ATTEMPTS = 2;
/** Per-execution run-data cache capacity. */
export const RUN_CACHE_CAPACITY = 20;

// --------------------------------------------------------------------------
// Per-execution LRU cache
// --------------------------------------------------------------------------

/**
 * The raw `/metadata` object of one run.
 *
 * Deliberately **not** passed through `parseRunMetadata`: that parser keeps
 * only the flat run-level verdict fields, while the Triple_HMI also needs the
 * nested `bedrock.{nodeId}` records for the per-Inspection verdicts. The
 * payload therefore travels as an untrusted record and every field is
 * narrowed where it is read (`triple/verdicts.ts`), which keeps the shared
 * `api/types.ts` module unchanged.
 */
export type RawRunMetadata = Readonly<Record<string, unknown>>;

/** Cached immutable data of one terminal run; null = fetch failed. */
interface RunCacheEntry {
  /** Results `images` inventory, or null when unavailable after retry (4.9). */
  images: ResultImage[] | null;
  /** Raw metadata object, or null when unavailable after retry (4.8). */
  metadata: RawRunMetadata | null;
}

/**
 * Insertion-ordered Map as LRU: reads re-insert the key, writes evict the
 * oldest entry past capacity. Terminal-run data is immutable, so entries
 * never expire — they only age out.
 */
class RunCache {
  private readonly entries = new Map<string, RunCacheEntry>();

  get(executionId: string): RunCacheEntry | undefined {
    const entry = this.entries.get(executionId);
    if (entry !== undefined) {
      this.entries.delete(executionId);
      this.entries.set(executionId, entry);
    }
    return entry;
  }

  set(executionId: string, entry: RunCacheEntry): void {
    this.entries.delete(executionId);
    this.entries.set(executionId, entry);
    while (this.entries.size > RUN_CACHE_CAPACITY) {
      const oldest = this.entries.keys().next().value as string;
      this.entries.delete(oldest);
    }
  }
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

/**
 * Narrows a `/metadata` body to a record, preserving every key (including the
 * nested `bedrock` records). A body that is not a plain object is a
 * *successful* verdict-less payload, so it becomes `{}` rather than null — a
 * completed run then renders images + status with no verdict content and no
 * error state (Requirement 5.10).
 */
function asMetadataRecord(body: unknown): RawRunMetadata {
  return typeof body === "object" && body !== null && !Array.isArray(body)
    ? (body as RawRunMetadata)
    : {};
}

/** The bound registration's id, or null while not bound (2.4, 2.7). */
function boundRegistrationId(state: TripleAppState): string | null {
  return state.binding.state === "bound"
    ? state.binding.registration.registrationId
    : null;
}

// --------------------------------------------------------------------------
// Poller
// --------------------------------------------------------------------------

export interface TriplePollerDeps {
  getState: () => TripleAppState;
  dispatch: (event: TripleEvent) => void;
  /** Clock for `lastSuccessfulUpdate`; defaults to `Date.now`. */
  now?: () => number;
}

export interface TriplePoller {
  /** Starts the timer loop (idempotent). */
  start(): void;
  /** Stops the timer loop. */
  stop(): void;
  /** Immediate executions poll with an unconditional Live_View refresh. */
  pollNow(): Promise<void>;
  /**
   * Fetches + dispatches registrations, re-running the binding
   * (Requirements 2.1, 2.4, 2.7, 8.5, 8.8). Returns null when the request
   * failed.
   */
  refreshRegistrations(): Promise<Registration[] | null>;
  /**
   * Loads a run's data into the pinned historical view, forcing a refetch over
   * a cached failure so an operator retry can succeed (Requirements 7.3, 7.7).
   */
  loadHistoricalRun(run: Execution): Promise<void>;
}

export function createTriplePoller(deps: TriplePollerDeps): TriplePoller {
  const { getState, dispatch } = deps;
  const now = deps.now ?? Date.now;
  const cache = new RunCache();

  let timer: ReturnType<typeof setTimeout> | null = null;
  let running = false;
  let cycleCount = 0;
  /** The executionId whose data was last pushed into the Live_View. */
  let lastLiveRunId: string | null = null;
  /** Guards against overlapping cycles when a cycle outlives its period. */
  let cycleInFlight = false;

  // ---- Event helpers ------------------------------------------------------

  /** Any 2xx: connected within one step, staleness accounting reset (8.3). */
  function reportSuccess(): void {
    dispatch({ type: "request-succeeded", atEpochMs: now() });
  }

  /**
   * A failed non-poll request: network error / 10 s timeout / 5xx disconnects
   * (8.1), 401 routes to the auth path inside `api/client.ts` and leaves the
   * connection state alone, and the consecutive-poll-failure count is left
   * untouched — only whole poll cycles count toward staleness (3.9).
   */
  function reportFailure(kind: ApiErrorKind): void {
    dispatch({ type: "request-failed", kind });
  }

  // ---- Run data (Requirements 4.1, 4.8, 4.9) ------------------------------

  /**
   * One request repeated up to `RUN_DATA_ATTEMPTS` times — the initial attempt
   * plus exactly one retry (Requirements 4.8, 4.9) — reporting the connection
   * outcome of the final attempt.
   */
  async function fetchWithRetry<T>(
    url: string,
    parse: (body: unknown) => T,
  ): Promise<T | null> {
    let lastKind: ApiErrorKind = "network";
    for (let attempt = 0; attempt < RUN_DATA_ATTEMPTS; attempt++) {
      const result = await apiFetch(url);
      if (result.ok) {
        reportSuccess();
        return parse(result.data);
      }
      lastKind = result.kind;
    }
    reportFailure(lastKind);
    return null;
  }

  /**
   * The run's `/results` inventory and raw `/metadata`, from the cache when
   * already known.
   *
   * Terminal-run data is immutable, so a complete cache entry is always
   * reused. A cache entry holding a failed half is re-attempted only when
   * `retryFailures` is set — the unconditional reconnect/rebind refresh
   * (Requirements 8.6, 8.7) and the operator's historical selection
   * (Requirement 7.7) — so an ordinary cycle never re-hammers a route that
   * already failed twice.
   */
  async function ensureRunData(
    execution: Execution,
    retryFailures: boolean,
  ): Promise<RunCacheEntry> {
    const { executionId } = execution;
    const cached = cache.get(executionId);
    if (cached !== undefined) {
      const complete = cached.images !== null && cached.metadata !== null;
      if (complete || !retryFailures) return cached;
    }

    // The two requests are independent, so the missing halves are fetched
    // concurrently: one new run costs one round trip, not two
    // (Requirement 3.1's latency budget).
    const [images, metadata] = await Promise.all([
      cached?.images ??
        fetchWithRetry(executionResultsUrl(executionId), parseResultImages),
      cached?.metadata ??
        fetchWithRetry(executionMetadataUrl(executionId), asMetadataRecord),
    ]);

    const entry: RunCacheEntry = { images, metadata };
    cache.set(executionId, entry);
    return entry;
  }

  /**
   * Fetches (or reads cached) run data and pushes it into the view.
   *
   * A failed run needs no requests at all: its failure state, error summary,
   * and the placeholders in all three slots come from the execution itself
   * (Requirement 5.9), and the reducer has already applied them.
   *
   * When neither payload could be retrieved the run-data failure event carries
   * the inspection-data-unavailable placeholders and the historical-fetch
   * error indication (Requirements 4.9, 7.7).
   */
  async function loadRunData(
    run: Execution,
    retryFailures: boolean,
  ): Promise<void> {
    if (run.status === "failed") return;

    const { images, metadata } = await ensureRunData(run, retryFailures);
    if (images === null && metadata === null) {
      dispatch({ type: "run-data-failed", executionId: run.executionId });
      return;
    }
    dispatch({
      type: "run-data-loaded",
      executionId: run.executionId,
      images,
      metadata,
    });
  }

  // ---- Executions poll ----------------------------------------------------

  /**
   * One executions poll for the bound registration.
   *
   * With `force`, the Live_View data is (re)dispatched even when the latest
   * terminal run is unchanged — the unconditional reconnect refresh of
   * Requirements 8.6/8.7 and the resume-after-binding refresh of
   * Requirements 2.2/8.8. Otherwise `/results` + `/metadata` are fetched only
   * when the latest terminal run changed (Requirement 4.1).
   */
  async function pollExecutions(force: boolean): Promise<void> {
    const registrationId = boundRegistrationId(getState());
    if (registrationId === null) return;

    const result = await apiFetch(
      registrationExecutionsUrl(registrationId, EXECUTIONS_LIMIT),
    );
    if (!result.ok) {
      // A failed poll cycle retains the displayed content and counts toward
      // the stale-data indicator (Requirements 3.8, 3.9).
      dispatch({ type: "poll-failed", kind: result.kind });
      return;
    }

    const executions = parseExecutions(result.data);
    dispatch({ type: "executions-polled", executions, atEpochMs: now() });

    // Historical mode keeps its pinned run; the reducer has already updated
    // the history strip and the newer-run indicator (Requirement 7.4).
    if (getState().live.mode !== "live") return;

    const latest = latestTerminalRun(executions);
    if (latest === null) {
      lastLiveRunId = null;
      return;
    }
    if (force || latest.executionId !== lastLiveRunId) {
      lastLiveRunId = latest.executionId;
      // A forced refresh re-attempts anything a previous cycle failed to
      // fetch, so the reconnect Live_View refresh is truly unconditional
      // (Requirements 8.6, 8.7).
      await loadRunData(latest, force);
    }
  }

  // ---- Registrations (Requirements 2.1, 2.4, 2.7, 8.2, 8.5, 8.8) ---------

  async function refreshRegistrations(): Promise<Registration[] | null> {
    const result = await apiFetch(registrationsUrl());
    if (!result.ok) {
      reportFailure(result.kind);
      return null;
    }
    // Any 2xx probe reconnects in this step (8.3) before the payload is
    // re-run through `bindTargetWorkflow` (2.2–2.4, 2.7, 8.5, 8.8).
    reportSuccess();
    const registrations = parseRegistrations(result.data);
    dispatch({ type: "registrations-loaded", registrations });
    return registrations;
  }

  // ---- Timer loop ---------------------------------------------------------

  function scheduleNext(): void {
    if (!running) return;
    const delay =
      getState().connection.state === "connected"
        ? POLL_INTERVAL_MS
        : RETRY_INTERVAL_MS;
    timer = setTimeout(() => void tick(), delay);
  }

  async function tick(): Promise<void> {
    if (!running || cycleInFlight) {
      scheduleNext();
      return;
    }
    cycleInFlight = true;
    try {
      if (getState().connection.state !== "connected") {
        // Disconnected 10 s retry probe (8.2). A 2xx reconnects (8.3) and is
        // followed by an immediate poll with an unconditional Live_View and
        // history refresh, changed or not (8.6, 8.7).
        const registrations = await refreshRegistrations();
        if (registrations !== null) {
          lastLiveRunId = null;
          await pollExecutions(true);
        }
      } else if (boundRegistrationId(getState()) === null) {
        // Not deployed (or not yet bound): the cycle re-checks registrations
        // instead of polling executions, and resumes the Live_View in the
        // same cycle once a payload binds (2.4, 8.8).
        const registrations = await refreshRegistrations();
        if (registrations !== null && boundRegistrationId(getState()) !== null) {
          lastLiveRunId = null;
          await pollExecutions(true);
        }
      } else {
        // Connected steady state (8.4): the 2 s executions poll, plus the
        // periodic registrations refresh every 15th cycle (~30 s, 8.5).
        cycleCount++;
        if (cycleCount % REGISTRATIONS_REFRESH_EVERY === 0) {
          await refreshRegistrations();
        }
        await pollExecutions(false);
      }
    } finally {
      cycleInFlight = false;
      scheduleNext();
    }
  }

  // ---- Public surface -----------------------------------------------------

  return {
    start(): void {
      if (running) return;
      running = true;
      scheduleNext();
    },

    stop(): void {
      running = false;
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    },

    async pollNow(): Promise<void> {
      // Post-binding / post-login immediate poll: an unconditional refresh so
      // the bound workflow's Live_View appears within 2 seconds (2.2, 8.8).
      lastLiveRunId = null;
      await pollExecutions(true);
    },

    refreshRegistrations,

    async loadHistoricalRun(run: Execution): Promise<void> {
      // The reducer already pinned the run (`history-run-selected`); fetch or
      // read cached data, forcing a refetch over cached failures so an
      // operator retry can succeed (7.3, 7.7).
      await loadRunData(run, true);
    },
  };
}
