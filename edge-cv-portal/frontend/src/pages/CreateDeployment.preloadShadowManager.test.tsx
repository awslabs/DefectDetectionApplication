/**
 * Bug condition exploration component test —
 * shadowmanager-sync-config-on-revision task 1, case 4 (defect 1.2).
 *
 * Bugfix spec: .kiro/specs/shadowmanager-sync-config-on-revision/
 *
 * The UI revise flow preloads the existing deployment's components via
 * preloadExistingComponents, skipping only the autoManaged set
 * {aws.greengrass.Nucleus, aws.greengrass.LogManager}.
 * aws.greengrass.ShadowManager is NOT in the skip set, so it is resubmitted
 * BARE — the getTargetDeployment prefill API returns component_name +
 * component_version only, structurally dropping the configurationUpdate —
 * which permanently disarms the backend's ShadowManager synchronize
 * auto-include (the presence gate skips) after a target's first revision.
 *
 * This test asserts the FIXED behavior (the preloaded selection OMITS
 * ShadowManager, as it already omits Nucleus/LogManager, so the backend
 * fresh-add path manages it) and is EXPECTED TO FAIL on unfixed code —
 * ShadowManager lands in the preloaded selection. After task 3.2 adds
 * 'aws.greengrass.ShadowManager' to the autoManaged skip set, this same
 * file validates the fix (task 3.3).
 *
 * Property 1: Bug Condition (Fix Check) — the UI revise flow submits
 * ShadowManager absent.
 * Validates: Requirements 2.4 (defect: 1.2)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateDeployment from './CreateDeployment';

const { apiMocks, routerState } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listComponents: vi.fn(),
    listDevices: vi.fn(),
    getTargetDeployment: vi.fn(),
    getModel: vi.fn(),
    createDeployment: vi.fn(),
  },
  routerState: { search: '' },
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
    }
  }
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      // Any other API call the page happens to make resolves to an empty
      // object so effects settle without error.
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

vi.mock('../contexts/UsecaseContext', () => ({
  useUsecase: () => ({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams(routerState.search), vi.fn()],
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const LOCAL_SERVER = 'aws.edgeml.dda.LocalServer.x86_64';

// The catalog: the LocalServer plus a plain infra component, so preloaded
// siblings resolve through the catalog match path.
const PRIVATE_COMPONENTS = [
  {
    arn: 'arn:localserver',
    component_name: LOCAL_SERVER,
    latest_version: { componentVersion: '1.2.0' },
    description: 'DDA LocalServer',
    platforms: [],
  },
  {
    arn: 'arn:infra',
    component_name: 'com.dda.infra',
    latest_version: { componentVersion: '1.0.0' },
    description: 'Infra',
    platforms: [],
  },
];

// The thor1 revise shape: the existing deployment's prefill components —
// name + version ONLY (getTargetDeployment structurally drops any
// configurationUpdate) — include ShadowManager 2.3.15 alongside LocalServer
// and the already-skipped Nucleus/LogManager infrastructure.
const EXISTING_DEPLOYMENT = {
  deployment_id: 'dep-thor1',
  deployment_name: 'Existing',
  target_arn: 'arn:aws:iot:::thing/thor1-device',
  deployment_status: 'ACTIVE',
  revision_id: '10',
  creation_timestamp: '2026-08-14T00:00:00Z',
  components: [
    { component_name: LOCAL_SERVER, component_version: '1.2.0' },
    { component_name: 'com.dda.infra', component_version: '1.0.0' },
    {
      component_name: 'aws.greengrass.ShadowManager',
      component_version: '2.3.15',
    },
    { component_name: 'aws.greengrass.Nucleus', component_version: '2.12.0' },
    {
      component_name: 'aws.greengrass.LogManager',
      component_version: '2.3.7',
    },
  ],
};

function device(overrides: Record<string, unknown> = {}) {
  return {
    device_id: 'thor1-device',
    platform: 'linux',
    architecture: 'x86_64',
    target_architecture: 'x86_64',
    status: 'HEALTHY',
    installed_components: [],
    ...overrides,
  };
}

beforeEach(() => {
  routerState.search = '';
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC1' }],
    count: 1,
  });
  apiMocks.listComponents.mockImplementation((params: { scope?: string }) =>
    Promise.resolve({
      components: params.scope === 'PUBLIC' ? [] : PRIVATE_COMPONENTS,
    })
  );
  apiMocks.listDevices.mockResolvedValue({ devices: [device()], count: 1 });
  apiMocks.getTargetDeployment.mockResolvedValue({
    existing_deployment: EXISTING_DEPLOYMENT,
  });
  apiMocks.getModel.mockResolvedValue({ model: {} });
  apiMocks.createDeployment.mockResolvedValue({ deployment_id: 'dep-new' });
});

afterEach(() => {
  vi.clearAllMocks();
});

/**
 * The selected-components table (the preloaded selection). Located by its
 * content rather than table index so unrelated tables never shadow it; the
 * Technical Name column renders the raw component_name. Assertions are
 * scoped to this table because the page ALSO names LogManager in a static
 * CloudWatch informational alert.
 */
const findSelectionTable = (container: HTMLElement) =>
  createWrapper(container)
    .findAllTables()
    .find((t) => t.getElement().textContent?.includes(LOCAL_SERVER));

describe('CreateDeployment — revise preload skips ShadowManager (Req 2.4)', () => {
  it('omits aws.greengrass.ShadowManager from the preloaded selection like Nucleus/LogManager', async () => {
    routerState.search = 'target_device=thor1-device';
    const { container } = render(<CreateDeployment />);

    // The preload settles: the LocalServer sibling lands in the
    // selected-components table.
    await waitFor(() => {
      expect(findSelectionTable(container)).toBeDefined();
    });
    const table = findSelectionTable(container)!.getElement();
    expect(within(table).getAllByText('com.dda.infra').length).toBeGreaterThan(
      0
    );

    // Nucleus and LogManager are already autoManaged-skipped — they never
    // land in the preloaded selection.
    expect(within(table).queryByText('aws.greengrass.Nucleus')).toBeNull();
    expect(within(table).queryByText('aws.greengrass.LogManager')).toBeNull();

    // BUG (1.2): on unfixed code autoManaged = {Nucleus, LogManager} only,
    // so aws.greengrass.ShadowManager lands in the preloaded selection —
    // stripped of its configurationUpdate — and the revise submission
    // carries it bare, disarming the backend auto-include. Fixed contract:
    // the preload OMITS it so the backend fresh-add path manages it.
    expect(within(table).queryByText('aws.greengrass.ShadowManager')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Task 4.3 — Property 5: Fix Checking — Frontend Preload Skip Set
// (preservation-side cases; the exploration case above IS the core
// fix-check once green).
//
// Validates: Requirements 2.4, 3.4
// ---------------------------------------------------------------------------

// The same existing deployment WITHOUT ShadowManager: the preload must
// behave exactly as it did pre-fix (all non-auto-managed components
// preloaded unchanged; Nucleus/LogManager still skipped).
const EXISTING_DEPLOYMENT_WITHOUT_SM = {
  ...EXISTING_DEPLOYMENT,
  components: EXISTING_DEPLOYMENT.components.filter(
    (c) => c.component_name !== 'aws.greengrass.ShadowManager'
  ),
};

/** Renders the revise flow and waits for the preloaded selection table. */
async function renderRevise() {
  routerState.search = 'target_device=thor1-device';
  const { container } = render(<CreateDeployment />);
  await waitFor(() => {
    expect(findSelectionTable(container)).toBeDefined();
  });
  return container;
}

/** Clicks the revise-mode submit button (Cloudscape primary, type=submit). */
async function submitDeployment(container: HTMLElement) {
  const submit = createWrapper(container)
    .findAllButtons()
    .find((b) => b.getElement().textContent?.includes('Update Deployment'));
  expect(submit).toBeDefined();
  await act(async () => {
    submit!.getElement().click();
  });
}

describe('CreateDeployment — preload preservation + submission (task 4.3, Req 2.4, 3.4)', () => {
  it('prefill WITH ShadowManager: siblings preloaded with their versions; the submission payload carries NO ShadowManager entry', async () => {
    const container = await renderRevise();
    const table = findSelectionTable(container)!.getElement();

    // Every sibling component is preloaded with its existing version.
    expect(within(table).getAllByText(LOCAL_SERVER).length).toBeGreaterThan(0);
    expect(within(table).getAllByText('com.dda.infra').length).toBeGreaterThan(
      0
    );
    expect(within(table).getAllByText('1.2.0').length).toBeGreaterThan(0);
    expect(within(table).getAllByText('1.0.0').length).toBeGreaterThan(0);
    // ShadowManager is omitted from the selection (the fix).
    expect(
      within(table).queryByText('aws.greengrass.ShadowManager')
    ).toBeNull();

    // Submit the revision: the eventual payload carries the siblings with
    // their versions and NO ShadowManager entry (nor Nucleus/LogManager) —
    // the backend fresh-add path manages the auto-includes.
    await submitDeployment(container);
    await waitFor(() => {
      expect(apiMocks.createDeployment).toHaveBeenCalledTimes(1);
    });
    const payload = apiMocks.createDeployment.mock.calls[0][0];
    expect(payload.components).toEqual([
      { component_name: LOCAL_SERVER, component_version: '1.2.0' },
      { component_name: 'com.dda.infra', component_version: '1.0.0' },
    ]);
    expect(
      payload.components.some(
        (c: { component_name: string }) =>
          c.component_name === 'aws.greengrass.ShadowManager'
      )
    ).toBe(false);
    expect(payload.target_devices).toEqual(['thor1-device']);
  });

  it('prefill WITHOUT ShadowManager: selection identical to pre-fix behavior (all non-auto-managed components preloaded unchanged)', async () => {
    apiMocks.getTargetDeployment.mockResolvedValue({
      existing_deployment: EXISTING_DEPLOYMENT_WITHOUT_SM,
    });
    const container = await renderRevise();
    const table = findSelectionTable(container)!.getElement();

    // The non-auto-managed components preload unchanged, versions intact.
    expect(within(table).getAllByText(LOCAL_SERVER).length).toBeGreaterThan(0);
    expect(within(table).getAllByText('com.dda.infra').length).toBeGreaterThan(
      0
    );
    expect(within(table).getAllByText('1.2.0').length).toBeGreaterThan(0);
    expect(within(table).getAllByText('1.0.0').length).toBeGreaterThan(0);
    expect(
      within(table).queryByText('aws.greengrass.ShadowManager')
    ).toBeNull();

    // The submission matches pre-fix behavior exactly: both components,
    // their versions verbatim.
    await submitDeployment(container);
    await waitFor(() => {
      expect(apiMocks.createDeployment).toHaveBeenCalledTimes(1);
    });
    const payload = apiMocks.createDeployment.mock.calls[0][0];
    expect(payload.components).toEqual([
      { component_name: LOCAL_SERVER, component_version: '1.2.0' },
      { component_name: 'com.dda.infra', component_version: '1.0.0' },
    ]);
  });

  it('Nucleus and LogManager are still skipped by the preload (skip set preserved)', async () => {
    apiMocks.getTargetDeployment.mockResolvedValue({
      existing_deployment: EXISTING_DEPLOYMENT_WITHOUT_SM,
    });
    const container = await renderRevise();
    const table = findSelectionTable(container)!.getElement();

    // The pre-existing auto-managed skips are untouched by the fix.
    expect(within(table).queryByText('aws.greengrass.Nucleus')).toBeNull();
    expect(within(table).queryByText('aws.greengrass.LogManager')).toBeNull();
  });
});
