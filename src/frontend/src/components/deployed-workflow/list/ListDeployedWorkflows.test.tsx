/*
 * Unit tests for the deployed-workflows list page (task 5).
 *
 * **Feature: edge-workflow-run-ux, Property 1: Deployed workflow registrations
 * are visible and runnable from the UI**
 * **Validates: Requirements 2.1, 2.4, 2.5**
 *
 * The API is mocked at the module boundary
 * (jest.mock("api/WorkflowRegistrationAPI")); CRA's jest config resets mocks
 * before each test, so implementations are (re)installed per test.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type { WorkflowRegistration } from "api/WorkflowRegistrationAPI";
import ListDeployedWorkflows from "./ListDeployedWorkflows";

jest.mock("api/WorkflowRegistrationAPI");

const REGISTERED: WorkflowRegistration = {
  registrationId: "reg-registered-1",
  workflowId: "wf-alpha",
  version: "3",
  arch: "arm64",
  artifactPath: "/greengrass/artifacts/wf-alpha/3",
  status: "registered",
  registeredAt: 1700000000,
};

const INVALID: WorkflowRegistration = {
  registrationId: "reg-invalid-1",
  workflowId: "wf-beta",
  version: "7",
  arch: "x86_64",
  artifactPath: "/greengrass/artifacts/wf-beta/7",
  status: "invalid",
  registeredAt: 1700000500,
  invalidReason: "Missing required artifact file: manifest.json",
};

function renderList(): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
    logger: {
      log: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/deployed-workflows"]}>
        <ListDeployedWorkflows />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ListDeployedWorkflows", () => {
  it("renders a row for each registration with identity and status (2.1)", async () => {
    (
      RegistrationAPI.listWorkflowRegistrations as jest.Mock
    ).mockResolvedValue([REGISTERED, INVALID]);

    const { container } = renderList();

    // Registered row: identity + status, linking to its details page.
    const registeredLink = await screen.findByRole("link", {
      name: "wf-alpha",
    });
    expect(registeredLink).toHaveAttribute(
      "href",
      "/deployed-workflows/reg-registered-1",
    );
    const registeredRow = registeredLink.closest("tr")!;
    expect(registeredRow.textContent).toContain("3");
    expect(registeredRow.textContent).toContain("arm64");
    expect(registeredRow.textContent).toContain("Registered");

    // Invalid row: identity + invalid status.
    const invalidLink = screen.getByRole("link", { name: "wf-beta" });
    expect(invalidLink).toHaveAttribute(
      "href",
      "/deployed-workflows/reg-invalid-1",
    );
    const invalidRow = invalidLink.closest("tr")!;
    expect(invalidRow.textContent).toContain("7");
    expect(invalidRow.textContent).toContain("x86_64");
    expect(invalidRow.textContent).toContain("Invalid");

    // Exactly the two rows are rendered.
    expect(
      container.querySelectorAll('a[href^="/deployed-workflows/"]'),
    ).toHaveLength(2);
  });

  it("shows the invalid reason for an invalid registration (2.4)", async () => {
    (
      RegistrationAPI.listWorkflowRegistrations as jest.Mock
    ).mockResolvedValue([REGISTERED, INVALID]);

    renderList();

    const reason = await screen.findByText(
      "Missing required artifact file: manifest.json",
    );
    // The reason is shown on the invalid registration's row.
    expect(reason.closest("tr")!.textContent).toContain("wf-beta");
  });

  it("renders the empty state without error for an empty list (2.5)", async () => {
    (
      RegistrationAPI.listWorkflowRegistrations as jest.Mock
    ).mockResolvedValue([]);

    const { container } = renderList();

    await screen.findByText("No deployed workflows");
    expect(
      screen.getByText("No deployed workflows to display."),
    ).toBeInTheDocument();
    expect(
      container.querySelectorAll('a[href^="/deployed-workflows/"]'),
    ).toHaveLength(0);
  });

  it("degrades gracefully on API failure: no rows, no crash (2.1, 2.5)", async () => {
    (
      RegistrationAPI.listWorkflowRegistrations as jest.Mock
    ).mockRejectedValue(new Error("network down"));

    const { container } = renderList();

    // The page header still renders and the query settles without rows.
    // (Matching the legacy ListWorkflows convention, the page falls back to
    // the empty table state rather than a dedicated error banner.)
    expect(
      screen.getByRole("heading", { name: /Deployed workflows/ }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.getByText("No deployed workflows to display."),
      ).toBeInTheDocument();
    });
    expect(
      container.querySelectorAll('a[href^="/deployed-workflows/"]'),
    ).toHaveLength(0);
  });
});
