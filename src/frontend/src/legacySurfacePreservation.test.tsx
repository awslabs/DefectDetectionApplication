/*
 * Preservation property tests (edge-workflow-run-ux, task 3).
 *
 * **Feature: edge-workflow-run-ux, Property 2: Existing pages, API calls, and
 * backend behavior unchanged**
 * **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
 *
 * Observation-first methodology: the expectations below were OBSERVED on the
 * UNFIXED code (App.tsx route table, SideNav.tsx sections/links, WorkflowAPI.ts
 * endpoints, config/Interface.tsx exports) and then encoded here. These tests
 * PASS on unfixed code (baseline) and MUST STILL PASS after the fix, whose only
 * allowed deltas are:
 *   - one new top-level `deployed-workflows` route subtree in App.tsx
 *   - one new SideNav link { text: "Deployed workflows", href: "/deployed-workflows" }
 *   - new files under api/WorkflowRegistrationAPI.ts and components/deployed-workflow/
 *
 * Observed baseline encoded here:
 *   - Route table (App.tsx): "/" redirects to /result; top-level routes
 *     image-sources (index/add/:id/:id\/edit/:id\/edit-settings/:id\/edit-region-of-interest),
 *     workflows (index/:workflowId/:workflowId\/edit), models, result,
 *     history (index/:workflowId/:workflowId\/detail\/:captureId), capture (/:workflowId),
 *     capture-results (index/:workflowId/:workflowId\/detail\/:captureId), application-health.
 *   - SideNav sections in order: Configure, Capture, Infer, Application health,
 *     with the 8 links encoded in EXPECTED_NAV_LINKS below.
 *   - WorkflowAPI.ts targets `${Connection.ENDPOINT}/workflows...` via APIList.
 *   - config/Interface.tsx exports exactly { Connection, APIList, AppDescriptions }.
 *
 * Backend baseline (3.3/3.4): `git status --porcelain -- src/backend` is EMPTY
 * at the time these tests were written; it must still be empty after the fix
 * (the backend, including its 409 rejection of triggers on invalid
 * registrations, is preserved by not touching it).
 *
 * Testing approach:
 *   - The real application root is rendered (real router from App.tsx, real
 *     AuthContextProvider/AppLayoutProvider/AuthProvider/Layout), with API
 *     modules mocked at the module boundary (WorkflowAPI, AuthAPI, Station,
 *     FeatureConfigurationAPI, ImageSourceAPI) so the legacy workflow pages
 *     render for real in jsdom.
 *   - NON-workflow leaf pages are mocked with inert marker components. That is
 *     deliberate: the preservation property under test is the ROUTE TABLE
 *     mapping (route -> component identity), not the internals of each page.
 *     jest.mock replaces exactly the module the route table references, so if
 *     the fix rewired any pre-existing route to a different component, the
 *     expected marker would no longer render and the property would fail.
 */

import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import fc from "fast-check";
import axios from "axios";
import * as InterfaceModule from "config/Interface";
import { APIList, Connection } from "config/Interface";
import * as WorkflowAPI from "api/WorkflowAPI";
import * as AuthAPI from "api/AuthAPI";
import * as LocalAuthAPI from "api/LocalAuthAPI";
import * as StationAPI from "api/Station";
import * as FeatureConfigurationAPI from "api/FeatureConfigurationAPI";
import * as ImageSourceAPI from "api/ImageSourceAPI";
import SideNav from "components/layout/SideNav";

/* ------------------------------------------------------------------ */
/* jsdom polyfills (test-only; Cloudscape may touch these APIs)        */
/* ------------------------------------------------------------------ */

if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}
if (!(window as any).ResizeObserver) {
  (window as any).ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}
if (!(window as any).IntersectionObserver) {
  (window as any).IntersectionObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): unknown[] {
      return [];
    }
  };
}

/* ------------------------------------------------------------------ */
/* Module-boundary mocks                                               */
/* ------------------------------------------------------------------ */

// Legacy API client mocked at the module boundary (per design/task 3).
jest.mock("api/WorkflowAPI");
// App-shell data dependencies so the real providers/layout render in jsdom.
jest.mock("api/AuthAPI");
// LoginGate (portal-user-manager local-auth feature) fetches GET
// /local-auth/status at app mount; mock it at the module boundary so the
// gate resolves "local login disabled" and route pages render as before.
jest.mock("api/LocalAuthAPI");
jest.mock("api/Station");
// Used by the real EditWorkflow page.
jest.mock("api/FeatureConfigurationAPI");
jest.mock("api/ImageSourceAPI");

// Non-workflow leaf pages -> inert markers (route-table identity probes).
jest.mock("components/image-source/list/ListImageSources", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-list-image-sources" }),
}));
jest.mock("components/image-source/add/AddImageSource", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-add-image-source" }),
}));
jest.mock("components/image-source/details/ImageSourceDetails", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-image-source-details" }),
}));
jest.mock("components/image-source/edit/EditImageSource", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-edit-image-source" }),
}));
jest.mock("components/image-settings/EditImageSettings", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-edit-image-settings" }),
}));
jest.mock("components/image-source/roi/EditRoI", () => ({
  __esModule: true,
  default: () => require("react").createElement("div", { "data-testid": "page-edit-roi" }),
}));
jest.mock("components/image-source/image-capture/ImageCapturePage", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-image-capture" }),
}));
jest.mock("components/model/DeployedModels", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-deployed-models" }),
}));
jest.mock("components/live-result/LiveResults", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-live-results" }),
}));
jest.mock("components/result-history/ResultHistory", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-result-history" }),
}));
jest.mock("components/result-history/ResultDetails", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-result-details" }),
}));
jest.mock("components/result-history/ImageCaptureResultHistory", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-capture-results" }),
}));
jest.mock("components/application-health/ApplicationHealthOverview", () => ({
  __esModule: true,
  default: () =>
    require("react").createElement("div", { "data-testid": "page-application-health" }),
}));

/* ------------------------------------------------------------------ */
/* Fixtures                                                            */
/* ------------------------------------------------------------------ */

// Minimal legacy workflow. Deliberately has NO imageSources so WorkflowDetails
// renders its (observed) "not configured" branch, which needs no further data.
const LEGACY_WORKFLOW = {
  workflowId: "wf-123",
  name: "Legacy workflow",
  description: "A legacy Pipeline_Configuration workflow",
  lastUpdatedTime: 1700000000000,
};

beforeEach(() => {
  // CRA jest resets mocks before each test, so (re)install implementations here.
  (AuthAPI.fetchAuthConfig as jest.Mock).mockResolvedValue({
    auth_enabled: false,
    auth_settings: {},
  });
  // "Local auth disabled" -> LoginGate renders the app directly (no login).
  (LocalAuthAPI.fetchLocalAuthStatus as jest.Mock).mockResolvedValue({
    localLoginEnabled: false,
  });
  (StationAPI.getStation as jest.Mock).mockResolvedValue({
    name: "Test station",
    version: "1.0.0",
    tenantId: "tenant-1",
    deviceId: "device-1",
    webuxUrl: "",
  });
  (WorkflowAPI.listWorkflows as jest.Mock).mockResolvedValue([]);
  (WorkflowAPI.getWorkflow as jest.Mock).mockResolvedValue(LEGACY_WORKFLOW);
  (FeatureConfigurationAPI.listFeatureConfigurations as jest.Mock).mockResolvedValue([]);
  (ImageSourceAPI.listImageSources as jest.Mock).mockResolvedValue([]);
});

afterEach(() => {
  jest.restoreAllMocks();
});

/* ------------------------------------------------------------------ */
/* Real-app rendering helper                                           */
/* ------------------------------------------------------------------ */

// App.tsx creates its browser router at module scope from window.location, so
// the URL must be set BEFORE the module is first required. The single App /
// router instance is then reused (re-requiring the module graph would create a
// second React copy and break hooks); subsequent navigation is driven through
// the history API + popstate, which the already-initialized router listens to.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let App: any;

function renderAppAt(route: string): void {
  window.history.pushState({}, "preservation", route);
  if (!App) {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    App = require("App").default;
  } else {
    act(() => {
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
  }
  render(<App />);
}

const FIND_TIMEOUT = { timeout: 10000 };

/* ------------------------------------------------------------------ */
/* Observed pre-existing route table (App.tsx, unfixed code)           */
/* ------------------------------------------------------------------ */

interface PreservedRoute {
  path: string;
  /** Resolves iff the route renders the same component as the original table. */
  expectResolved: () => Promise<void>;
}

const expectMarker =
  (testId: string) =>
  async (): Promise<void> => {
    const markers = await screen.findAllByTestId(testId, {}, FIND_TIMEOUT);
    expect(markers.length).toBeGreaterThan(0);
  };

const PRESERVED_ROUTES: PreservedRoute[] = [
  // "/" redirects to /result (observed <Navigate replace to="/result" />)
  { path: "/", expectResolved: expectMarker("page-live-results") },
  { path: "/image-sources", expectResolved: expectMarker("page-list-image-sources") },
  { path: "/image-sources/add", expectResolved: expectMarker("page-add-image-source") },
  { path: "/image-sources/src-1", expectResolved: expectMarker("page-image-source-details") },
  { path: "/image-sources/src-1/edit", expectResolved: expectMarker("page-edit-image-source") },
  {
    path: "/image-sources/src-1/edit-settings",
    expectResolved: expectMarker("page-edit-image-settings"),
  },
  {
    path: "/image-sources/src-1/edit-region-of-interest",
    expectResolved: expectMarker("page-edit-roi"),
  },
  { path: "/models", expectResolved: expectMarker("page-deployed-models") },
  { path: "/result", expectResolved: expectMarker("page-live-results") },
  { path: "/history", expectResolved: expectMarker("page-result-history") },
  { path: "/history/wf-1", expectResolved: expectMarker("page-result-history") },
  { path: "/history/wf-1/detail/cap-1", expectResolved: expectMarker("page-result-details") },
  { path: "/capture", expectResolved: expectMarker("page-image-capture") },
  { path: "/capture/wf-1", expectResolved: expectMarker("page-image-capture") },
  { path: "/capture-results", expectResolved: expectMarker("page-capture-results") },
  { path: "/capture-results/wf-1", expectResolved: expectMarker("page-capture-results") },
  {
    path: "/capture-results/wf-1/detail/cap-1",
    expectResolved: expectMarker("page-result-details"),
  },
  { path: "/application-health", expectResolved: expectMarker("page-application-health") },
  // Legacy Pipeline_Configuration pages render for REAL (WorkflowAPI mocked at
  // the module boundary); identified by observed page-unique UI.
  {
    path: "/workflows",
    expectResolved: async (): Promise<void> => {
      await screen.findByRole("button", { name: "Add workflow" }, FIND_TIMEOUT);
    },
  },
  {
    path: "/workflows/wf-123",
    expectResolved: async (): Promise<void> => {
      await screen.findByText(/This workflow isn't configured/, {}, FIND_TIMEOUT);
    },
  },
  {
    path: "/workflows/wf-123/edit",
    expectResolved: async (): Promise<void> => {
      await screen.findByRole("heading", { name: "Edit workflow" }, FIND_TIMEOUT);
    },
  },
];

/* ------------------------------------------------------------------ */
/* Observed pre-existing SideNav items (SideNav.tsx, unfixed code)     */
/* ------------------------------------------------------------------ */

interface NavLink {
  text: string;
  href: string;
}

const EXPECTED_NAV_SECTIONS = ["Configure", "Capture", "Infer", "Application health"];

const EXPECTED_NAV_LINKS: NavLink[] = [
  // Section: Configure
  { text: "Image sources", href: "/image-sources" },
  { text: "Workflows", href: "/workflows" },
  { text: "Deployed models", href: "/models" },
  // Section: Capture
  { text: "Capture images", href: "/capture" },
  { text: "Image capture results", href: "/capture-results" },
  // Section: Infer
  { text: "Run inference", href: "/result" },
  { text: "Inference results", href: "/history" },
  // Section: Application health
  { text: "Application health overview", href: "/application-health" },
];

// The one delta the fix is allowed to introduce (design: SideNav.tsx additive
// only). Encoded so this suite passes NOW and STILL passes after the fix.
const ALLOWED_NEW_NAV_LINK: NavLink = {
  text: "Deployed workflows",
  href: "/deployed-workflows",
};

function isOrderedSubsequence(expected: NavLink[], actual: NavLink[]): boolean {
  let i = 0;
  for (const link of actual) {
    if (
      i < expected.length &&
      link.text === expected[i].text &&
      link.href === expected[i].href
    ) {
      i += 1;
    }
  }
  return i === expected.length;
}

/* ------------------------------------------------------------------ */
/* 1. Legacy Pipeline_Configuration route preservation (3.1)           */
/* ------------------------------------------------------------------ */

describe("legacy Pipeline_Configuration routes (3.1)", () => {
  it(
    "/workflows renders the legacy ListWorkflows page via the legacy WorkflowAPI client",
    async () => {
      renderAppAt("/workflows");

      // Observed unfixed behavior: list page header actions and empty state.
      await screen.findByRole("button", { name: "Add workflow" }, FIND_TIMEOUT);
      await screen.findByRole("button", { name: "Edit workflow" }, FIND_TIMEOUT);
      await screen.findByText("No workflows to display.", {}, FIND_TIMEOUT);

      // The page keeps calling the legacy /workflows endpoints via the
      // existing API client module (mocked at that exact boundary).
      expect(WorkflowAPI.listWorkflows).toHaveBeenCalled();
    },
    60000
  );

  it(
    "/workflows/:workflowId renders the legacy WorkflowDetails page",
    async () => {
      renderAppAt("/workflows/wf-123");

      // Observed unfixed behavior for a workflow without imageSources:
      // DetailsHeader + "not configured" alert.
      await screen.findByText(/This workflow isn't configured/, {}, FIND_TIMEOUT);
      // findAll: Cloudscape modal portals (delete/download modals) may add
      // hidden duplicates in jsdom, see note in the edit-route test below.
      expect(
        (await screen.findAllByRole("button", { name: "Delete workflow" }, FIND_TIMEOUT)).length
      ).toBeGreaterThan(0);
      expect(
        (await screen.findAllByRole("button", { name: "Edit workflow" }, FIND_TIMEOUT)).length
      ).toBeGreaterThan(0);

      expect(WorkflowAPI.getWorkflow).toHaveBeenCalledWith("wf-123");
    },
    60000
  );

  it(
    "/workflows/:workflowId/edit renders the legacy EditWorkflow page",
    async () => {
      renderAppAt("/workflows/wf-123/edit");

      // Observed unfixed behavior: form header (also the breadcrumb handle
      // text "Edit workflow" defined on the route) and Save/Cancel actions.
      await screen.findByRole("heading", { name: "Edit workflow" }, FIND_TIMEOUT);
      // findAll: Cloudscape renders hidden modal portals into document.body
      // whose CSS-based hiding jsdom does not apply, so duplicate buttons can
      // legitimately appear (e.g. an output-editing modal's own Save).
      expect(
        (await screen.findAllByRole("button", { name: "Save" }, FIND_TIMEOUT)).length
      ).toBeGreaterThan(0);
      expect(
        (await screen.findAllByRole("button", { name: "Cancel" }, FIND_TIMEOUT)).length
      ).toBeGreaterThan(0);

      // The form is populated from the legacy WorkflowAPI response (served by
      // the mocked module directly or via the app's query cache keyed
      // ["getWorkflow", "wf-123"] that only the legacy client fills).
      await screen.findByDisplayValue("Legacy workflow", {}, FIND_TIMEOUT);
    },
    60000
  );
});

/* ------------------------------------------------------------------ */
/* 2. Side navigation preservation (3.2)                               */
/* ------------------------------------------------------------------ */

describe("side navigation pre-existing items (3.2)", () => {
  function renderedNavLinks(): NavLink[] {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <SideNav />
      </MemoryRouter>
    );
    return Array.from(container.querySelectorAll("a[href]"))
      .map((a) => ({
        text: (a.textContent || "").trim(),
        href: a.getAttribute("href") || "",
      }))
      // Exclude the station header link (href "/"), which is not a nav item.
      .filter((link) => link.href !== "/");
  }

  it("keeps every pre-existing link with unchanged text, href, and order", () => {
    const links = renderedNavLinks();

    // All pre-existing items appear, in the observed order, as a subsequence
    // (a subsequence check is used so inserting the one allowed new link
    // anywhere does not break this test after the fix).
    expect(isOrderedSubsequence(EXPECTED_NAV_LINKS, links)).toBe(true);

    // No pre-existing item changed: each expected href maps to exactly one
    // link and its text is identical (and vice versa for text).
    for (const expected of EXPECTED_NAV_LINKS) {
      expect(links.filter((l) => l.href === expected.href)).toEqual([expected]);
      expect(links.filter((l) => l.text === expected.text)).toEqual([expected]);
    }
  });

  it('allows no delta other than the single new "Deployed workflows" link', () => {
    const links = renderedNavLinks();
    const expectedHrefs = new Set(EXPECTED_NAV_LINKS.map((l) => l.href));
    const extras = links.filter((l) => !expectedHrefs.has(l.href));

    // On unfixed code: no extras. After the fix: exactly the one allowed link.
    expect(extras.length).toBeLessThanOrEqual(1);
    for (const extra of extras) {
      expect(extra).toEqual(ALLOWED_NEW_NAV_LINK);
    }
  });

  it("keeps the observed section structure", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <SideNav />
      </MemoryRouter>
    );
    for (const section of EXPECTED_NAV_SECTIONS) {
      expect(screen.getAllByText(section).length).toBeGreaterThan(0);
    }
  });
});

/* ------------------------------------------------------------------ */
/* 3. Legacy API client and config preservation (3.1, 3.3)             */
/* ------------------------------------------------------------------ */

describe("legacy API client and config/Interface exports (3.1, 3.3)", () => {
  it("config/Interface.tsx exports are unchanged", () => {
    const E = Connection.ENDPOINT;

    // Exported names are exactly the observed set.
    expect(
      Object.keys(InterfaceModule)
        .filter((k) => k !== "__esModule")
        .sort()
    ).toEqual(["APIList", "AppDescriptions", "Connection"]);

    expect(typeof E).toBe("string");
    expect(E).toMatch(/^https?:\/\//);

    // Exact observed APIList (keys AND values) — the fix must not touch it.
    expect(APIList).toEqual({
      camerasAPI: `${E}/cameras`,
      cameraFeatureBoundsAPI: `${E}/cameras/{camera_id}/feature-bounds`,
      cameraFeaturesAPI: `${E}/cameras/{camera_id}/features`,
      previewImageAPI: `${E}/cameras/{camera_id}/preview`,
      executePipelineAPI: `${E}/cameras/{camera_id}/execute-pipeline`,
      listStreamsAPI: `${E}/streams`,
      getStreamImageAPI: `${E}/streams/{stream_id}/images?maxResults=1`,
      streamSubscribeAPI: `${E}/streams/{camera_id}/subscribe`,
      streamFrameAPI: `${E}/streams/{camera_id}/frame`,
      streamHeartbeatAPI: `${E}/streams/{camera_id}/heartbeat`,
      streamUnsubscribeAPI: `${E}/streams/{camera_id}/unsubscribe`,
      streamViewersAPI: `${E}/streams/{camera_id}/viewers`,
      capturedImageAPI: `${E}/captured-images`,
      imageSourcesAPI: `${E}/image-sources`,
      featureConfigurations: `${E}/feature-configurations`,
      workflows: `${E}/workflows`,
      workflowCaptureTaskAPI: `${E}/workflows/{workflow_id}/capture-task`,
      workflowResultAPI: `${E}/workflows/{workflow_id}/results/{capture_id}`,
      systemHealth: `${E}/system-health`,
      snapshot: `${E}/snapshot`,
      restartDda: `${E}/restart-dda`,
      getDdaComponentStatus: `${E}/dda-component-status`,
      greengrassComponents: `${E}/greengrass-components`,
      getStation: `${E}/system/station`,
      getCapture: `${E}/workflows/{workflow_id}/capture-details/{capture_id}`,
      getAuthConfig: `${E}/authorization-configurations`,
      // Observed-baseline update (suite maintenance convention): the
      // portal-user-manager local-auth feature added these two entries to
      // config/Interface.tsx; the baseline was re-observed and updated.
      getLocalAuthStatus: `${E}/local-auth/status`,
      postLocalAuthLogin: `${E}/local-auth/login`,
    });

    expect(Object.keys(InterfaceModule.AppDescriptions).sort()).toEqual([
      "captureImageDes",
      "processImageDes",
      "viewModelsDes",
      "viewResiltsDes",
    ]);
  });

  it("WorkflowAPI.ts endpoints still target ${Connection.ENDPOINT}/workflows", async () => {
    // Bypass the module-boundary mock to exercise the REAL legacy client,
    // spying on axios so no network is touched.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const actualWorkflowAPI: any = jest.requireActual("api/WorkflowAPI");
    const E = Connection.ENDPOINT;

    const getSpy = jest.spyOn(axios, "get").mockResolvedValue({ data: [] });
    const postSpy = jest.spyOn(axios, "post").mockResolvedValue({ data: {} });
    const deleteSpy = jest.spyOn(axios, "delete").mockResolvedValue({ data: undefined });

    await actualWorkflowAPI.listWorkflows();
    expect(getSpy).toHaveBeenLastCalledWith(`${E}/workflows`);

    await actualWorkflowAPI.getWorkflow("wf-9");
    expect(getSpy).toHaveBeenLastCalledWith(`${E}/workflows/wf-9`);

    await actualWorkflowAPI.runWorkflow("wf-9");
    expect(postSpy).toHaveBeenLastCalledWith(`${E}/workflows/wf-9/run`, {
      returnImageString: false,
    });

    await actualWorkflowAPI.createWorkflow();
    expect(postSpy).toHaveBeenLastCalledWith(`${E}/workflows`);

    await actualWorkflowAPI.deleteWorkflow("wf-9");
    expect(deleteSpy).toHaveBeenLastCalledWith(`${E}/workflows/wf-9`);
  });
});

/* ------------------------------------------------------------------ */
/* 4. Property: pre-existing routes resolve unchanged (3.1, 3.2)       */
/* ------------------------------------------------------------------ */

describe("route table preservation property (3.1, 3.2)", () => {
  /**
   * **Feature: edge-workflow-run-ux, Property 2: Existing pages, API calls,
   * and backend behavior unchanged**
   * **Validates: Requirements 3.1, 3.2**
   *
   * For ANY route drawn from the pre-existing route set (observed in App.tsx
   * on unfixed code), the application's router resolves it to the same
   * component as the original route table. The generator produces a random
   * full permutation of the route set, so every pre-existing route is
   * exercised on every run, in random order (the fix adds only a disjoint
   * `deployed-workflows` subtree, so this must keep passing afterwards).
   */
  it(
    "resolves every pre-existing route to the same page component as the original route table",
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc.shuffledSubarray(PRESERVED_ROUTES, {
            minLength: PRESERVED_ROUTES.length,
            maxLength: PRESERVED_ROUTES.length,
          }),
          async (routesInRandomOrder) => {
            for (const route of routesInRandomOrder) {
              cleanup();
              renderAppAt(route.path);
              await route.expectResolved();
            }
          }
        ),
        { numRuns: 2 }
      );
    },
    600000
  );
});
