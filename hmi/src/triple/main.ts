/**
 * Entry point for the IMTS Triple Inspection HMI (`/hmi/triple.html`), task 11.3.
 *
 * This module is the only effectful composition point of the kiosk: it wires
 * the pure modules (`triple/config.ts`, `triple/machine.ts`, and everything
 * they derive from) to their shells (`api/client.ts`, `triple/poller.ts`,
 * `triple/render.ts`, `triple/images.ts`) and holds nothing of its own beyond
 * the current state and the dispatch loop.
 *
 * Startup sequence:
 *
 *  1. Resolve the Target_Workflow name through `resolveWorkflowName`: the
 *     `workflow` query parameter of the kiosk URL, else the build-time
 *     `VITE_TRIPLE_WORKFLOW_NAME`, else the default (Requirement 2.5).
 *  2. Decide the first screen with the reused `auth/session.ts` startup
 *     decision — the login form iff no Session_Token is stored or the stored
 *     one has expired, otherwise resume without prompting (Requirements 1.1,
 *     1.5).
 *  3. When the form would be shown, first ask the unauthenticated
 *     `GET /local-auth/status`: a device reporting local login **disabled**
 *     enters the app with no form at all (Requirement 1.8). A device that
 *     reports it enabled — or that cannot be reached — keeps the form.
 *  4. On entering the app (fresh login or resumed session): fetch
 *     `GET /workflows/registrations` (Requirement 2.1), bind through
 *     `bindTargetWorkflow` inside the reducer, run one immediate poll so the
 *     bound workflow's Live_View appears within 2 seconds, then start the
 *     poller's cycles. A device without the workflow deployed renders the
 *     not-deployed message and the poller's re-check cycle re-binds it
 *     automatically (Requirements 2.4, 2.6, 8.8).
 *
 * Operator intents are the renderer's three callbacks: the login form
 * (Requirements 1.2, 1.6, 1.7, 1.9), a history tile (Requirement 7.3), and the
 * return-to-live control (Requirement 7.5). Each dispatches one reducer event
 * and, where data is needed, asks the poller for it.
 *
 * `auth/session.ts` and `api/client.ts` are reused **unchanged**: the bearer
 * header, the 10-second per-request timeout, the failure classification, and
 * the single 401 re-login all come from there, and the `token`-in-query image
 * URLs come from `api/routes.ts` via `triple/images.ts` (Requirement 1.3).
 *
 * The module auto-starts only for a document that declares itself the kiosk
 * entry (the `data-triple-kiosk` attribute `triple.html` sets on `<body>`), so
 * importing it — as the wiring tests of task 11.4 do — has no side effects and
 * `startTripleApp` can be driven with injected collaborators instead.
 */

import "./kiosk.css";

import { apiFetch, configureApiClient, login } from "../api/client";
import type { Execution } from "../api/types";
import { startupScreen } from "../auth/session";
import { resolveWorkflowName, type ConfigValue } from "./config";
import {
  initialTripleState,
  reduce,
  type TripleAppState,
  type TripleEvent,
} from "./machine";
import { createTriplePoller } from "./poller";
import { createTripleRenderer, type PanelImageLoader } from "./render";

// --------------------------------------------------------------------------
// Constants
// --------------------------------------------------------------------------

/**
 * `GET /local-auth/status` → `{localLoginEnabled}` — the unauthenticated route
 * that decides whether a login form is needed at all (Requirement 1.8).
 *
 * Declared here rather than in `api/routes.ts` because the shared modules are
 * reused unchanged by this spec.
 */
export const LOCAL_AUTH_STATUS_URL = "/local-auth/status";

/** Marks the kiosk page, so importing this module elsewhere starts nothing. */
export const KIOSK_ENTRY_ATTRIBUTE = "data-triple-kiosk";

/** The kiosk URL's query parameter that overrides the Target_Workflow name. */
export const WORKFLOW_QUERY_PARAM = "workflow";

// --------------------------------------------------------------------------
// Public surface
// --------------------------------------------------------------------------

export interface TripleAppOptions {
  /** Root element; defaults to `#app` (created when the page lacks it). */
  root?: HTMLElement;
  /** Kiosk URL query string; defaults to `location.search` (R2.5). */
  search?: string;
  /** Build-time name; defaults to `VITE_TRIPLE_WORKFLOW_NAME` (R2.5). */
  buildTimeWorkflowName?: ConfigValue;
  /** Clock for event timestamps; defaults to `Date.now`. */
  now?: () => number;
  /** Overrides the renderer's per-panel image loader (tests). */
  loadImage?: PanelImageLoader;
}

export interface TripleApp {
  /** The current state (the single source of everything rendered). */
  getState(): TripleAppState;
  /** Resolves once the startup sequence above has settled. */
  ready: Promise<void>;
  /** Stops the poller's timer loop. */
  stop(): void;
}

// --------------------------------------------------------------------------
// Environment helpers
// --------------------------------------------------------------------------

/** The app root, created when the host page lacks one. */
function appRoot(): HTMLElement {
  const existing = document.getElementById("app");
  if (existing !== null) return existing;
  const created = document.createElement("div");
  created.id = "app";
  document.body.append(created);
  return created;
}

/** The build-time `VITE_TRIPLE_WORKFLOW_NAME`, or null when undefined (R2.5). */
function buildTimeWorkflowName(): ConfigValue {
  const env = import.meta.env as unknown as Record<string, unknown> | undefined;
  const value = env?.["VITE_TRIPLE_WORKFLOW_NAME"];
  return typeof value === "string" ? value : null;
}

/**
 * True iff the `GET /local-auth/status` body says local login is disabled.
 *
 * Only an explicit `localLoginEnabled: false` counts: an unparseable body
 * leaves the login form in place rather than silently skipping it.
 */
function reportsLoginDisabled(body: unknown): boolean {
  if (typeof body !== "object" || body === null) return false;
  return (body as Record<string, unknown>)["localLoginEnabled"] === false;
}

// --------------------------------------------------------------------------
// Composition
// --------------------------------------------------------------------------

/**
 * Builds and starts the kiosk app: renderer + reducer + poller, plus the
 * startup sequence documented above. The returned `ready` promise settles when
 * startup has finished, which is what the wiring tests await.
 */
export function startTripleApp(options: TripleAppOptions = {}): TripleApp {
  const now = options.now ?? Date.now;
  const root = options.root ?? appRoot();
  const search = options.search ?? globalThis.location?.search ?? "";

  // Query parameter beats the build-time value beats the default (R2.5).
  const targetName = resolveWorkflowName(
    new URLSearchParams(search).get(WORKFLOW_QUERY_PARAM),
    options.buildTimeWorkflowName ?? buildTimeWorkflowName(),
  );

  // Login iff nothing is stored or the stored token expired (1.1); an
  // unexpired token resumes without prompting (1.5).
  let state: TripleAppState = initialTripleState(startupScreen(), targetName);

  function dispatch(event: TripleEvent): void {
    state = reduce(state, event);
    renderer.render(state);
  }

  const poller = createTriplePoller({ getState: () => state, dispatch, now });

  // The shared client's single 401 re-login already discarded the stored token
  // when it failed; surface the login form and stop the cycle (R1.4).
  configureApiClient({
    onAuthExpired: () => {
      poller.stop();
      dispatch({ type: "auth-expired" });
    },
  });

  /** Resolves a history tile's executionId against the latest poll payload. */
  function findRun(executionId: string): Execution | null {
    return (
      state.live.latestExecutions.find((run) => run.executionId === executionId) ??
      (state.live.displayed?.execution.executionId === executionId
        ? state.live.displayed.execution
        : null)
    );
  }

  const renderer = createTripleRenderer(
    root,
    {
      onLoginSubmit(username, password) {
        void handleLogin(username, password);
      },

      onHistorySelect(executionId) {
        const run = findRun(executionId);
        if (run === null) return;
        // Pin the run (7.3), then load its data — cached for a run already
        // seen, refetched when a previous attempt failed (7.7).
        dispatch({ type: "history-run-selected", run });
        void poller.loadHistoricalRun(run);
      },

      onReturnToLive() {
        // The reducer resumes live selection in this step (7.5); the immediate
        // poll refreshes it against the newest payload.
        dispatch({ type: "return-to-live" });
        void poller.pollNow();
      },
    },
    options.loadImage !== undefined ? { loadImage: options.loadImage } : {},
  );

  /**
   * Submits the form's credentials (R1.2). A failure maps to the message the
   * renderer shows: 403 → local login disabled (1.6), 401 → credentials
   * rejected (1.7), timeout/network → LocalServer unreachable (1.9). Nothing
   * is stored in those cases and the form stays displayed for re-entry.
   */
  async function handleLogin(username: string, password: string): Promise<void> {
    const result = await login({ username, password });
    if (result.ok) {
      dispatch({ type: "login-succeeded", atEpochMs: now() });
      await bootstrap();
      return;
    }
    const reason =
      result.reason === "local-login-disabled"
        ? "disabled"
        : result.reason === "credentials-rejected"
          ? "rejected"
          : "unreachable";
    dispatch({ type: "login-failed", reason });
  }

  /**
   * Entering the app: registrations (2.1) → binding inside the reducer
   * (2.2, 2.3) → one immediate poll so the Live_View, or its no-runs message
   * (2.6), is on screen within 2 seconds → the poller's cycles.
   *
   * Nothing special is needed for a device without the workflow: the reducer's
   * not-deployed state renders the message (2.4) and the poller's cycle
   * re-checks registrations and resumes the Live_View on its own (8.8).
   */
  async function bootstrap(): Promise<void> {
    const registrations = await poller.refreshRegistrations();
    if (registrations !== null && state.binding.state === "bound") {
      await poller.pollNow();
    }
    poller.start();
  }

  /** The startup sequence of the module docstring. */
  async function start(): Promise<void> {
    renderer.render(state);
    if (state.auth.screen === "app") {
      await bootstrap(); // resumed session (1.5)
      return;
    }
    // The form is only actually needed when the device has local login on
    // (1.8); an unreachable or unparseable status leaves it displayed.
    const status = await apiFetch(LOCAL_AUTH_STATUS_URL);
    if (status.ok && reportsLoginDisabled(status.data)) {
      // Same app-entry transition a successful login takes, without
      // credentials: no token is needed on a device with local login off.
      dispatch({ type: "login-succeeded", atEpochMs: now() });
      await bootstrap();
    }
  }

  return {
    getState: () => state,
    ready: start(),
    stop: () => poller.stop(),
  };
}

// --------------------------------------------------------------------------
// Kiosk auto-start
// --------------------------------------------------------------------------

/** True iff this document is the kiosk page rather than a test importer. */
function isKioskEntryDocument(): boolean {
  return (
    typeof document !== "undefined" &&
    document.body !== null &&
    document.body.hasAttribute(KIOSK_ENTRY_ATTRIBUTE)
  );
}

if (isKioskEntryDocument()) {
  startTripleApp();
}
