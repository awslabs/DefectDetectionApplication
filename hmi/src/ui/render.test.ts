/**
 * Single-inspection renderer (`/hmi/index.html`): what it shows of the API
 * target (`api/base.ts`) and which Session_Token its image URLs carry.
 *
 * Covered: the login form naming the LocalServer `host:port` the credentials
 * are posted to, the ignored-`?api=` notice, and image URLs carrying `token=`
 * only for the origin that issued the stored session.
 *
 * @vitest-environment jsdom
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resetApiBase, setApiBase } from "../api/base";
import type { Execution, Registration } from "../api/types";
import { initialState, reduce, type AppState } from "../app/machine";
import { saveSession } from "../auth/session";
import { selectImagePair } from "../logic/images";
import { deriveVerdict } from "../logic/verdict";
import { createRenderer } from "./render";

const STATION_A = "http://192.168.8.224:5000";
const STATION_B = "http://192.168.8.225:5000";

function registration(): Registration {
  return {
    registrationId: "reg-1",
    workflowId: "wf-1",
    name: "vlm-inspection",
    version: "1.0.0",
    status: "registered",
    registeredAt: 1_700_000_000,
  };
}

function execution(): Execution {
  return {
    executionId: "exec-1",
    registrationId: "reg-1",
    status: "completed",
    startedAt: 1_700_000_100,
    finishedAt: 1_700_000_110,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: "cap-1",
  };
}

/** A displayed completed run whose captured frame is the base output image. */
function displayedRunState(): AppState {
  const run = execution();
  let state = reduce(initialState("app"), {
    type: "registrations-loaded",
    registrations: [registration()],
  });
  state = reduce(state, { type: "registration-selected", registrationId: "reg-1" });
  state = reduce(state, { type: "executions-polled", executions: [run], verdicts: {} });
  return reduce(state, {
    type: "run-data-loaded",
    executionId: run.executionId,
    verdict: deriveVerdict(run, {}),
    images: selectImagePair([{ kind: "output", hasOverlay: false }]),
  });
}

function mount(): { root: HTMLElement; render: (state: AppState) => void } {
  document.body.replaceChildren();
  const root = document.createElement("div");
  document.body.append(root);
  const renderer = createRenderer(root, {
    onLoginSubmit: () => undefined,
    onSelectRegistration: () => undefined,
    onHistorySelect: () => undefined,
    onReturnToLive: () => undefined,
  });
  return { root, render: (state) => renderer.render(state) };
}

function one(scope: ParentNode, selector: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(selector);
  if (found === null) throw new Error(`no element matching ${selector}`);
  return found;
}

function visible(node: HTMLElement): boolean {
  return !node.classList.contains("hidden");
}

function capturedSrc(root: HTMLElement): URL {
  const src = one(root, 'img[alt="CAPTURED FRAME"]').getAttribute("src");
  if (src === null) throw new Error("the captured frame has no src");
  return new URL(src, location.href);
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  history.replaceState(null, "", "/");
  resetApiBase();
  localStorage.clear();
});

describe("login form target", () => {
  it("names the LocalServer host:port the credentials are posted to", () => {
    setApiBase(STATION_A);
    const { root, render } = mount();
    render(initialState("login"));

    const target = one(root, ".login-target");
    expect(visible(target)).toBe(true);
    expect(target.textContent).toBe("Signing in to 192.168.8.224:5000");
  });
});

describe("ignored ?api= notice", () => {
  it("names the rejected host and leaves the login form usable", () => {
    history.replaceState(null, "", "/hmi/index.html?api=https://other.example");
    resetApiBase();
    const { root, render } = mount();
    render(initialState("login"));

    const notice = one(root, ".api-notice");
    expect(visible(notice)).toBe(true);
    expect(notice.textContent).toContain('Ignored ?api= host "other.example"');
    expect(visible(one(root, ".login-screen"))).toBe(true);

    (one(notice, "button") as HTMLButtonElement).click();
    expect(visible(notice)).toBe(false);
  });
});

describe("image URLs and the session origin", () => {
  it("carry no token= when the stored session belongs to another origin", () => {
    saveSession({ token: "tok-A", expiresAt: 9e9, origin: STATION_A });
    setApiBase(STATION_B);
    const { root, render } = mount();
    render(displayedRunState());

    const src = capturedSrc(root);
    expect(src.origin).toBe(STATION_B);
    expect(src.pathname).toBe("/workflows/executions/exec-1/output-image");
    expect(src.searchParams.has("token")).toBe(false);
    expect(src.href).not.toContain("tok-A");
  });

  it("carry the token to the origin that issued it", () => {
    saveSession({ token: "tok-A", expiresAt: 9e9, origin: STATION_A });
    setApiBase(STATION_A);
    const { root, render } = mount();
    render(displayedRunState());

    expect(capturedSrc(root).searchParams.get("token")).toBe("tok-A");
  });
});
