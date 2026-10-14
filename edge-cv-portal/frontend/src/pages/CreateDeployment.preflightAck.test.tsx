/**
 * Component tests for the Create/Revise Deployment screen's pre-submit
 * closure validation surfacing and the SPECIFIC acknowledgement
 * (deployment-preflight-validation task 4.7 — Requirements 2.13, 2.14,
 * 2.15, 2.16).
 *
 * Covers:
 * - a 409 PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED refusal renders the finding,
 *   the effective-outcome statement in full, and every selected component
 *   that still requires the de-selected one (2.13, 2.15);
 * - acknowledging the NAMED component and re-submitting sends exactly
 *   those names in `acknowledged_retained_components` (2.14);
 * - changing the component set clears the pending acknowledgement, so the
 *   re-submit carries none and the backend re-reports the refusal instead
 *   of silently authorizing it (2.14);
 * - a 409 PREFLIGHT_VALIDATION_FAILED blocking finding offers NO
 *   acknowledgement control at all (2.16).
 *
 * Conventions per `CreateDeployment.archFilter.test.tsx` /
 * `CreateDeployment.preloadShadowManager.test.tsx`: hoisted `apiService`
 * proxy mock, router mock, Cloudscape test utils. The fixtures are
 * Counterexample C's verbatim shapes, in the wire form
 * `deployment_preflight.py` produces.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateDeployment from './CreateDeployment';
import { ApiError } from '../services/api';

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
  // The real ApiError carries the structured envelope's code and details
  // (services/api.ts) — the preflight refusal is unreadable without them.
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
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
// Fixtures — Counterexample C on jetson-thor1 (a JP7 device)
// ---------------------------------------------------------------------------

const WORKFLOW = 'dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8';
const RETAINED_MODEL = 'model-vllm-qwen3-5-9b-jetson-xavier-jp7';

// The catalog holds the still-selected workflow and one plain component the
// test can remove to change the submitted set. The de-selected model is
// deliberately NOT in the selection — the finding is computed by the backend
// from the target's previous deployment.
const PRIVATE_COMPONENTS = [
  {
    arn: 'arn:workflow',
    component_name: WORKFLOW,
    latest_version: { componentVersion: '12.0.0' },
    description: 'Packaged workflow',
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

const EXISTING_DEPLOYMENT = {
  deployment_id: 'dep-thor1',
  deployment_name: 'Existing',
  target_arn: 'arn:aws:iot:::thing/jp7-device',
  deployment_status: 'ACTIVE',
  revision_id: '21',
  creation_timestamp: '2026-08-26T20:39:36.933Z',
  components: [
    { component_name: WORKFLOW, component_version: '12.0.0' },
    { component_name: 'com.dda.infra', component_version: '1.0.0' },
  ],
};

const EFFECTIVE_OUTCOME =
  `${RETAINED_MODEL} will REMAIN installed and running on the target ` +
  `device(s) as a resolved dependency of ${WORKFLOW} 12.0.0, and will NOT be ` +
  `removed from the device by this deployment.`;

const DESELECTED_FINDING = {
  kind: 'deselected-still-required',
  finding_class: 'acknowledgement-required',
  component_name: RETAINED_MODEL,
  component_version: '1.0.0',
  required_by: [
    {
      component_name: WORKFLOW,
      component_version: '12.0.0',
      version_requirement: '>=0.0.0',
      dependency_type: 'HARD',
    },
  ],
  remains_installed: true,
  removed_by_this_deployment: false,
  effective_outcome: EFFECTIVE_OUTCOME,
  remediation:
    `De-select the component(s) that require ${RETAINED_MODEL} in the same ` +
    `edit and submit once, or re-submit with ` +
    `acknowledged_retained_components naming exactly ${RETAINED_MODEL}.`,
};

const PLATFORM_FINDING = {
  kind: 'platform-mismatch',
  finding_class: 'blocking-invalid',
  component_name: 'dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe',
  component_version: '1.0.0',
  claimed_platforms: [
    { os: 'linux', variant: 'arm64_jp5', architecture: 'aarch64' },
    { os: 'linux', variant: 'arm64_jp6', architecture: 'aarch64' },
  ],
  devices: [
    {
      thing_name: 'jp7-device',
      platform: { os: 'linux', architecture: 'aarch64', variant: 'arm64_jp7' },
    },
  ],
  remediation: 'Re-package it for the target JetPack, or de-select it.',
};

function acknowledgementRequiredError() {
  return new ApiError(
    'One or more de-selected components will remain installed on the target ' +
      'devices as resolved dependencies of components that are still ' +
      'selected; the deployment was not submitted.',
    409,
    'PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED',
    {
      findings: [DESELECTED_FINDING],
      acknowledgement_field: 'acknowledged_retained_components',
      acknowledgement_required_for: [RETAINED_MODEL],
    }
  );
}

function validationFailedError() {
  return new ApiError(
    'One or more selected components, or components in their dependency ' +
      'closure, cannot be deployed to the target devices; the deployment ' +
      'was not submitted.',
    409,
    'PREFLIGHT_VALIDATION_FAILED',
    { findings: [PLATFORM_FINDING, DESELECTED_FINDING] }
  );
}

function device(overrides: Record<string, unknown> = {}) {
  return {
    device_id: 'jp7-device',
    platform: 'linux',
    architecture: 'aarch64',
    target_architecture: 'arm64_jp7',
    status: 'HEALTHY',
    // Empty so the unrelated component-removal confirmation modal (driven
    // by the device's installed components) stays out of this test.
    installed_components: [],
    ...overrides,
  };
}

beforeEach(() => {
  routerState.search = 'target_device=jp7-device';
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
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

/** The selected-components table (the revise-mode preloaded selection). */
const findSelectionTable = (container: HTMLElement) =>
  createWrapper(container)
    .findAllTables()
    .find((t) => t.getElement().textContent?.includes('com.dda.infra'));

async function renderRevise() {
  const { container } = render(<CreateDeployment />);
  await waitFor(() => {
    expect(findSelectionTable(container)).toBeDefined();
  });
  return container;
}

async function submitDeployment(container: HTMLElement) {
  const submit = createWrapper(container)
    .findAllButtons()
    .find((b) => b.getElement().textContent?.includes('Update Deployment'));
  expect(submit).toBeDefined();
  await act(async () => {
    submit!.getElement().click();
  });
}

/** The acknowledgement checkbox naming `componentName`, if offered. */
function findAcknowledgementCheckbox(container: HTMLElement, componentName: string) {
  return createWrapper(container)
    .findAllCheckboxes()
    .find((c) => c.getElement().textContent?.includes(componentName));
}

async function removeSelectedComponent(container: HTMLElement, componentName: string) {
  const table = findSelectionTable(container)!;
  const row = table
    .findRows()
    .map((r) => r.getElement())
    .find((r) => r.textContent?.includes(componentName));
  expect(row).toBeDefined();
  await act(async () => {
    within(row!).getByRole('button', { name: 'Remove' }).click();
  });
}

// ---------------------------------------------------------------------------

describe('CreateDeployment — pre-submit closure validation refusal (2.13, 2.15)', () => {
  it('renders the de-selected finding, its effective outcome and the requiring component', async () => {
    apiMocks.createDeployment.mockRejectedValue(acknowledgementRequiredError());
    const container = await renderRevise();

    await submitDeployment(container);
    await waitFor(() => {
      expect(apiMocks.createDeployment).toHaveBeenCalledTimes(1);
    });

    // The refusal is surfaced as its own alert, not as a bare error string.
    await waitFor(() => {
      expect(
        screen.getByText(
          /Deployment not submitted: de-selected component\(s\) will remain installed/
        )
      ).toBeInTheDocument();
    });
    // The effective outcome (2.13) is stated in full — the component stays
    // installed and is NOT removed by this deployment.
    expect(screen.getByText(EFFECTIVE_OUTCOME)).toBeInTheDocument();
    // The requiring component is named with its version and dependency edge
    // so the intent is resolvable in one step (2.15).
    expect(
      screen.getByText(`${WORKFLOW} v12.0.0 at ">=0.0.0" (HARD)`)
    ).toBeInTheDocument();
    // ...and the group is labelled by what will actually happen.
    expect(
      screen.getByText(/Will NOT be removed from the device \(1\)/)
    ).toBeInTheDocument();

    // The first submit carried no acknowledgement.
    expect(
      apiMocks.createDeployment.mock.calls[0][0].acknowledged_retained_components
    ).toBeUndefined();
  });

  it('acknowledging the named component and re-submitting sends exactly those names (2.14)', async () => {
    apiMocks.createDeployment
      .mockRejectedValueOnce(acknowledgementRequiredError())
      .mockResolvedValue({ deployment_id: 'dep-new' });
    const container = await renderRevise();

    await submitDeployment(container);
    await waitFor(() => {
      expect(screen.getByText(EFFECTIVE_OUTCOME)).toBeInTheDocument();
    });

    // The affordance is SPECIFIC: it names the de-selected component. There
    // is no blanket "proceed anyway" control.
    const checkbox = findAcknowledgementCheckbox(container, RETAINED_MODEL);
    expect(checkbox).toBeDefined();
    expect(screen.queryByText(/proceed anyway/i)).toBeNull();
    await act(async () => {
      checkbox!.findNativeInput().getElement().click();
    });

    await submitDeployment(container);
    await waitFor(() => {
      expect(apiMocks.createDeployment).toHaveBeenCalledTimes(2);
    });
    const payload = apiMocks.createDeployment.mock.calls[1][0];
    expect(payload.acknowledged_retained_components).toEqual([RETAINED_MODEL]);
    // The acknowledgement changes nothing else about the submission (3.14):
    // the component set is exactly the operator's.
    expect(payload.components).toEqual([
      { component_name: WORKFLOW, component_version: '12.0.0' },
      { component_name: 'com.dda.infra', component_version: '1.0.0' },
    ]);
  });

  it('changing the component set clears the acknowledgement so the refusal is re-reported (2.14)', async () => {
    apiMocks.createDeployment.mockRejectedValue(acknowledgementRequiredError());
    const container = await renderRevise();

    await submitDeployment(container);
    await waitFor(() => {
      expect(screen.getByText(EFFECTIVE_OUTCOME)).toBeInTheDocument();
    });
    await act(async () => {
      findAcknowledgementCheckbox(container, RETAINED_MODEL)!
        .findNativeInput()
        .getElement()
        .click();
    });
    expect(
      findAcknowledgementCheckbox(container, RETAINED_MODEL)!
        .findNativeInput()
        .getElement().checked
    ).toBe(true);

    // The operator edits the submission. The finding the backend computes
    // for the new set may differ, so the acknowledgement must not travel
    // with it — it is dropped, visibly.
    //
    // NOTE: the table's Remove button is a Cloudscape Button inside the
    // page's <form>, whose default formAction is 'submit', so this click
    // ALSO fires a submit of the pre-change selection. That is pre-existing
    // page behaviour, unrelated to this change and deliberately left alone;
    // the assertions below are written so it cannot mask the invariant.
    await removeSelectedComponent(container, 'com.dda.infra');
    expect(
      findAcknowledgementCheckbox(container, RETAINED_MODEL)!
        .findNativeInput()
        .getElement().checked
    ).toBe(false);

    const callsBefore = apiMocks.createDeployment.mock.calls.length;
    await submitDeployment(container);
    await waitFor(() => {
      expect(apiMocks.createDeployment.mock.calls.length).toBeGreaterThan(
        callsBefore
      );
    });

    type Payload = {
      components: Array<{ component_name: string; component_version: string }>;
      acknowledged_retained_components?: string[];
    };
    const payloads: Payload[] = apiMocks.createDeployment.mock.calls.map(
      (call) => call[0] as Payload
    );
    const changedSet = payloads.filter((p) => p.components.length === 1);
    expect(changedSet.length).toBeGreaterThan(0);
    // The invariant: NO submission of the CHANGED set ever carries the
    // acknowledgement, so nothing is authorized on the operator's behalf.
    for (const payload of changedSet) {
      expect(payload.components).toEqual([
        { component_name: WORKFLOW, component_version: '12.0.0' },
      ]);
      expect(payload.acknowledged_retained_components).toBeUndefined();
    }
    expect(payloads[payloads.length - 1].components).toHaveLength(1);
    // ...and the refusal is reported again rather than silently cleared.
    await waitFor(() => {
      expect(screen.getByText(EFFECTIVE_OUTCOME)).toBeInTheDocument();
    });
  });

  it('offers no acknowledgement control when a blocking-invalid finding is present (2.16)', async () => {
    apiMocks.createDeployment.mockRejectedValue(validationFailedError());
    const container = await renderRevise();

    await submitDeployment(container);
    await waitFor(() => {
      expect(
        screen.getByText(
          /Deployment not submitted: components cannot be deployed to the target device\(s\)/
        )
      ).toBeInTheDocument();
    });

    // The blocking finding is reported with the component's claimed
    // platforms and the device's reported platform as distinct facts, in
    // the group that can only be resolved by editing the component set.
    expect(
      screen.getByText(/Must be resolved by editing the component set \(1\)/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /dda\.workflow\.8784b33b-25a6-44c3-b62d-d47e8213eabe v1\.0\.0 claims .*variant=arm64_jp5.*jp7-device/
      )
    ).toBeInTheDocument();

    // The acknowledgement-required finding from the SAME single-pass
    // response is still reported with its outcome statement...
    expect(screen.getByText(EFFECTIVE_OUTCOME)).toBeInTheDocument();
    // ...but no acknowledgement can bypass a blocking finding, so no
    // acknowledgement control is offered at all.
    expect(findAcknowledgementCheckbox(container, RETAINED_MODEL)).toBeUndefined();
    expect(
      screen.getByText(/Resolve the blocking finding\(s\) above first/)
    ).toBeInTheDocument();
  });
});
