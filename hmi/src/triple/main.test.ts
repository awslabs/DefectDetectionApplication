/**
 * Auth wiring unit tests for the Triple_HMI entry point (task 11.4).
 *
 * These drive `startTripleApp` end to end against a scripted LocalServer: the
 * real `api/client.ts` and `auth/session.ts` (both reused unchanged), the real
 * reducer, and the real renderer, with only `fetch` and the per-panel image
 * loader substituted. What is asserted is therefore the wiring itself — which
 * requests the entry makes, what it stores, and what the operator sees.
 *
 * Covered: a successful login storing the Session_Token and attaching it as a
 * bearer credential to the subsequent authenticated requests (1.2); the three
 * login failure messages — local login disabled (1.6), credentials rejected
 * (1.7), and LocalServer unreachable (1.9) — each leaving nothing stored and
 * the form displayed; the startup `GET /local-auth/status` reporting local
 * login disabled entering the app with no form at all (1.8); and a stored
 * unexpired Session_Token resuming without prompting for credentials (1.5).
 *
 * @vitest-environment jsdom
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClient } from "../api/client";
import {
  registrationExecutionsUrl,
  registrationsUrl,
  loginUrl,
} from "../api/routes";
import type { Registration } from "../api/types";
import {
  SESSION_STORAGE_KEY,
  clearRetainedCredentials,
  saveSession,
} from "../auth/session";
import {
  LOCAL_AUTH_STATUS_URL,
  startTripleApp,
  type TripleApp,
} from "./main";
import { TRIPLE_MESSAGES } from "./render";

const NAME = "blue-plate-detection-guided-inspection";
const NOW_MS = 1_700_000_000_000;

/**
 * An `expiresAt` (epoch seconds) an hour ahead of the *clock the startup
 * session decision reads* — `Date.now()`, not the injected event clock — so a
 * stored session counts as unexpired. Set per test in `beforeEach`.
 */
let expiresAt = 0;
/** An `expiresAt` already in the past for the same clock. */
let expiredAt = 0;

// --------------------------------------------------------------------------
// Scripted LocalServer
// --------------------------------------------------------------------------

interface RecordedRequest {
  url: string;
  method: string;
  authorization: string | null;
  body: string | null;
}

/** What the scripted server answers for the two auth routes. */
interface Script {
  /** `GET /local-auth/status` body, or a network failure. */
  status: { localLoginEnabled: boolean } | "network-error";
  /** `POST /local-auth/login` outcome. */
  login:
    | { ok: true; token: string }
    | { ok: false; httpStatus: number }
    | "network-error";
}

function jsonResponse(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function registration(): Registration {
  return {
    registrationId: "reg-1",
    workflowId: "wf-1",
    name: NAME,
    version: "1.0.0",
    status: "registered",
    registeredAt: 1_699_999_000,
  };
}

const requests: RecordedRequest[] = [];
let script: Script;

/** Routes every request of the startup sequence; unknown routes are 404s. */
function scriptedFetch(): typeof fetch {
  return async (input, init) => {
    const url = String(input);
    const headers = new Headers(init?.headers);
    requests.push({
      url,
      method: init?.method ?? "GET",
      authorization: headers.get("Authorization"),
      body: typeof init?.body === "string" ? init.body : null,
    });

    if (url === LOCAL_AUTH_STATUS_URL) {
      if (script.status === "network-error") throw new TypeError("fetch failed");
      return jsonResponse(200, script.status);
    }
    if (url === loginUrl()) {
      if (script.login === "network-error") throw new TypeError("fetch failed");
      return script.login.ok
        ? jsonResponse(200, { token: script.login.token, expiresAt })
        : jsonResponse(script.login.httpStatus, { detail: "no" });
    }
    if (url === registrationsUrl()) {
      return jsonResponse(200, [registration()]);
    }
    if (url === registrationExecutionsUrl("reg-1", 10)) {
      return jsonResponse(200, []);
    }
    return jsonResponse(404);
  };
}

function urls(): string[] {
  return requests.map((request) => request.url);
}

function requestsTo(url: string): RecordedRequest[] {
  return requests.filter((request) => request.url === url);
}

// --------------------------------------------------------------------------
// Harness
// --------------------------------------------------------------------------

let app: TripleApp | null = null;

/** Starts the app on a fresh root, with images and the clock neutralized. */
function start(): { app: TripleApp; root: HTMLElement } {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "app";
  document.body.append(root);
  const started = startTripleApp({
    root,
    search: "",
    buildTimeWorkflowName: NAME,
    now: () => NOW_MS,
    // The panels' own loading is task 10.2's concern and is covered by
    // render.test.ts; here it would only arm timers.
    loadImage: () => undefined,
  });
  app = started;
  return { app: started, root };
}

/** Lets every pending promise (and the 0 ms timer edge) settle. */
async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(0);
}

function one(scope: ParentNode, selector: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(selector);
  if (found === null) throw new Error(`no element matching ${selector}`);
  return found;
}

function visible(node: HTMLElement): boolean {
  return !node.classList.contains("hidden");
}

/** Fills and submits the rendered login form (Requirement 1.2). */
function submitLogin(root: HTMLElement, username: string, password: string): void {
  const form = one(root, "form.login-box") as HTMLFormElement;
  const user = one(form, 'input[name="username"]') as HTMLInputElement;
  const pass = one(form, 'input[name="password"]') as HTMLInputElement;
  user.value = username;
  pass.value = password;
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

function storedSession(): string | null {
  return localStorage.getItem(SESSION_STORAGE_KEY);
}

beforeEach(() => {
  vi.useFakeTimers();
  expiresAt = Math.floor(Date.now() / 1000) + 3_600;
  expiredAt = Math.floor(Date.now() / 1000) - 1;
  requests.length = 0;
  localStorage.clear();
  clearRetainedCredentials();
  script = { status: { localLoginEnabled: true }, login: { ok: true, token: "tok-1" } };
  // Storage stays the jsdom default, so the client and the startup session
  // decision read the same store.
  configureApiClient({ fetchFn: scriptedFetch() });
});

afterEach(() => {
  app?.stop();
  app = null;
  resetApiClient();
  clearRetainedCredentials();
  localStorage.clear();
  vi.useRealTimers();
});

describe("startTripleApp login wiring", () => {
  it("stores the Session_Token and attaches it as a bearer credential (1.2)", async () => {
    const { app: started, root } = start();
    await started.ready;
    await settle();

    // Local login is enabled and nothing is stored, so the form is shown.
    expect(started.getState().auth.screen).toBe("login");
    expect(visible(one(root, ".login-screen"))).toBe(true);
    expect(visible(one(root, ".triple-kiosk"))).toBe(false);
    expect(urls()).toEqual([LOCAL_AUTH_STATUS_URL]);

    submitLogin(root, "operator", "secret");
    await settle();

    // The token and its expiry are persisted together (1.2).
    expect(storedSession()).toBe(
      JSON.stringify({ token: "tok-1", expiresAt }),
    );
    expect(JSON.parse(requestsTo(loginUrl())[0]!.body!)).toEqual({
      username: "operator",
      password: "secret",
    });

    // The app is entered and every authenticated non-image request that
    // followed carries the bearer header (1.2).
    expect(started.getState().auth.screen).toBe("app");
    expect(visible(one(root, ".login-screen"))).toBe(false);
    expect(visible(one(root, ".triple-kiosk"))).toBe(true);

    const authenticated = requests.filter(
      (request) =>
        request.url !== loginUrl() && request.url !== LOCAL_AUTH_STATUS_URL,
    );
    expect(authenticated.length).toBeGreaterThan(0);
    expect(urls()).toContain(registrationsUrl());
    for (const request of authenticated) {
      expect(request.authorization).toBe("Bearer tok-1");
    }
    expect(one(root, ".login-error").textContent).toBe("");
  });

  it("reports that local login is disabled on HTTP 403 (1.6)", async () => {
    script.login = { ok: false, httpStatus: 403 };
    const { app: started, root } = start();
    await started.ready;
    await settle();

    submitLogin(root, "operator", "secret");
    await settle();

    expect(one(root, ".login-error").textContent).toBe(TRIPLE_MESSAGES.loginDisabled);
    // Nothing stored, form still displayed for re-entry.
    expect(storedSession()).toBeNull();
    expect(started.getState().auth.screen).toBe("login");
    expect(visible(one(root, ".login-screen"))).toBe(true);
    expect(urls()).not.toContain(registrationsUrl());
  });

  it("reports rejected credentials on HTTP 401 and keeps the form (1.7)", async () => {
    script.login = { ok: false, httpStatus: 401 };
    const { app: started, root } = start();
    await started.ready;
    await settle();

    submitLogin(root, "operator", "wrong");
    await settle();

    expect(one(root, ".login-error").textContent).toBe(TRIPLE_MESSAGES.loginRejected);
    expect(storedSession()).toBeNull();
    expect(started.getState().auth.screen).toBe("login");
    expect(visible(one(root, ".login-screen"))).toBe(true);
    // Exactly one login attempt: the login route is outside the 401 re-login
    // interception, so a rejection is not retried.
    expect(requestsTo(loginUrl())).toHaveLength(1);

    // A second attempt with good credentials still works from the same form.
    script.login = { ok: true, token: "tok-2" };
    submitLogin(root, "operator", "secret");
    await settle();
    expect(started.getState().auth.screen).toBe("app");
    expect(storedSession()).toContain("tok-2");
  });

  it("reports the LocalServer as unreachable on a network failure (1.9)", async () => {
    script.login = "network-error";
    const { app: started, root } = start();
    await started.ready;
    await settle();

    submitLogin(root, "operator", "secret");
    await settle();

    expect(one(root, ".login-error").textContent).toBe(
      TRIPLE_MESSAGES.loginUnreachable,
    );
    expect(storedSession()).toBeNull();
    expect(started.getState().auth.screen).toBe("login");
    expect(visible(one(root, ".login-screen"))).toBe(true);
  });
});

describe("startTripleApp startup decisions", () => {
  it("presents no login form when the device reports local login disabled (1.8)", async () => {
    script.status = { localLoginEnabled: false };
    const { app: started, root } = start();
    await started.ready;
    await settle();

    // The status probe decided it; no credentials were ever submitted.
    expect(urls()[0]).toBe(LOCAL_AUTH_STATUS_URL);
    expect(requestsTo(loginUrl())).toHaveLength(0);
    expect(storedSession()).toBeNull();

    expect(started.getState().auth.screen).toBe("app");
    expect(visible(one(root, ".login-screen"))).toBe(false);
    expect(visible(one(root, ".triple-kiosk"))).toBe(true);
    // The app was entered for real: registrations were fetched and bound
    // without any Session_Token (2.1).
    expect(urls()).toContain(registrationsUrl());
    expect(requestsTo(registrationsUrl())[0]!.authorization).toBeNull();
    expect(started.getState().binding.state).toBe("bound");
  });

  it("keeps the login form when the status probe cannot be reached", async () => {
    script.status = "network-error";
    const { app: started, root } = start();
    await started.ready;
    await settle();

    expect(started.getState().auth.screen).toBe("login");
    expect(visible(one(root, ".login-screen"))).toBe(true);
    expect(requestsTo(loginUrl())).toHaveLength(0);
    expect(urls()).not.toContain(registrationsUrl());
  });

  it("resumes from a stored unexpired Session_Token without prompting (1.5)", async () => {
    saveSession({ token: "stored-token", expiresAt });
    const { app: started, root } = start();
    await started.ready;
    await settle();

    // No form, and neither auth route was touched.
    expect(started.getState().auth.screen).toBe("app");
    expect(visible(one(root, ".login-screen"))).toBe(false);
    expect(visible(one(root, ".triple-kiosk"))).toBe(true);
    expect(urls()).not.toContain(LOCAL_AUTH_STATUS_URL);
    expect(requestsTo(loginUrl())).toHaveLength(0);

    // The stored token is what the resumed session authenticates with (1.2).
    expect(urls()[0]).toBe(registrationsUrl());
    for (const request of requests) {
      expect(request.authorization).toBe("Bearer stored-token");
    }
    expect(storedSession()).toContain("stored-token");
  });

  it("presents the login form for a stored expired Session_Token (1.1)", async () => {
    saveSession({ token: "expired", expiresAt: expiredAt });
    const { app: started, root } = start();
    await started.ready;
    await settle();

    expect(started.getState().auth.screen).toBe("login");
    expect(visible(one(root, ".login-screen"))).toBe(true);
    expect(urls()).toEqual([LOCAL_AUTH_STATUS_URL]);
  });
});
