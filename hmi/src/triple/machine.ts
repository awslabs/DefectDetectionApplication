/**
 * The Triple_HMI app state machine: a pure reducer
 * `(TripleAppState, TripleEvent) → TripleAppState`
 * (design "Components and Interfaces", `triple/machine.ts`).
 *
 * Everything the kiosk shows is a function of this state, and every state
 * change is a function of one event dispatched by an effectful shell
 * (`triple/poller.ts`, `api/client.ts`, `triple/render.ts`). The reducer never
 * fetches, never touches the DOM, and never reads a clock — timestamps arrive
 * on the events — so the whole module is directly property-testable.
 *
 * Four concerns:
 *
 *  - **Auth** (Requirement 1): login screen ↔ app transitions, the login
 *    error states (credentials rejected / local login disabled / LocalServer
 *    unreachable), and the auth-expired path taken when the shared client's
 *    single 401 re-login fails. A 401 always routes here and never to the
 *    disconnected state (Requirements 1.4, 8.1).
 *  - **Connection** (Requirement 8): CONNECTED ↔ DISCONNECTED. Disconnect
 *    exactly on network error / 10-second timeout / HTTP 5xx, retaining the
 *    last Run_Result and the last-successful-update time (8.1); any 2xx while
 *    disconnected reconnects in the same step (8.3, 8.4). Additive on top of
 *    the original HMI's machine: a consecutive-poll-failure counter that
 *    raises the stale-data indicator at ≥5 failed cycles and is reset by any
 *    success (Requirements 3.8, 3.9).
 *  - **Binding** (Requirements 2.2–2.4, 2.7, 8.5, 8.8): every registrations
 *    payload is re-run through `bindTargetWorkflow`, so the first bind, the
 *    not-deployed transition, and the automatic re-bind are all the same pure
 *    evaluation with no extra logic here.
 *  - **Live view** (Requirements 3, 7): the displayed run is the maximal
 *    terminal run of the latest poll payload, chosen by the reused
 *    `logic/runs.ts` ordering in a single reducer step (3.2, 3.3, 3.5, 3.7);
 *    the in-progress flag is derived from the same payload without disturbing
 *    the displayed content (3.4); historical mode pins the displayed run while
 *    the history strip keeps updating and a newer-run flag is raised (7.4),
 *    and return-to-live clears both and resumes live selection (7.5).
 *
 * Run content (Inspections + verdicts) is derived here from the payloads the
 * poller reports — `deriveInspections` over the `/results` inventory and
 * `deriveVerdicts` over the `/metadata` object — and applied only while the
 * run it belongs to is still the displayed run, so a late arrival can never
 * paint one run's images or verdicts onto another run.
 */

import type { ApiErrorKind } from "../api/client";
import type { Execution, Registration, ResultImage } from "../api/types";
import { hasInProgressRun, isTerminal, latestTerminalRun } from "../logic/runs";
import { bindTargetWorkflow } from "./binding";
import { DEFAULT_TRIPLE_WORKFLOW_NAME } from "./config";
import {
  buildHistory,
  insertHistoryEntry,
  toHistoryEntry,
  type HistoryEntry,
  type SlotsByExecution,
} from "./history";
import { deriveInspections } from "./inspections";
import {
  deriveVerdicts,
  type InspectionSlotVM,
  type InspectionSlotVMTriple,
  type VerdictDerivation,
  type VerdictMetadata,
} from "./verdicts";

// --------------------------------------------------------------------------
// Constants
// --------------------------------------------------------------------------

/**
 * Consecutive failed poll cycles that raise the stale-data indicator
 * (Requirement 3.9). Any successful LocalServer response resets the count, so
 * the indicator disappears within one update cycle of a success.
 */
export const STALE_POLL_FAILURE_THRESHOLD = 5;

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------

export type ConnectionState = "connected" | "disconnected";
export type LiveMode = "live" | "historical";
export type LoginError = "rejected" | "disabled" | "unreachable";

export interface AuthSlice {
  screen: "login" | "app";
  /** Set on the login screen after a failed attempt (1.6, 1.7, 1.9). */
  loginError?: LoginError;
}

export interface ConnectionSlice {
  state: ConnectionState;
  /** Epoch milliseconds of the last successful LocalServer response (8.1). */
  lastSuccessfulUpdate: number | null;
  /** Failed poll cycles since the last success; ≥5 → stale indicator (3.9). */
  consecutivePollFailures: number;
}

/**
 * Target_Workflow binding (Requirements 2.2–2.4, 2.7, 8.5, 8.8).
 * `pending` means no registrations payload has been evaluated yet.
 */
export type BindingSlice =
  | { state: "pending" }
  | { state: "bound"; registration: Registration }
  | { state: "not-deployed" };

/**
 * The displayed run's full view model (design `RunResultVM`), assembled from
 * the run itself plus its `/results` and `/metadata` payloads.
 */
export interface RunResultVM extends VerdictDerivation {
  execution: Execution;
  /** True iff `/results` failed after its single retry (4.9). */
  resultsUnavailable: boolean;
  /**
   * True between the run becoming the displayed run and its results/metadata
   * being applied. The renderer shows loading placeholders rather than the
   * previous run's content — never stale images or verdicts.
   */
  dataPending: boolean;
}

export interface LiveSlice {
  mode: LiveMode;
  /** null → the no-runs / no-terminal-runs placeholder state (2.6, 3.7). */
  displayed: RunResultVM | null;
  /** True iff a pending/running execution exists (3.4). */
  inProgress: boolean;
  /** History strip entries, newest first, capacity 10 (7.1, 7.2, 7.6, 7.8). */
  history: HistoryEntry[];
  /** Historical mode only: a newer terminal run has completed (7.4). */
  newerRunAvailable: boolean;
  /** The displayed run's data could not be fetched (7.7). */
  historicalDataError: boolean;
  /**
   * The most recent executions payload. Return-to-live recomputes the maximal
   * terminal run from it in a single step (7.5).
   */
  latestExecutions: Execution[];
}

export interface TripleAppState {
  auth: AuthSlice;
  connection: ConnectionSlice;
  binding: BindingSlice;
  live: LiveSlice;
  /** The resolved Target_Workflow name every payload is bound against (2.5). */
  targetName: string;
  /**
   * Per-execution slot verdicts learned from loaded run data, keyed by
   * `executionId` — the cache `history.ts` resolves history-tile verdicts
   * from. Pruned to the runs the history strip and the displayed run need.
   */
  runSlots: Readonly<Record<string, InspectionSlotVMTriple>>;
}

/** Empty live slice: no runs displayed, no history, live mode. */
function emptyLiveSlice(): LiveSlice {
  return {
    mode: "live",
    displayed: null,
    inProgress: false,
    history: [],
    newerRunAvailable: false,
    historicalDataError: false,
    latestExecutions: [],
  };
}

/**
 * Initial state. `screen` comes from the startup session decision (1.1, 1.5);
 * `targetName` from `resolveWorkflowName` (2.5). The connection starts
 * connected with no successful update yet and a zero failure count, and the
 * binding starts `pending` until the first registrations payload arrives.
 */
export function initialTripleState(
  screen: "login" | "app",
  targetName: string = DEFAULT_TRIPLE_WORKFLOW_NAME,
): TripleAppState {
  return {
    auth: { screen },
    connection: {
      state: "connected",
      lastSuccessfulUpdate: null,
      consecutivePollFailures: 0,
    },
    binding: { state: "pending" },
    live: emptyLiveSlice(),
    targetName,
    runSlots: {},
  };
}

/** True iff the stale-data indicator is shown (Requirement 3.9). */
export function isStaleData(state: TripleAppState): boolean {
  return state.connection.consecutivePollFailures >= STALE_POLL_FAILURE_THRESHOLD;
}

// --------------------------------------------------------------------------
// Events
// --------------------------------------------------------------------------

export type TripleEvent =
  // Auth (Requirement 1)
  | { type: "login-succeeded"; atEpochMs: number }
  | { type: "login-failed"; reason: LoginError }
  | { type: "auth-expired" } // the client's single re-login failed (1.4)
  // Request outcomes other than the executions poll (Requirement 8)
  | { type: "request-succeeded"; atEpochMs: number }
  | { type: "request-failed"; kind: ApiErrorKind }
  // Executions poll outcomes (Requirements 3, 7)
  | {
      type: "executions-polled";
      executions: readonly Execution[];
      /** Epoch milliseconds of the response; omitted leaves it unchanged. */
      atEpochMs?: number;
    }
  | { type: "poll-failed"; kind: ApiErrorKind }
  // Displayed-run data (Requirements 4, 5)
  | {
      type: "run-data-loaded";
      executionId: string;
      /** `/results` inventory; null → unavailable after one retry (4.9). */
      images: readonly ResultImage[] | null;
      /** `/metadata` object; null → unavailable after one retry (4.8). */
      metadata: VerdictMetadata;
    }
  | { type: "run-data-failed"; executionId: string } // both fetches failed (7.7)
  // Operator interactions (Requirement 7)
  | { type: "history-run-selected"; run: Execution }
  | { type: "return-to-live" }
  // Registrations (Requirements 2, 8.5, 8.8)
  | {
      type: "registrations-loaded";
      registrations: readonly Registration[];
      /** Overrides the state's target name when the config is re-resolved. */
      targetName?: string;
    };

// --------------------------------------------------------------------------
// Reducer
// --------------------------------------------------------------------------

/** Pure transition function; never mutates the input state. */
export function reduce(state: TripleAppState, event: TripleEvent): TripleAppState {
  switch (event.type) {
    // ---- Auth ------------------------------------------------------------
    case "login-succeeded":
      // Login success enters the app connected, with a clean failure count.
      return {
        ...state,
        auth: { screen: "app" },
        connection: {
          state: "connected",
          lastSuccessfulUpdate: event.atEpochMs,
          consecutivePollFailures: 0,
        },
      };

    case "login-failed":
      // 401 → credentials rejected (1.7); 403 → local login disabled (1.6);
      // timeout/network → LocalServer unreachable (1.9). Nothing is stored,
      // and the form stays displayed for re-entry.
      return { ...state, auth: { screen: "login", loginError: event.reason } };

    case "auth-expired":
      // The client already discarded the stored token (1.4). Not a
      // disconnect: 401 routes to the auth path, never to disconnected (8.1).
      return { ...state, auth: { screen: "login" } };

    // ---- Connection ------------------------------------------------------
    case "request-succeeded":
      // Any 2xx reconnects in this same step (8.3) and clears the staleness
      // accounting (3.9).
      return {
        ...state,
        connection: {
          state: "connected",
          lastSuccessfulUpdate: event.atEpochMs,
          consecutivePollFailures: 0,
        },
      };

    case "request-failed":
      // Disconnect exactly on network error / 10 s timeout / HTTP 5xx (8.1);
      // 401 belongs to the auth path and other HTTP errors are per-request
      // failures, not connection loss. Displayed content, history, and the
      // last-successful-update time are retained either way.
      return applyFailureKind(state, event.kind, state.connection.consecutivePollFailures);

    // ---- Executions poll -------------------------------------------------
    case "executions-polled":
      return applyExecutionsPolled(state, event.executions, event.atEpochMs);

    case "poll-failed":
      // A failed poll cycle retains the displayed content unchanged and
      // increments the consecutive-failure count (3.8, 3.9).
      return applyFailureKind(
        state,
        event.kind,
        state.connection.consecutivePollFailures + 1,
      );

    // ---- Displayed-run data ----------------------------------------------
    case "run-data-loaded":
      return applyRunData(state, event.executionId, event.images, event.metadata);

    case "run-data-failed":
      // Both fetches unusable: inspection-data placeholders in every slot
      // with the run's status still shown (4.9), and the historical-fetch
      // error indication while the history strip stays intact (7.7).
      return applyRunData(state, event.executionId, null, null);

    // ---- Historical mode -------------------------------------------------
    case "history-run-selected": {
      // Pin the selected run and show the historical indicator (7.3). Its
      // data arrives via run-data-loaded (cache hit or fetch).
      const current = state.live.displayed;
      const same = current !== null && current.execution.executionId === event.run.executionId;
      return {
        ...state,
        live: {
          ...state.live,
          mode: "historical",
          displayed: same ? current : freshRunResult(event.run),
          newerRunAvailable: false,
          historicalDataError: false,
        },
      };
    }

    case "return-to-live": {
      // Resume live selection in this same step: the displayed run is again
      // the maximal terminal run of the latest payload, and both the
      // historical indicator and the newer-run flag are cleared (7.5).
      const latest = latestTerminalRun(state.live.latestExecutions);
      return {
        ...state,
        live: {
          ...state.live,
          mode: "live",
          displayed: selectDisplayed(state.live.displayed, latest),
          newerRunAvailable: false,
          historicalDataError: false,
        },
      };
    }

    // ---- Registrations and binding ---------------------------------------
    case "registrations-loaded":
      return applyRegistrationsLoaded(
        state,
        event.registrations,
        event.targetName ?? state.targetName,
      );

    default:
      return state;
  }
}

// --------------------------------------------------------------------------
// Connection failures (Requirements 3.8, 3.9, 8.1)
// --------------------------------------------------------------------------

/**
 * Applies a failed request outcome: the connection state per Requirement 8.1
 * and the given consecutive-failure count per Requirement 3.9. Nothing else
 * in the state changes — the displayed Run_Result, the history strip, and the
 * last-successful-update time are all retained (3.8, 8.1).
 */
function applyFailureKind(
  state: TripleAppState,
  kind: ApiErrorKind,
  consecutivePollFailures: number,
): TripleAppState {
  const disconnects = kind === "network" || kind === "timeout" || kind === "http-5xx";
  return {
    ...state,
    connection: {
      state: disconnects ? "disconnected" : state.connection.state,
      lastSuccessfulUpdate: state.connection.lastSuccessfulUpdate,
      consecutivePollFailures,
    },
  };
}

// --------------------------------------------------------------------------
// Displayed-run selection (Requirements 3.2–3.5, 3.7)
// --------------------------------------------------------------------------

/** The three empty Inspection_Slots (the no-inspection-data placeholders). */
function placeholderSlots(): InspectionSlotVMTriple {
  const slot = (slotNumber: 1 | 2 | 3): InspectionSlotVM => ({ slotNumber });
  return [slot(1), slot(2), slot(3)];
}

/**
 * The view model of a run that has just become the displayed run, before its
 * `/results` and `/metadata` payloads arrive.
 *
 * A failed run needs no payload at all: its failure state and error summary
 * come from the execution itself, all three slots hold placeholders, and no
 * image reference from any prior run appears (5.9).
 */
function freshRunResult(execution: Execution): RunResultVM {
  if (execution.status === "failed") {
    const derivation = deriveVerdicts(execution, undefined, []);
    return {
      ...derivation,
      execution,
      // Nothing was requested for this run, so nothing is unavailable.
      metadataUnavailable: false,
      resultsUnavailable: false,
      dataPending: false,
    };
  }
  return {
    execution,
    slots: placeholderSlots(),
    moreInspections: false,
    metadataUnavailable: false,
    resultsUnavailable: false,
    dataPending: true,
  };
}

/**
 * Chooses the displayed run for live mode.
 *
 * The maximal terminal run of the payload becomes the displayed run; when it
 * is the run already on screen its loaded content is kept, and when the
 * payload holds no terminal run at all the current displayed run is retained
 * unchanged (null → the no-terminal-runs placeholder state, 3.7).
 */
function selectDisplayed(
  displayed: RunResultVM | null,
  latest: Execution | null,
): RunResultVM | null {
  if (latest === null) return displayed;
  if (displayed !== null && displayed.execution.executionId === latest.executionId) {
    return displayed;
  }
  return freshRunResult(latest);
}

// --------------------------------------------------------------------------
// History maintenance (Requirements 7.1, 7.2, 7.4, 7.6, 7.8)
// --------------------------------------------------------------------------

function slotsResolver(
  runSlots: Readonly<Record<string, InspectionSlotVMTriple>>,
): SlotsByExecution {
  return (executionId) => runSlots[executionId];
}

/**
 * Folds a poll payload into the history strip: the initial population from
 * the runs the LocalServer already knows about (7.8), then idempotent
 * insertion of each terminal run at its ordering position (7.2). An entry
 * already present with the same verdict and start time is left untouched, so
 * repeated polls of an unchanged payload leave the strip identical.
 */
function mergeHistory(
  history: HistoryEntry[],
  executions: readonly Execution[],
  runSlots: Readonly<Record<string, InspectionSlotVMTriple>>,
): HistoryEntry[] {
  const resolver = slotsResolver(runSlots);
  if (history.length === 0) return buildHistory(executions, resolver);

  let next = history;
  for (const execution of executions) {
    if (!isTerminal(execution)) continue;
    const entry = toHistoryEntry(execution, runSlots[execution.executionId] ?? []);
    const existing = next.find((held) => held.executionId === entry.executionId);
    if (
      existing !== undefined &&
      existing.verdict === entry.verdict &&
      existing.startedAt === entry.startedAt
    ) {
      continue;
    }
    next = insertHistoryEntry(next, entry);
  }
  return next;
}

/** True iff `history` holds a terminal run that `previous` did not (7.4). */
function sawNewerRun(previous: HistoryEntry[], history: HistoryEntry[]): boolean {
  if (previous.length === 0) return false;
  return history.some(
    (entry) => !previous.some((held) => held.executionId === entry.executionId),
  );
}

/**
 * Drops cached slots for runs nothing displays any more, keeping the cache
 * bounded by the history capacity plus the displayed run.
 */
function pruneRunSlots(
  runSlots: Readonly<Record<string, InspectionSlotVMTriple>>,
  history: readonly HistoryEntry[],
  displayed: RunResultVM | null,
): Readonly<Record<string, InspectionSlotVMTriple>> {
  const keep = new Set(history.map((entry) => entry.executionId));
  if (displayed !== null) keep.add(displayed.execution.executionId);

  const kept: Record<string, InspectionSlotVMTriple> = {};
  let dropped = false;
  for (const [executionId, slots] of Object.entries(runSlots)) {
    if (keep.has(executionId)) {
      kept[executionId] = slots;
    } else {
      dropped = true;
    }
  }
  return dropped ? kept : runSlots;
}

// --------------------------------------------------------------------------
// Poll payload handling (Requirements 3.2–3.5, 3.7, 7.2, 7.4)
// --------------------------------------------------------------------------

function applyExecutionsPolled(
  state: TripleAppState,
  executions: readonly Execution[],
  atEpochMs: number | undefined,
): TripleAppState {
  // A successful poll response (re)connects and clears the staleness
  // accounting (3.9, 8.3).
  const connection: ConnectionSlice = {
    state: "connected",
    lastSuccessfulUpdate: atEpochMs ?? state.connection.lastSuccessfulUpdate,
    consecutivePollFailures: 0,
  };

  const latestExecutions = [...executions];
  // The in-progress flag is derived from the payload alone and never touches
  // the displayed Run_Result (3.4).
  const inProgress = hasInProgressRun(latestExecutions);
  const history = mergeHistory(state.live.history, executions, state.runSlots);

  const historical = state.live.mode === "historical";
  // Historical mode pins the displayed run: the history strip and the
  // newer-run flag update, the displayed content does not (7.4).
  const displayed = historical
    ? state.live.displayed
    : selectDisplayed(state.live.displayed, latestTerminalRun(latestExecutions));
  const newerRunAvailable = historical
    ? state.live.newerRunAvailable || sawNewerRun(state.live.history, history)
    : false;

  return {
    ...state,
    connection,
    live: {
      ...state.live,
      displayed,
      inProgress,
      history,
      latestExecutions,
      newerRunAvailable,
    },
    runSlots: pruneRunSlots(state.runSlots, history, displayed),
  };
}

// --------------------------------------------------------------------------
// Displayed-run data handling (Requirements 4.8, 4.9, 5.x, 7.7)
// --------------------------------------------------------------------------

/**
 * Applies a run's `/results` inventory and `/metadata` object, deriving its
 * Inspections and verdicts.
 *
 * Only the displayed run's data is applied: a payload that arrives after the
 * view moved on is dropped, so one run's images or verdicts can never be
 * painted onto another run (4.11, 5.8). `images === null` marks the results
 * fetch unavailable after its single retry (4.9) — which is also the
 * historical-fetch error indication (7.7) — and `metadata === null` marks the
 * metadata fetch unavailable after its retry (4.8).
 */
function applyRunData(
  state: TripleAppState,
  executionId: string,
  images: readonly ResultImage[] | null,
  metadata: VerdictMetadata,
): TripleAppState {
  const current = state.live.displayed;
  if (current === null || current.execution.executionId !== executionId) return state;

  const execution = current.execution;
  const inspections = images === null ? [] : deriveInspections(images);
  const derivation = deriveVerdicts(execution, metadata, inspections);
  const displayed: RunResultVM = {
    ...derivation,
    execution,
    resultsUnavailable: images === null,
    dataPending: false,
  };

  // The run's now-known slot verdicts feed its history tile (7.1).
  const runSlots = { ...state.runSlots, [executionId]: derivation.slots };
  const history = state.live.history.map((entry) =>
    entry.executionId === executionId
      ? toHistoryEntry(execution, derivation.slots)
      : entry,
  );

  return {
    ...state,
    live: {
      ...state.live,
      displayed,
      history,
      historicalDataError: images === null,
    },
    runSlots,
  };
}

// --------------------------------------------------------------------------
// Registrations payload handling (Requirements 2.2–2.4, 2.7, 8.5, 8.8)
// --------------------------------------------------------------------------

/**
 * Re-runs `bindTargetWorkflow` over the payload (Requirements 2.2, 2.3).
 *
 * Because the binding is a pure function of the payload and the target name,
 * every transition falls out of this one evaluation: the first bind, the
 * not-deployed message when no active match exists (2.4), the return to
 * not-deployed when the bound registration goes inactive or absent (2.7,
 * 8.5), and the automatic re-bind when a match reappears (8.8).
 *
 * Live content belongs to a specific registration, so binding to a different
 * registration — or losing the binding — resets the live slice; re-binding the
 * same registration leaves the Live_View untouched.
 */
function applyRegistrationsLoaded(
  state: TripleAppState,
  registrations: readonly Registration[],
  targetName: string,
): TripleAppState {
  const binding = bindTargetWorkflow(registrations, targetName);

  if (binding.state === "not-deployed") {
    if (state.binding.state === "not-deployed") {
      return state.targetName === targetName ? state : { ...state, targetName };
    }
    return {
      ...state,
      targetName,
      binding,
      live: emptyLiveSlice(),
      runSlots: {},
    };
  }

  const boundId =
    state.binding.state === "bound" ? state.binding.registration.registrationId : null;
  if (boundId === binding.registration.registrationId) {
    return { ...state, targetName, binding };
  }
  return {
    ...state,
    targetName,
    binding,
    live: emptyLiveSlice(),
    runSlots: {},
  };
}
