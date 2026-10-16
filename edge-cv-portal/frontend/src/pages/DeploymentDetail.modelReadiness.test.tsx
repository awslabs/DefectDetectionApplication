/**
 * DeploymentDetail: the "Models not ready on the device (current state)"
 * warning (spec triton-model-cleanup-wedge, task 6.3, D11; Requirement 2.10).
 *
 * The backend adds `model_readiness` to the latest deployment of a target
 * when its devices report, in their dda-model-status shadow, a deployment
 * model that is not READY or self-heal activity. The page renders it as a
 * Cloudscape warning Alert labeled as current device state; without the
 * field nothing is rendered.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import DeploymentDetail from './DeploymentDetail';

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    getDeployment: vi.fn(),
    cancelDeployment: vi.fn(),
  },
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService };
});

vi.mock('react-router-dom', () => ({
  useParams: () => ({ deploymentId: 'dep-latest' }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams('usecase_id=uc-1'), vi.fn()],
}));

vi.mock('../components/LogsDiagnosticsTab', () => ({
  default: () => null,
}));

const HEADER = 'Models not ready on the device (current state)';
const fmt = (ts: string) => new Date(ts).toLocaleString();

function deployment(overrides: Record<string, unknown> = {}) {
  return {
    deployment_id: 'dep-latest',
    deployment_name: 'line-a',
    target_arn: 'arn:aws:iot:::thinggroup/line-a',
    revision_id: '3',
    deployment_status: 'ACTIVE',
    iot_job_id: 'job-1',
    iot_job_arn: '',
    is_latest_for_target: true,
    creation_timestamp: '2026-10-16T08:00:00Z',
    components: [
      { component_name: 'aws.edgeml.dda.LocalServer.arm64JP5', component_version: '1.0.55', configuration_update: {} },
      { component_name: 'model-a', component_version: '8.0.0', configuration_update: {} },
    ],
    deployment_policies: {},
    tags: {},
    usecase_id: 'uc-1',
    effective_deployments: [],
    error_messages: [],
    ...overrides,
  };
}

const MODEL_READINESS = {
  warning: 'Current device state: 1 model(s) of this deployment are not READY on 1 device(s)',
  devices: [
    {
      core_device: 'line-a-camera-01',
      shadow_updated_at: '2026-10-16T10:00:00Z',
      not_ready: [
        {
          model: 'model-a',
          status: 'LOADING',
          since: '2026-10-16T09:00:00Z',
          stalled: true,
          reason: 'no load progress',
        },
      ],
      self_heal: {
        state: 'exhausted',
        enabled: true,
        attempts_in_24h: 3,
        last_requested_at: '2026-10-16T08:30:00Z',
        rearms_at: '2026-10-17T08:30:00Z',
      },
    },
  ],
  checked_devices: 1,
  truncated: false,
};

async function renderPage(dep: Record<string, unknown>) {
  apiMocks.getDeployment.mockResolvedValue({ deployment: dep });
  const { container } = render(<DeploymentDetail />);
  const wrapper = createWrapper(container);
  await waitFor(() => expect(wrapper.findHeader()).not.toBeNull());
  return wrapper;
}

function readinessAlert(wrapper: ReturnType<typeof createWrapper>) {
  return wrapper
    .findAllAlerts()
    .find((alert) => alert.findHeader()?.getElement().textContent === HEADER);
}

beforeEach(() => {
  apiMocks.getDeployment.mockReset();
});

describe('DeploymentDetail model readiness warning', () => {
  it('renders the current-state warning with one line per device and model and a self-heal line', async () => {
    const wrapper = await renderPage(deployment({ model_readiness: MODEL_READINESS }));

    const alert = readinessAlert(wrapper);
    expect(alert).toBeDefined();
    const text = alert!.findContent().getElement().textContent ?? '';

    expect(text).toContain(MODEL_READINESS.warning);
    expect(text).toContain('Device: line-a-camera-01');
    expect(text).toContain(`Current device state, shadow updated ${fmt('2026-10-16T10:00:00Z')}`);
    expect(text).toContain(
      `model-a: LOADING since ${fmt('2026-10-16T09:00:00Z')}, stalled (no load progress)`,
    );
    expect(text).toContain(
      `Self-heal: exhausted, enabled, 3 attempt(s) in 24 h, last requested ${fmt(
        '2026-10-16T08:30:00Z',
      )}, re-arms ${fmt('2026-10-17T08:30:00Z')}`,
    );
    expect(text).not.toContain('were not checked');
  });

  it('states when not every device was checked', async () => {
    const wrapper = await renderPage(
      deployment({ model_readiness: { ...MODEL_READINESS, checked_devices: 20, truncated: true } }),
    );
    const text = readinessAlert(wrapper)!.findContent().getElement().textContent ?? '';
    expect(text).toContain('Checked 20 device(s); other devices');
    expect(text).toContain('were not checked.');
  });

  it('renders nothing without the field', async () => {
    const wrapper = await renderPage(deployment());
    expect(readinessAlert(wrapper)).toBeUndefined();
    expect(wrapper.getElement().textContent).not.toContain('current state');
  });
});
