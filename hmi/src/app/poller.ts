/**
 * The polling loop — the effectful timer shell around the pure reducer
 * (design "Design Decision 3" and "Design Decision 5").
 *
 * Responsibilities (task 9.1):
 *  - While connected: one `GET .../executions?limit=10` for the displayed
 *    registration every 2 seconds (Requirements 3.1, 3.6).
 *  - Fetch `/results` + `/metadata` (metadata retried once, Requirement 4.9)
 *    only when the latest terminal run changes (Requirement 4.1) — an
 *    event-driven pair of requests per new run, not per cycle.
 *  - Per-execution LRU cache (capacity 20) of immutable terminal-run data,
 *    backing history verdicts and historical-run viewing without re-fetching.
 *  - Every 15th cycle (~30 s): refresh `GET /workflows/registrations` to
 *    notice retired/new workflows (Requirement 8.5).
 *  - While disconnected: a single retry probe (`GET /workflows/registrations`)
 *    every 10 seconds (Requirement 8.2).
 *  - On reconnect: immediate executions poll with an unconditional Live_View
 *    and history refresh, even when nothing changed (Requirements 8.6, 8.7).
 *  - Registrations fetched after login (Requirement 2.1); a selection swap
 *    triggers an immediate poll so the new workflow's view appears within
 *    the 2-second bound (Requirement 2.3).
 *
 * The poller never mutates state itself: every observation is dispatched as
 * an `AppEvent` into the reducer. Timers use the global `setTimeout`, which
 * the test suite drives with fake timers.
 */

import { apiFetch } from "../api/client";
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
  parseRunMetadata,
  type Execution,
  type Registration,
  type ResultImage,
  type RunMetadata,
} from "../api/types";
import { latestTerminalRun } from "../logic/runs";
import { selectImagePair } from "../logic/images";
import { deriveVerdict, type VerdictState } from "../logic/verdict";
import type { AppEvent, AppState, VerdictMap } from "./machine";

// --------------------------------------------------------------------------
// Constants
// --------------------------------------------------------------------------

/** Connected poll cycle period (Requirement 3.1). */
export const POLL_INTERVAL_MS = 2_000;
/** Disconnected retry probe period (Requirement 8.2). */
export const RETRY_INTERVAL_MS = 10_000;
/** Registrations refresh every Nth connected cycle (~30 s). */
export const REGISTRATIONS_REFRESH_EVERY = 15;
/** Executions poll payload bound (Requirement 3.6). */
export const EXECUTIONS_LIMIT = 10;
/** Per-execution data cache capacity. */
export const RUN_CACHE_CAPACITY = 20;

// --------------------------------------------------------------------------
// Per-execution LRU cache
// --------------------------------------------------------------------------

/** Cached immutable data of one terminal run; null fields = fetch failed. */
interface RunCacheEntry {
  /** Parsed metadata, or null when the fetch failed after its retry (4.9). */
  metadata: RunMetadata | null;
  /** Results `images` inventory, or null when the fetch failed. */
  images: ResultImage[] | null;
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
      // Refresh recency.
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
// Poller
// --------------------------------------------------------------------------

export interface PollerDeps {
  getState: () => AppState;
  dispatch: (event: AppEvent) => void;
  /** Clock for `lastSuccessfulUpdate`; defaults to `Date.now`. */
  now?: () => number;
}

export interface Poller {
  /** Starts the timer loop (idempotent). */
  start(): void;
  /** Stops the timer loop and ignores in-flight completions. */
  stop(): void;
  /** Immediate executions poll with an unconditional Live_View refresh. */
  pollNow(): Promise<void>;
  /** Fetches + dispatches registrations (after login, Requirement 2.1). */
  refreshRegistrations(): Promise<Registration[] | null>;
  /** One-off executions fetch (default-selection wiring, Requirement 2.4). */
  fetchExecutions(registrationId: string): Promise<Execution[] | null>;
  /** Loads a historical run's data into the view (Requirements 7.3, 7.8). */
  loadHistoricalRun(run: Execution): Promise<void>;
}

export function createPoller(deps: PollerDeps): Poller {
  const { getState, dispatch } = deps;
  const now = deps.now ?? Date.now;
  const cache = new RunCache();

  let timer: ReturnType<typeof setTimeout> | null = null;
  let running = false;
  let cycleCount = 0;
  /** The executionId whose data was last pushed into the Live_View. */
  let lastLiveRunId: string | null = null;
  /** Guards against overlapping cycles when a poll outlives its period. */
  let cycleInFlight = false;

  // ---- Event helpers ------------------------------------------------------

  function reportSuccess(): void {
    dispatch({ type: "request-succeeded", atEpochMs: now() });
  }

  function reportFailure(kind: Parameters<typeof failureEvent>[0]): void {
    dispatch(failureEvent(kind));
  }

  function failureEvent(
    kind: "network" | "timeout" | "http-5xx" | "http-401" | "http-other",
  ): AppEvent {
    return { type: "request-failed", kind };
  }

  // ---- Data fetching ------------------------------------------------------

  /**
   * Metadata for one completed run, cached; fetched with a single retry
   * (Requirement 4.9). `refetchNull` forces a new attempt over a cached
   * failure (used for operator-initiated historical views).
   */
  async function ensureMetadata(
    execution: Execution,
    refetchNull = false,
  ): Promise<RunMetadata | null> {
    const cached = cache.get(execution.executionId);
    if (cached !== undefined && (cached.metadata !== null || !refetchNull)) {
      return cached.metadata;
    }
    let metadata: RunMetadata | null = null;
    for (let attempt = 0; attempt < 2 && metadata === null; attempt++) {
      const result = await apiFetch(executionMetadataUrl(execution.executionId));
      if (result.ok) metadata = parseRunMetadata(result.data);
    }
    cache.set(execution.executionId, {
      metadata,
      images: cached?.images ?? null,
    });
    return metadata;
  }

  /** Results inventory for one run, cached. */
  async function ensureResults(
    execution: Execution,
    refetchNull = false,
  ): Promise<ResultImage[] | null> {
    const cached = cache.get(execution.executionId);
    if (cached !== undefined && (cached.images !== null || !refetchNull)) {
      return cached.images;
    }
    const result = await apiFetch(executionResultsUrl(execution.executionId));
    const images = result.ok ? parseResultImages(result.data) : null;
    cache.set(execution.executionId, {
      metadata: cached?.metadata ?? null,
      images,
    });
    return images;
  }

  /**
   * Verdict states for the payload's completed runs, from cached metadata.
   * Missing metadata is fetched once per execution — a bounded burst on the
   * first poll of a registration (≤ EXECUTIONS_LIMIT requests), zero
   * afterwards. Failed runs need no metadata (verdict from status alone).
   */
  async function buildVerdictMap(executions: Execution[]): Promise<VerdictMap> {
    const verdicts: Record<string, VerdictState> = {};
    for (const execution of executions) {
      if (execution.status !== "completed") continue;
      const metadata = await ensureMetadata(execution);
      verdicts[execution.executionId] = deriveVerdict(execution, metadata).state;
    }
    return verdicts;
  }

  /** Fetches (or reads cached) run data and pushes it into the Live_View. */
  async function loadRunData(run: Execution, historical: boolean): Promise<void> {
    const metadata =
      run.status === "completed" ? await ensureMetadata(run, historical) : null;
    const images = await ensureResults(run, historical);
    if (
      historical &&
      images === null &&
      run.status === "completed" &&
      metadata === null
    ) {
      // Nothing about the historical run could be retrieved (7.8).
      dispatch({ type: "run-data-failed", executionId: run.executionId });
      return;
    }
    dispatch({
      type: "run-data-loaded",
      executionId: run.executionId,
      verdict: deriveVerdict(run, metadata),
      images: images === null ? null : selectImagePair(images),
    });
  }

  // ---- Executions poll ----------------------------------------------------

  /**
   * One executions poll for the displayed registration. With `force`, the
   * Live_View data is (re)dispatched even when the latest terminal run is
   * unchanged — the reconnect refresh of Requirements 8.6/8.7 and the
   * selection-swap refresh of Requirement 2.3.
   */
  async function pollExecutions(force: boolean): Promise<void> {
    const registrationId = getState().selectedRegistrationId;
    if (registrationId === null) return;

    const result = await apiFetch(
      registrationExecutionsUrl(registrationId, EXECUTIONS_LIMIT),
    );
    if (!result.ok) {
      reportFailure(result.kind);
      return;
    }
    reportSuccess();
    const executions = parseExecutions(result.data);

    // History verdicts first, so the executions-polled event carries a
    // complete verdict map and history tiles are correct from the start.
    const verdicts = await buildVerdictMap(executions);
    dispatch({ type: "executions-polled", executions, verdicts });

    // Live_View data: only when the maximal terminal run changed (4.1),
    // or unconditionally on a forced refresh (8.6, 8.7). Historical mode
    // keeps its pinned run (7.4); the reducer already updated history.
    if (getState().live.mode !== "live") return;
    const latest = latestTerminalRun(executions);
    if (latest === null) {
      lastLiveRunId = null;
      return;
    }
    if (force || latest.executionId !== lastLiveRunId) {
      lastLiveRunId = latest.executionId;
      await loadRunData(latest, false);
    }
  }

  // ---- Registrations ------------------------------------------------------

  async function refreshRegistrations(): Promise<Registration[] | null> {
    const result = await apiFetch(registrationsUrl());
    if (!result.ok) {
      reportFailure(result.kind);
      return null;
    }
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
      if (getState().connection.state === "connected") {
        // Connected 2 s cycle (8.4): executions poll, plus the periodic
        // registrations refresh every 15th cycle (~30 s).
        cycleCount++;
        if (cycleCount % REGISTRATIONS_REFRESH_EVERY === 0) {
          await refreshRegistrations();
        }
        await pollExecutions(false);
      } else {
        // Disconnected 10 s retry probe (8.2): one registrations request;
        // success reconnects (8.3) and triggers the immediate poll with an
        // unconditional Live_View + history refresh (8.6, 8.7).
        const registrations = await refreshRegistrations();
        if (registrations !== null) {
          await pollExecutions(true);
        }
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
      // Selection swap / post-login immediate poll (2.3): unconditional
      // refresh so the new registration's view appears within one cycle.
      lastLiveRunId = null;
      await pollExecutions(true);
    },

    refreshRegistrations,

    async fetchExecutions(registrationId: string): Promise<Execution[] | null> {
      const result = await apiFetch(
        registrationExecutionsUrl(registrationId, EXECUTIONS_LIMIT),
      );
      if (!result.ok) {
        reportFailure(result.kind);
        return null;
      }
      reportSuccess();
      return parseExecutions(result.data);
    },

    async loadHistoricalRun(run: Execution): Promise<void> {
      // The reducer already pinned the run (history-run-selected); fetch or
      // read cached data, forcing a refetch over cached failures so an
      // operator retry can succeed (7.8).
      await loadRunData(run, true);
    },
  };
}
