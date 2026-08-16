/**
 * Component tests for the Create/Revise Deployment screen's
 * Target_Architecture gated-component filtering
 * (device-arch-compatibility task 4.4, Requirements 3.3-3.6, 4.1-4.3).
 *
 * Covers:
 * - an incompatible gated component is excluded from the addable options
 *   and listed in the "Incompatible with the selected device(s)" section
 *   with an explainable reason (Req 3.3, 3.4);
 * - a selected device with no recorded architecture surfaces the warning
 *   alert and hides gated components, failing closed (Req 3.5);
 * - a non-gated component is never hidden by the architecture gate
 *   (Req 3.6);
 * - no device selected / a thing-group target applies no gated filtering
 *   (Req 3.7, 5.4);
 * - revise mode surfaces a now-incompatible pre-loaded component without
 *   dropping it from the selected set (Req 4.1-4.3);
 * - a record published as per-JetPack suffixed vLLM components
 *   (published_component.components) offers only the twin matching the
 *   device's architecture (vllm-multi-arch-publish-conflict Req 2.13,
 *   2.14).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import CreateDeployment from './CreateDeployment';

const { apiMocks, routerState } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listComponents: vi.fn(),
    listDevices: vi.fn(),
    getTargetDeployment: vi.fn(),
    getModel: vi.fn(),
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

// One non-gated component (arch-agnostic), one vLLM gated component whose
// backing record supports only arm64_jp6, and one plugin gated component
// supporting only arm64_jp5.
const PRIVATE_COMPONENTS = [
  {
    arn: 'arn:infra',
    component_name: 'com.dda.infra',
    latest_version: { componentVersion: '1.0.0' },
    description: 'Infra',
    platforms: [],
  },
  {
    arn: 'arn:vllm',
    component_name: 'model-vllm-llama',
    model_name: 'Llama Model',
    training_job_id: 'tj-1',
    latest_version: { componentVersion: '1.0.0' },
    platforms: [],
  },
  {
    arn: 'arn:plugin',
    component_name: 'dda.plugin.edgefilter',
    is_plugin_component: true,
    lifecycle_state: 'prod',
    supported_architectures: ['arm64_jp5'],
    latest_version: { componentVersion: '2.0.0' },
    platforms: [],
  },
];

function device(overrides: Record<string, unknown> = {}) {
  return {
    device_id: 'jp5-device',
    platform: 'linux',
    architecture: 'aarch64',
    target_architecture: 'arm64_jp5',
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
  apiMocks.listComponents.mockImplementation(
    (params: { scope?: string }) =>
      Promise.resolve({
        components: params.scope === 'PUBLIC' ? [] : PRIVATE_COMPONENTS,
      })
  );
  apiMocks.listDevices.mockResolvedValue({ devices: [device()], count: 1 });
  apiMocks.getTargetDeployment.mockResolvedValue({ existing_deployment: null });
  // The backing vLLM record supports only arm64_jp6.
  apiMocks.getModel.mockResolvedValue({
    model: { published_component: { supported_architectures: ['arm64_jp6'] } },
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('CreateDeployment — gated-component architecture filter', () => {
  it('excludes an incompatible gated component and lists it with a reason (Req 3.3, 3.4)', async () => {
    // Select the jp5 device via the URL revise path.
    routerState.search = 'target_device=jp5-device';
    render(<CreateDeployment />);

    // Once the vLLM supported set resolves, the model-vllm component
    // (supports only arm64_jp6) is excluded from the options and moved to
    // the incompatible grouping with an explainable reason.
    await waitFor(() => {
      expect(
        screen.getByText(/Incompatible with the selected device\(s\) \(1\)/)
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(/model-vllm-llama is not supported by/)
    ).toBeInTheDocument();
    // Portal Components retains only the non-gated component (the vLLM one
    // is filtered out); the recommended tab no longer offers the model.
    expect(screen.getByText('Portal Components (1)')).toBeInTheDocument();
    expect(screen.queryByText('Llama Model')).toBeNull();
    // The compatible plugin (supports arm64_jp5) stays in its tab.
    expect(screen.getByText('Node Plugins (1)')).toBeInTheDocument();
  });

  it('warns and hides gated components for a device with no recorded architecture (Req 3.5)', async () => {
    apiMocks.listDevices.mockResolvedValue({
      devices: [device({ target_architecture: null })],
      count: 1,
    });
    routerState.search = 'target_device=jp5-device';
    render(<CreateDeployment />);

    await waitFor(() => {
      expect(
        screen.getByText('Selected device(s) have no recorded architecture')
      ).toBeInTheDocument();
    });
    // The device is named and the gated plugin is hidden (fails closed).
    expect(
      screen.getByText(
        (_c, node) => node?.tagName === 'LI' && node.textContent === 'jp5-device'
      )
    ).toBeInTheDocument();
    expect(screen.getByText('Node Plugins (0)')).toBeInTheDocument();
    expect(
      screen.getByText(/dda\.plugin\.edgefilter is not supported by/)
    ).toBeInTheDocument();
  });

  it('never hides a non-gated component by the architecture gate (Req 3.6)', async () => {
    routerState.search = 'target_device=jp5-device';
    render(<CreateDeployment />);

    // The non-gated com.dda.infra stays available regardless of the gate;
    // only the gated vLLM component is removed once resolution settles.
    await waitFor(() => {
      expect(screen.getByText('Portal Components (1)')).toBeInTheDocument();
    });
    // com.dda.infra is not in the incompatible grouping.
    expect(screen.queryByText(/com\.dda\.infra is not supported/)).toBeNull();
  });

  it('applies no gated filtering for a thing-group target (Req 3.7)', async () => {
    routerState.search = 'target_thing_group=line-a';
    render(<CreateDeployment />);

    // Wait for the catalog to load (plugins tab reflects the full set).
    await waitFor(() => {
      expect(screen.getByText('Node Plugins (1)')).toBeInTheDocument();
    });
    // No device architecture is resolvable for a group, so nothing is
    // hidden and no incompatible grouping is shown.
    expect(
      screen.queryByText(/Incompatible with the selected device/)
    ).toBeNull();
    expect(
      screen.queryByText('Selected device(s) have no recorded architecture')
    ).toBeNull();
  });

  it('applies no gated filtering when no device is selected (Req 5.4)', async () => {
    routerState.search = '';
    render(<CreateDeployment />);

    await waitFor(() => {
      expect(screen.getByText('Node Plugins (1)')).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/Incompatible with the selected device/)
    ).toBeNull();
  });

  it('hides a regular jp5 model component on a jp6 device by name inference (Req 3.3)', async () => {
    // A non-gated model component whose JetPack target is encoded in its
    // name; the backend does not arch-gate it, so only name inference
    // catches the jp5-vs-jp6 mismatch.
    apiMocks.listComponents.mockImplementation((params: { scope?: string }) =>
      Promise.resolve({
        components:
          params.scope === 'PUBLIC'
            ? []
            : [
                {
                  arn: 'arn:model-jp5',
                  component_name: 'model-cookies-binary-jetson-xavier-jp5',
                  model_name: 'cookies-binary',
                  latest_version: { componentVersion: '8.0.0' },
                  platforms: [],
                },
                {
                  arn: 'arn:model-jp6',
                  component_name: 'model-cookies-binary-jetson-xavier-jp6',
                  model_name: 'cookies-binary',
                  latest_version: { componentVersion: '9.0.0' },
                  platforms: [],
                },
              ],
      })
    );
    // jp6 device
    apiMocks.listDevices.mockResolvedValue({
      devices: [device({ target_architecture: 'arm64_jp6' })],
      count: 1,
    });
    routerState.search = 'target_device=jp5-device';
    render(<CreateDeployment />);

    // The jp5 build is excluded and explained; the jp6 build stays offered.
    await waitFor(() => {
      expect(
        screen.getByText(
          /model-cookies-binary-jetson-xavier-jp5 is not supported by/
        )
      ).toBeInTheDocument();
    });
    // Portal Components retains only the jp6 build.
    expect(screen.getByText('Portal Components (1)')).toBeInTheDocument();
    expect(
      screen.queryByText(/model-cookies-binary-jetson-xavier-jp6 is not supported/)
    ).toBeNull();
  });

  it('surfaces a now-incompatible pre-loaded component in revise mode without dropping it (Req 4.1-4.3)', async () => {
    // Revise an existing deployment on the jp5 device whose components
    // include the vLLM component that now supports only arm64_jp6.
    apiMocks.getTargetDeployment.mockResolvedValue({
      existing_deployment: {
        deployment_id: 'dep-1',
        deployment_name: 'Existing',
        target_arn: 'arn:aws:iot:::thing/jp5-device',
        deployment_status: 'ACTIVE',
        revision_id: 'r1',
        creation_timestamp: '2024-01-01T00:00:00Z',
        components: [
          { component_name: 'model-vllm-llama', component_version: '1.0.0' },
        ],
      },
    });
    routerState.search = 'target_device=jp5-device';
    render(<CreateDeployment />);

    // The pre-loaded component is kept in the selected set (revise banner
    // shows) and flagged as incompatible with a reason — not removed. The
    // reason appears both in the in-table warning indicator (revise
    // surfacing) and in the incompatible grouping.
    await waitFor(() => {
      expect(
        screen.getAllByText(/model-vllm-llama is not supported by/).length
      ).toBeGreaterThan(0);
    });
    // The component row (technical name) is still present in the selected
    // components table, i.e. it was not dropped.
    const technicalNameCells = screen.getAllByText('model-vllm-llama');
    expect(technicalNameCells.length).toBeGreaterThan(0);
    // A Remove action is available — the user retains control.
    expect(screen.getAllByText('Remove').length).toBeGreaterThan(0);
  });

  it('offers the suffixed JP7 per-JetPack vLLM component and filters out the JP6 twin on an arm64_jp7 device (Req 2.13, 2.14)', async () => {
    // vllm-multi-arch-publish-conflict: one publish now registers ONE
    // Per_JetPack_Component per packaged target and writes back a
    // `published_component.components` list alongside the record-wide
    // union. The deploy screen must resolve each suffixed component to
    // its OWN architecture via vllmArchsForComponent — not the
    // record-wide union that previously hid nothing (or everything).
    apiMocks.listComponents.mockImplementation((params: { scope?: string }) =>
      Promise.resolve({
        components:
          params.scope === 'PUBLIC'
            ? []
            : [
                {
                  arn: 'arn:vllm-jp6',
                  component_name: 'model-vllm-llama-jetson-xavier-jp6',
                  model_name: 'Llama Model JP6',
                  training_job_id: 'tj-1',
                  latest_version: { componentVersion: '2.0.0' },
                  platforms: [],
                },
                {
                  arn: 'arn:vllm-jp7',
                  component_name: 'model-vllm-llama-jetson-xavier-jp7',
                  model_name: 'Llama Model JP7',
                  training_job_id: 'tj-1',
                  latest_version: { componentVersion: '2.0.0' },
                  platforms: [],
                },
              ],
      })
    );
    // The backing record's publish write-back: the record-wide
    // supported_architectures union kept for legacy readers, plus one
    // `components` entry per Per_JetPack_Component (design step 7).
    apiMocks.getModel.mockResolvedValue({
      model: {
        published_component: {
          component_name: 'model-vllm-llama',
          component_version: '2.0.0',
          supported_architectures: ['arm64_jp6', 'arm64_jp7'],
          components: [
            {
              component_name: 'model-vllm-llama-jetson-xavier-jp6',
              component_version: '2.0.0',
              target: 'jetson-xavier-jp6',
              architecture: 'arm64_jp6',
              supported_architectures: ['arm64_jp6'],
              component_arn:
                'arn:aws:greengrass:us-east-1:123456789012:components:model-vllm-llama-jetson-xavier-jp6:versions:2.0.0',
            },
            {
              component_name: 'model-vllm-llama-jetson-xavier-jp7',
              component_version: '2.0.0',
              target: 'jetson-xavier-jp7',
              architecture: 'arm64_jp7',
              supported_architectures: ['arm64_jp7'],
              component_arn:
                'arn:aws:greengrass:us-east-1:123456789012:components:model-vllm-llama-jetson-xavier-jp7:versions:2.0.0',
            },
          ],
        },
      },
    });
    // An arm64_jp7 device (e.g. jetson-thor1).
    apiMocks.listDevices.mockResolvedValue({
      devices: [
        device({ device_id: 'jp7-device', target_architecture: 'arm64_jp7' }),
      ],
      count: 1,
    });
    routerState.search = 'target_device=jp7-device';
    render(<CreateDeployment />);

    // The JP6 twin resolves to its own ['arm64_jp6'] (rule 1: matching
    // components[] entry) and is filtered out with a reason.
    await waitFor(() => {
      expect(
        screen.getByText(
          /model-vllm-llama-jetson-xavier-jp6 is not supported by/
        )
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(/Incompatible with the selected device\(s\) \(1\)/)
    ).toBeInTheDocument();
    // The JP7 twin resolves to ['arm64_jp7'] and stays offered — it is
    // NOT in the incompatible grouping and its model remains available.
    expect(
      screen.queryByText(/model-vllm-llama-jetson-xavier-jp7 is not supported/)
    ).toBeNull();
    expect(screen.getByText('Portal Components (1)')).toBeInTheDocument();
    expect(screen.getAllByText('Llama Model JP7').length).toBeGreaterThan(0);
    expect(screen.queryByText('Llama Model JP6')).toBeNull();
  });
});
