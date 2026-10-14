/*
 * Bug condition exploration test (edge-workflow-run-ux, task 2).
 *
 * **Feature: edge-workflow-run-ux, Property 1: Deployed workflow registrations
 * are visible and runnable from the UI**
 * **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5**
 *
 * These four tests encode the EXPECTED behavior of the deployed-workflows UI
 * surface. On UNFIXED code they are EXPECTED TO FAIL — each failure is a
 * counterexample confirming the bug (isBugCondition from design.md):
 * registrations exist on the device, but the LocalServer UI has no route,
 * no nav entry, no API client, and no page that displays them.
 *
 * Bug condition from design:
 *   isBugCondition(input) := input.registrations IS NOT EMPTY
 *     AND NOT EXISTS page IN input.ui THAT displays them
 *     (identity, status, invalidReason, trigger control, executions)
 *
 * The bug is deterministic (the entire UI surface is missing), so the
 * property is scoped to the four concrete cases from the design's
 * "Exploratory Bug Condition Checking" section rather than broad generation.
 *
 * NOTE: `api/WorkflowRegistrationAPI` does not exist yet on unfixed code.
 * It is mocked with `{ virtual: true }` so its absence surfaces as TEST
 * FAILURES (via jest.requireActual inside the test) instead of a
 * suite-level module-resolution crash that would hide the other cases.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axios from "axios";
import { Connection } from "config/Interface";
import SideNav from "components/layout/SideNav";

// Invalid registration used by test 4 (example straight from design.md).
const INVALID_REGISTRATION = {
  registrationId: "reg-invalid-1",
  workflowId: "wf-1",
  version: "3",
  arch: "arm64",
  artifactPath: "/greengrass/artifacts/wf-1/3",
  status: "invalid",
  registeredAt: 1700000000,
  invalidReason: "Missing required artifact file: manifest.json",
};

// Virtual mock: lets this file load even while the module is missing on
// unfixed code. Once the fix lands, this becomes a normal module mock at the
// API boundary (per design: jest.mock("api/WorkflowRegistrationAPI")).
jest.mock(
  "api/WorkflowRegistrationAPI",
  () => ({
    listWorkflowRegistrations: jest
      .fn()
      .mockResolvedValue([INVALID_REGISTRATION]),
    getWorkflowRegistration: jest
      .fn()
      .mockResolvedValue({ ...INVALID_REGISTRATION, executions: [] }),
    triggerWorkflowRegistration: jest.fn(),
    getWorkflowExecution: jest.fn(),
  }),
  { virtual: true }
);

// Keep the app shell renderable in jsdom: auth disabled, no real network.
jest.mock("api/AuthAPI", () => ({
  ...jest.requireActual("api/AuthAPI"),
  fetchAuthConfig: jest
    .fn()
    .mockResolvedValue({ auth_enabled: false, auth_settings: {} }),
}));

// LoginGate (portal-user-manager local-auth feature) fetches GET
// /local-auth/status at app mount; mock it at the module boundary so the
// gate resolves "local login disabled" and the app renders as before.
jest.mock("api/LocalAuthAPI", () => ({
  ...jest.requireActual("api/LocalAuthAPI"),
  fetchLocalAuthStatus: jest
    .fn()
    .mockResolvedValue({ localLoginEnabled: false }),
}));

/**
 * Render the real application (router built in App.tsx) at
 * /deployed-workflows. App.tsx creates its browser router at module scope
 * from window.location, so the URL is set BEFORE the module is first
 * required (lazy require; a single App/router instance is reused because
 * re-requiring the module graph would duplicate React and break hooks).
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let App: any;
function renderAppAtDeployedWorkflows(): void {
  if (!App) {
    window.history.pushState({}, "exploration", "/deployed-workflows");
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    App = require("App").default;
  }
  render(<App />);
}

// Test-harness note (task 4.6): CRA's jest config sets `resetMocks: true`,
// which wipes the factory-time mock implementations above before EACH test.
// On unfixed code that never mattered (the suite failed before the mocks were
// consumed twice); on fixed code, later tests would otherwise see mocks that
// resolve `undefined`. Re-install the same implementations here — the exact
// pattern legacySurfacePreservation.test.tsx already uses. Assertions are
// unchanged; this is a harness-only adjustment.
beforeEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const registrationAPI = require("api/WorkflowRegistrationAPI");
  (registrationAPI.listWorkflowRegistrations as jest.Mock).mockResolvedValue([
    INVALID_REGISTRATION,
  ]);
  (registrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue({
    ...INVALID_REGISTRATION,
    executions: [],
  });
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const authAPI = require("api/AuthAPI");
  (authAPI.fetchAuthConfig as jest.Mock).mockResolvedValue({
    auth_enabled: false,
    auth_settings: {},
  });
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const localAuthAPI = require("api/LocalAuthAPI");
  (localAuthAPI.fetchLocalAuthStatus as jest.Mock).mockResolvedValue({
    localLoginEnabled: false,
  });
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe("deployed-workflows UI surface (bug condition exploration)", () => {
  /**
   * Test case 1 — Route Existence Test (design Exploratory #1)
   * Expected behavior: navigating to /deployed-workflows renders a
   * registrations page (2.1, 2.5) instead of falling through to the root
   * error element.
   * Expected counterexample on unfixed code: no route matches
   * /deployed-workflows, the router 404s into the empty root errorElement,
   * and nothing named "Deployed workflows" is rendered.
   */
  it("renders a registrations page at /deployed-workflows instead of falling through", async () => {
    renderAppAtDeployedWorkflows();

    const matches = await screen.findAllByText(
      /deployed workflows/i,
      {},
      { timeout: 4000 }
    );
    expect(matches.length).toBeGreaterThan(0);
  }, 15000);

  /**
   * Test case 2 — Navigation Entry Test (design Exploratory #2)
   * Expected behavior: SideNav offers a discoverable "Deployed workflows"
   * link pointing at /deployed-workflows (2.1).
   * Expected counterexample on unfixed code: SideNav's items contain no such
   * link (only Image sources / Workflows / Deployed models / Capture /
   * Infer / Application health entries exist).
   */
  it('has a "Deployed workflows" link in the side navigation', () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <SideNav />
      </MemoryRouter>
    );

    const link = screen.getByRole("link", { name: /deployed workflows/i });
    expect(link).toHaveAttribute("href", "/deployed-workflows");
  });

  /**
   * Test case 3 — API Client Test (design Exploratory #3)
   * Expected behavior: an api/WorkflowRegistrationAPI module exports
   * listWorkflowRegistrations / getWorkflowRegistration /
   * triggerWorkflowRegistration / getWorkflowExecution targeting
   * ${Connection.ENDPOINT}/workflows/registrations and
   * ${Connection.ENDPOINT}/workflows/executions (2.1, 2.2, 2.3).
   * Expected counterexample on unfixed code: the module does not exist —
   * jest.requireActual throws "Cannot find module".
   * (jest.requireActual bypasses the virtual mock above and loads the real
   * file, so a missing file registers as a test failure, not a suite crash.)
   */
  it("exposes api/WorkflowRegistrationAPI targeting the registrations/executions endpoints", async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let api: any;
    expect(() => {
      api = jest.requireActual("api/WorkflowRegistrationAPI");
    }).not.toThrow();

    expect(typeof api.listWorkflowRegistrations).toBe("function");
    expect(typeof api.getWorkflowRegistration).toBe("function");
    expect(typeof api.triggerWorkflowRegistration).toBe("function");
    expect(typeof api.getWorkflowExecution).toBe("function");

    const getSpy = jest
      .spyOn(axios, "get")
      .mockResolvedValue({ data: [] });
    const postSpy = jest
      .spyOn(axios, "post")
      .mockResolvedValue({ data: {} });

    await api.listWorkflowRegistrations();
    expect(getSpy.mock.calls[0][0]).toContain(
      `${Connection.ENDPOINT}/workflows/registrations`
    );

    await api.getWorkflowRegistration("reg-1");
    expect(getSpy.mock.calls[1][0]).toContain(
      `${Connection.ENDPOINT}/workflows/registrations/reg-1`
    );

    await api.triggerWorkflowRegistration("reg-1");
    expect(postSpy.mock.calls[0][0]).toContain(
      `${Connection.ENDPOINT}/workflows/registrations/reg-1/trigger`
    );

    await api.getWorkflowExecution("exec-1");
    expect(getSpy.mock.calls[2][0]).toContain(
      `${Connection.ENDPOINT}/workflows/executions/exec-1`
    );
  });

  /**
   * Test case 4 — Invalid Registration Visibility Test (design Exploratory #4)
   * Expected behavior: with the API (mocked at the module boundary)
   * returning an invalid registration, the UI surfaces its identity, its
   * invalid status, and its invalidReason (1.4, 2.1, 2.4).
   * Expected counterexample on unfixed code: nothing in the UI renders the
   * registration at all — the operator has no indication it exists, is
   * invalid, or why.
   */
  it("surfaces an invalid registration's status and invalidReason in the UI", async () => {
    renderAppAtDeployedWorkflows();

    // The invalidReason must be visible to the operator (2.4).
    await screen.findByText(
      /Missing required artifact file: manifest\.json/i,
      {},
      { timeout: 4000 }
    );

    // The registration's identity and invalid status must be shown (2.1).
    const identity = await screen.findAllByText(/wf-1/);
    expect(identity.length).toBeGreaterThan(0);
    const invalidStatus = await screen.findAllByText(/invalid/i);
    expect(invalidStatus.length).toBeGreaterThan(0);
  }, 15000);
});
