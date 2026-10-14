/**
 * The app state machine: a pure reducer `(AppState, AppEvent) → AppState`
 * (design "Components and Interfaces", `app/machine.ts`).
 *
 * Covers three concerns, each driven by events the effectful shells
 * (`api/client.ts`, `app/poller.ts`, `ui/render.ts`) dispatch:
 *
 *  - **Auth** (Requirement 1): login screen ↔ app transitions, login error
 *    states (credentials rejected / local login disabled), and the
 *    auth-expired path taken when the client's single 401 re-login fails.
 *  - **Connection** (Requirement 8): CONNECTED ↔ DISCONNECTED per the design
 *    state diagram — disconnect exactly on network error / 10-second timeout
 *    / HTTP 5xx (401 routes to the auth path, never to disconnected), the
 *    last Run_Result and last-successful-update time retained across the
 *    disconnect, reconnect on any successful response (8.1, 8.3, 8.4).
 *  - **Live view** (Requirements 3, 7, 2.8, 8.5): the displayed run is the
 *    maximal terminal run of the latest poll payload in a single reducer
 *    step (3.1, 3.2, 3.4, 3.7); in-progress detection never disturbs the
 *    displayed Run_Result (3.3); historical mode pins the view while history
 *    updates and a newer-run flag is raised (7.3, 7.4, 7.5); registrations
 *    payloads drive unavailable / no-workflows messaging (8.5, 2.5).
 *
 * The reducer is pure and synchronous: it never fetches. Run data (verdict +
 * images) arrives via `run-data-loaded` events the poller dispatches after
 * its fetches complete, and is applied only when it still matches the
 * displayed run.
 */

import type { ApiErrorKind } from "../api/client";
import type { Execution, Registration } from "../api/types";
import { buildHistory, insertTerminalRun, type HistoryEntry, type VerdictResolver } from "../logic/history";
import type { ImagePairSelection } from "../logic/images";
import { hasInProgressRun, latestTerminalRun } from "../logic/runs";
import { checkDisplayedAvailability, isActiveRegistration } from "../logic/selection";
import type { VerdictState, VerdictViewModel } from "../logic/verdict";

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------

export type ConnectionState = "connected" | "disconnected";
export type LiveMode = "live" | "historical";
export type LoginError = "rejected" | "disabled" | "unreachable";

export interface AuthSlice {
  screen: "login" | "app";
  /** Set on the login screen after a failed login attempt (1.6, 1.7). */
  loginError: LoginError | null;
}

export interface ConnectionSlice {
  state: ConnectionState;
  /** Epoch milliseconds of the last successful LocalServer response (8.1). */
  lastSuccessfulUpdate: number | null;
}

export interface LiveSlice {
  mode: LiveMode;
  /** The run whose Run_Result is shown; null → no-runs message (2.8, 3.8). */
  displayedRun: Execution | null;
  /** Verdict for the displayed run; null while its data is loading. */
  verdict: VerdictViewModel | null;
  /** Image pair for the displayed run; null while its data is loading. */
  images: ImagePairSelection | null;
  /** True iff a pending/running execution exists (3.3). */
  inProgress: boolean;
  /** History strip entries, newest first, capacity 10 (7.1, 7.2). */
  history: HistoryEntry[];
  /** Historical mode only: a newer terminal run has completed (7.4). */
  newerRunAvailable: boolean;
  /** Historical mode only: the pinned run's data fetch failed (7.8). */
  historicalDataError: boolean;
  /**
   * The most recent executions poll payload; return-to-live recomputes the
   * maximal terminal run from it in a single step (7.5).
   */
  latestExecutions: Execution[];
}

export type AvailabilityState = "available" | "unavailable" | "no-workflows";

export interface AppState {
  auth: AuthSlice;
  connection: ConnectionSlice;
  /** The latest registrations payload (renderer derives actives/labels). */
  registrations: Registration[];
  selectedRegistrationId: string | null;
  /** Availability of the displayed registration (8.5, 2.5). */
  availability: AvailabilityState;
  live: LiveSlice;
}

/** Initial state; `screen` comes from the startup decision (1.1, 1.5). */
export function initialState(screen: "login" | "app"): AppState {
  return {
    auth: { screen, loginError: null },
    connection: { state: "connected", lastSuccessfulUpdate: null },
    registrations: [],
    selectedRegistrationId: null,
    availability: "available",
    live: emptyLiveSlice(),
  };
}

function emptyLiveSlice(): LiveSlice {
  return {
    mode: "live",
    displayedRun: null,
    verdict: null,
    images: null,
    inProgress: false,
    history: [],
    newerRunAvailable: false,
    historicalDataError: false,
    latestExecutions: [],
  };
}

// --------------------------------------------------------------------------
// Events
// --------------------------------------------------------------------------

/**
 * Verdict states for terminal runs, keyed by executionId — supplied by the
 * poller from its per-execution metadata cache so history entries carry a
 * verdict (7.1). Failed runs never need an entry (status alone → failed-run);
 * completed runs missing from the map default to no-verdict.
 */
export type VerdictMap = Readonly<Record<string, VerdictState>>;

export type AppEvent =
  // Auth (Requirement 1)
  | { type: "login-succeeded"; atEpochMs: number }
  | { type: "login-failed"; reason: LoginError }
  | { type: "auth-expired" } // client's single re-login failed (1.8)
  // Connection (Requirement 8)
  | { type: "request-succeeded"; atEpochMs: number }
  | { type: "request-failed"; kind: ApiErrorKind }
  // Poll payloads (Requirements 3, 7)
  | { type: "executions-polled"; executions: Execution[]; verdicts: VerdictMap }
  | {
      type: "run-data-loaded";
      executionId: string;
      verdict: VerdictViewModel;
      images: ImagePairSelection | null;
    }
  | { type: "run-data-failed"; executionId: string } // historical fetch (7.8)
  // Operator interactions (Requirement 7)
  | { type: "history-run-selected"; run: Execution }
  | { type: "return-to-live" }
  // Registrations (Requirements 2, 8.5)
  | { type: "registrations-loaded"; registrations: Registration[] }
  | { type: "registration-selected"; registrationId: string };

// --------------------------------------------------------------------------
// Reducer
// --------------------------------------------------------------------------

/** Pure transition function; never mutates the input state. */
export function reduce(state: AppState, event: AppEvent): AppState {
  switch (event.type) {
    // ---- Auth slice (task 8.1) ------------------------------------------
    case "login-succeeded":
      // Login success enters the app connected ([*] → CONNECTED).
      return {
        ...state,
        auth: { screen: "app", loginError: null },
        connection: { state: "connected", lastSuccessfulUpdate: event.atEpochMs },
      };

    case "login-failed":
      // 401 → credentials rejected, form retained (1.7); 403 → local login
      // disabled (1.6). Nothing else changes; nothing was stored.
      return { ...state, auth: { screen: "login", loginError: event.reason } };

    case "auth-expired":
      // The client already discarded the stored token (1.8); surface the
      // login form. Not a disconnect: 401 routes to auth, never to the
      // disconnected state (8.1).
      return { ...state, auth: { screen: "login", loginError: null } };

    // ---- Connection slice (task 8.1) ------------------------------------
    case "request-succeeded":
      // Any successful response (re)connects within the same reducer step
      // (8.3); the poller resumes the 2 s cycle.
      return {
        ...state,
        connection: { state: "connected", lastSuccessfulUpdate: event.atEpochMs },
      };

    case "request-failed":
      // Disconnect exactly on network error / 10 s timeout / HTTP 5xx (8.1).
      // 401 is handled by the auth path; other HTTP errors are per-request
      // failures, not connection loss. Everything else in the state — the
      // last Run_Result, history, and lastSuccessfulUpdate — is retained.
      if (event.kind === "network" || event.kind === "timeout" || event.kind === "http-5xx") {
        return {
          ...state,
          connection: { ...state.connection, state: "disconnected" },
        };
      }
      return state;

    // ---- Live view: poll payload (task 8.3) -----------------------------
    case "executions-polled":
      return applyExecutionsPolled(state, event.executions, event.verdicts);

    case "run-data-loaded": {
      // Apply fetched verdict/images only when they still belong to the
      // displayed run; stale loads (view changed meanwhile) are dropped.
      if (state.live.displayedRun?.executionId !== event.executionId) return state;
      return {
        ...state,
        live: {
          ...state.live,
          verdict: event.verdict,
          images: event.images,
          historicalDataError: false,
        },
      };
    }

    case "run-data-failed":
      // Historical-run data unavailable (7.8): error indication in the
      // Live_View while the history strip and return-to-live stay intact.
      if (state.live.displayedRun?.executionId !== event.executionId) return state;
      return { ...state, live: { ...state.live, historicalDataError: true } };

    // ---- Historical mode (task 8.6) --------------------------------------
    case "history-run-selected":
      // Pin the selected run with the historical indicator (7.3); its data
      // arrives via run-data-loaded (cache hit or fetch).
      return {
        ...state,
        live: {
          ...state.live,
          mode: "historical",
          displayedRun: event.run,
          verdict: null,
          images: null,
          newerRunAvailable: false,
          historicalDataError: false,
        },
      };

    case "return-to-live": {
      // Resume live mode: the displayed run is again the maximal terminal
      // run of the latest poll payload, in this same step (7.5).
      const latest = latestTerminalRun(state.live.latestExecutions);
      const unchanged =
        latest !== null &&
        latest.executionId === state.live.displayedRun?.executionId;
      return {
        ...state,
        live: {
          ...state.live,
          mode: "live",
          displayedRun: latest,
          // Keep loaded data only when the run on screen stays the same.
          verdict: unchanged ? state.live.verdict : null,
          images: unchanged ? state.live.images : null,
          newerRunAvailable: false,
          historicalDataError: false,
        },
      };
    }

    // ---- Registrations and availability (task 8.6) -----------------------
    case "registrations-loaded":
      return applyRegistrationsLoaded(state, event.registrations);

    case "registration-selected":
      // Selection swap: fresh live slice; the poller triggers an immediate
      // executions poll for the new registration (2.3).
      if (event.registrationId === state.selectedRegistrationId) return state;
      return {
        ...state,
        selectedRegistrationId: event.registrationId,
        availability: "available",
        live: emptyLiveSlice(),
      };

    default:
      return state;
  }
}

// --------------------------------------------------------------------------
// Poll payload handling (Requirements 3.1–3.4, 3.7, 3.8, 7.4)
// --------------------------------------------------------------------------

function verdictResolver(verdicts: VerdictMap): VerdictResolver {
  return (execution) =>
    execution.status === "failed"
      ? "failed-run"
      : verdicts[execution.executionId] ?? "no-verdict";
}

function applyExecutionsPolled(
  state: AppState,
  executions: Execution[],
  verdicts: VerdictMap,
): AppState {
  const verdictFor = verdictResolver(verdicts);

  // History: initial build from existing runs (7.6), then idempotent
  // insertion of newly terminal runs at the newest position (7.2).
  const previousHistory = state.live.history;
  let history = previousHistory;
  if (history.length === 0) {
    history = buildHistory(executions, verdictFor);
  } else {
    for (const execution of executions) {
      history = insertTerminalRun(history, execution, verdictFor);
    }
  }
  const historyGrewNewer =
    history !== previousHistory &&
    previousHistory.length > 0 &&
    history.some(
      (entry) => !previousHistory.some((p) => p.executionId === entry.executionId),
    );

  // In-progress indicator: on iff a pending/running execution exists; never
  // touches the displayed Run_Result (3.3).
  const inProgress = hasInProgressRun(executions);

  if (state.live.mode === "historical") {
    // Pinned view: history and the newer-run flag update, the displayed
    // Run_Result does not (7.4).
    return {
      ...state,
      live: {
        ...state.live,
        inProgress,
        history,
        latestExecutions: executions,
        newerRunAvailable: state.live.newerRunAvailable || historyGrewNewer,
      },
    };
  }

  // Live mode: the displayed run becomes the maximal terminal run in this
  // single reducer step (3.1, 3.2, 3.4, 3.7). Null → no-runs message state
  // with the cycle continuing (2.8, 3.8).
  const latest = latestTerminalRun(executions);
  const unchanged =
    latest !== null && latest.executionId === state.live.displayedRun?.executionId;
  return {
    ...state,
    live: {
      ...state.live,
      displayedRun: latest,
      // A new displayed run's verdict/images load via run-data-loaded; until
      // then the panels render a loading placeholder, never stale data.
      verdict: unchanged ? state.live.verdict : null,
      images: unchanged ? state.live.images : null,
      inProgress,
      history,
      latestExecutions: executions,
    },
  };
}

// --------------------------------------------------------------------------
// Registrations payload handling (Requirements 8.5, 2.5)
// --------------------------------------------------------------------------

function applyRegistrationsLoaded(
  state: AppState,
  registrations: Registration[],
): AppState {
  let availability: AvailabilityState = "available";
  if (state.selectedRegistrationId !== null) {
    availability = checkDisplayedAvailability(
      registrations,
      state.selectedRegistrationId,
    ).kind;
  } else {
    // Nothing selected yet: only the zero-actives case matters (2.5).
    availability = registrations.some(isActiveRegistration)
      ? "available"
      : "no-workflows";
  }
  return { ...state, registrations, availability };
}
