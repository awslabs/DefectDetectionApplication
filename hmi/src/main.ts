/**
 * Entry point for the Quality Station HMI (task 9.5).
 *
 * Wires the pure reducer (`app/machine.ts`) to its effectful shells:
 *
 *  - Startup decision via `auth/session` (login form iff no stored token or
 *    the token is expired; resume otherwise — Requirements 1.1, 1.5).
 *  - `api/client` configured so a failed single re-login surfaces the login
 *    screen (Requirement 1.8) and stops the poller.
 *  - After entering the app: registrations fetched (Requirement 2.1),
 *    default workflow selected by latest most-recent-run `startedAt`
 *    (Requirements 2.4, 2.7), immediate first poll, then the 2-second cycle
 *    (Requirement 3.1).
 *  - Renderer subscription: every dispatched event reduces to a new state
 *    that is rendered immediately.
 *
 * The bundle is produced with `vite build` (base `/hmi/`) and served by the
 * LocalServer's guarded static mount (Requirement 6.7).
 */

import { configureApiClient, login } from "./api/client";
import type { Execution } from "./api/types";
import { startupScreen } from "./auth/session";
import { activeRegistrations, selectDefaultRegistration } from "./logic/selection";
import { initialState, reduce, type AppEvent, type AppState } from "./app/machine";
import { createPoller } from "./app/poller";
import { createRenderer } from "./ui/render";

// --------------------------------------------------------------------------
// Store: state + dispatch + render subscription
// --------------------------------------------------------------------------

let state: AppState = initialState(startupScreen()); // Requirements 1.1, 1.5

function dispatch(event: AppEvent): void {
  state = reduce(state, event);
  renderer.render(state);
}

const poller = createPoller({ getState: () => state, dispatch });

// A failed single re-login discarded the token (Requirement 1.8): show the
// login form and stop the cycle until the operator signs in again.
configureApiClient({
  onAuthExpired: () => {
    poller.stop();
    dispatch({ type: "auth-expired" });
  },
});

// --------------------------------------------------------------------------
// Operator intents
// --------------------------------------------------------------------------

const renderer = createRenderer(getAppRoot(), {
  onLoginSubmit(username, password) {
    void handleLogin(username, password);
  },

  onSelectRegistration(registrationId) {
    // Selection swap: fresh live slice + immediate poll so the new
    // workflow's view appears within the 2-second bound (Requirement 2.3).
    dispatch({ type: "registration-selected", registrationId });
    void poller.pollNow();
  },

  onHistorySelect(executionId) {
    const run = findRun(executionId);
    if (run === null) return;
    dispatch({ type: "history-run-selected", run }); // pins the view (7.3)
    void poller.loadHistoricalRun(run);
  },

  onReturnToLive() {
    dispatch({ type: "return-to-live" }); // 7.5
    // Reload the live run's data (cache-backed, so normally no network).
    void poller.pollNow();
  },
});

function getAppRoot(): HTMLElement {
  const root = document.getElementById("app");
  if (root !== null) return root;
  const fallback = document.createElement("div");
  fallback.id = "app";
  document.body.append(fallback);
  return fallback;
}

/** Resolves a history tile's executionId against the latest poll payload. */
function findRun(executionId: string): Execution | null {
  return (
    state.live.latestExecutions.find((e) => e.executionId === executionId) ??
    (state.live.displayedRun?.executionId === executionId
      ? state.live.displayedRun
      : null)
  );
}

// --------------------------------------------------------------------------
// Login and bootstrap
// --------------------------------------------------------------------------

async function handleLogin(username: string, password: string): Promise<void> {
  const result = await login({ username, password });
  if (result.ok) {
    dispatch({ type: "login-succeeded", atEpochMs: Date.now() });
    await bootstrap();
    return;
  }
  // 403 → disabled (1.6); 401 → rejected, nothing stored (1.7); transport
  // failures → unreachable, so the operator gets feedback either way.
  const reason =
    result.reason === "local-login-disabled"
      ? "disabled"
      : result.reason === "credentials-rejected"
        ? "rejected"
        : "unreachable";
  dispatch({ type: "login-failed", reason });
}

/**
 * Entering the app (fresh login or resumed session): fetch registrations
 * (2.1), select the default workflow by latest most-recent-run `startedAt`
 * (2.4, 2.7), run the immediate first poll, and start the 2-second cycle.
 * When the device is unreachable, the poller's disconnected retry probe
 * takes over (8.2).
 */
async function bootstrap(): Promise<void> {
  const registrations = await poller.refreshRegistrations();
  if (registrations !== null) {
    const actives = activeRegistrations(registrations);
    // Bounded burst: one bounded executions fetch per active registration,
    // only to establish the default selection (design Decision 3).
    const runLists = await Promise.all(
      actives.map(async (registration) => {
        const runs = await poller.fetchExecutions(registration.registrationId);
        return [registration.registrationId, runs ?? []] as const;
      }),
    );
    const defaultRegistration = selectDefaultRegistration(
      registrations,
      new Map(runLists.map(([id, runs]) => [id, runs])),
    );
    if (defaultRegistration !== null) {
      dispatch({
        type: "registration-selected",
        registrationId: defaultRegistration.registrationId,
      });
      await poller.pollNow();
    }
  }
  poller.start();
}

// --------------------------------------------------------------------------
// Startup
// --------------------------------------------------------------------------

renderer.render(state);
if (state.auth.screen === "app") {
  void bootstrap(); // resumed session (Requirement 1.5)
}
