/**
 * Component tests for the registration wizard's parameter scan wiring
 * (gst-parameter-prepopulation task 7.4, Requirements 5.1, 5.5, 6.4,
 * 7.1, 7.2, 7.3, 7.4, 3.3).
 *
 * The Parameters step embeds ParameterScanPanel with the plugin
 * context: an auto-scan populates the empty list through the ordinary
 * patch({parameters}) path (5.1), scanned rows carry the "from scan"
 * badge until edited (6.4), and an edited scanned row behaves exactly
 * like a manual row (3.3). Every degraded scan state is additive: the
 * Add-parameter flow stays usable and step navigation is never blocked
 * (5.5, 7.1–7.4).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import RegistrationWizard from './RegistrationWizard';
import type { GstPropertiesResponse } from './scan';

const {
  navigateMock,
  listUseCases,
  getPlugin,
  listNodeTypes,
  getGstProperties,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listUseCases: vi.fn(),
  getPlugin: vi.fn(),
  listNodeTypes: vi.fn(),
  getGstProperties: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useParams: () => ({ pluginId: 'p-1' }),
  useSearchParams: () => [new URLSearchParams()],
}));

vi.mock('../../services/api', () => {
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
  return { ApiError, apiService: { listUseCases } };
});

vi.mock('./api', () => ({
  nodeDesignerApi: {
    getPlugin,
    getVersion: vi.fn(),
    listNodeTypes,
    getNodeType: vi.fn(),
    registerNodeType: vi.fn(),
    updateNodeType: vi.fn(),
    getGstProperties,
  },
}));

// ------------------------------------------------------------- fixtures

const PLUGIN = {
  plugin_id: 'p-1',
  version: 3,
  usecase_id: 'uc-1',
  name: 'My Blur',
  description: '',
  kind: 'scaffold',
  deepstream: false,
  provenance: {},
  lifecycle_state: 'dev',
  review: { decision: 'pending' },
  artifacts: { x86_64: { buildStatus: 'succeeded', s3Key: 'k' } },
  component: {},
  source_s3_prefix: 'plugin-sources/uc-1/p-1/3/',
  created_by: 'user',
  created_at: 1,
  updated_at: 1,
};

const AVAILABLE: GstPropertiesResponse = {
  available: true,
  gstVersion: '1.20.3',
  capturedAt: '2026-02-14T12:00:00Z',
  elements: [
    {
      // Matches defaultElementFactory('My Blur').
      factory: 'my_blur',
      suggestions: [
        {
          name: 'radius',
          paramType: 'int',
          required: false,
          default: 5,
          constraints: { min: 0, max: 100 },
          description: 'Blur radius in pixels',
          examples: [5],
        },
        {
          name: 'mode',
          paramType: 'enum',
          required: false,
          default: 'gaussian',
          constraints: { values: ['gaussian', 'box'] },
          description: 'Blur mode',
          examples: ['gaussian'],
        },
      ],
      skipped: [],
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
  getPlugin.mockResolvedValue({ plugin: PLUGIN, versions: [] });
  listNodeTypes.mockResolvedValue({ nodeTypes: [], count: 0 });
});

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
const clickPrevious = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Previous' }));

/** Render the wizard and walk to the Parameters step (details, ports pre-filled). */
async function renderToParametersStep() {
  render(<RegistrationWizard />);
  await screen.findByText('Register custom node type');
  clickNext(); // Node details -> Ports
  await screen.findByText('Input ports');
  clickNext(); // Ports -> Parameters
  await screen.findByRole('button', { name: 'Add parameter' });
}

describe('RegistrationWizard parameter scan wiring', () => {
  it('auto-scans the empty list into badged rows and clears the badge when a row is edited (5.1, 6.4, 3.3)', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    await renderToParametersStep();

    // Auto-scan populated both rows through the ordinary patch path.
    await screen.findByTestId('scan-outcome');
    expect(getGstProperties).toHaveBeenCalledWith('p-1', 3);
    expect(screen.getByDisplayValue('radius')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Blur mode')).toBeInTheDocument();

    // Both scanned rows carry the "from scan" badge (6.4).
    expect(screen.getAllByText('from scan')).toHaveLength(2);

    // Editing a scanned row works exactly like a manual row (3.3) and
    // clears its scan provenance (6.4).
    fireEvent.change(screen.getByDisplayValue('Blur radius in pixels'), {
      target: { value: 'Radius in pixels' },
    });
    expect(screen.getByDisplayValue('Radius in pixels')).toBeInTheDocument();
    expect(screen.getAllByText('from scan')).toHaveLength(1);

    // The untouched row keeps its badge until it is edited too.
    fireEvent.change(screen.getByDisplayValue('mode'), {
      target: { value: 'blur_mode' },
    });
    expect(screen.getByDisplayValue('blur_mode')).toBeInTheDocument();
    expect(screen.queryByText('from scan')).toBeNull();
  });

  // Every degraded scan state must leave the manual flow usable and
  // step navigation unblocked (5.5, 7.1–7.4).
  const degradedCases: Array<{
    title: string;
    testId: string;
    expectText: string | RegExp;
    setup: () => void;
  }> = [
    {
      title: 'no x86_64 build (7.1)',
      testId: 'scan-unavailable-alert',
      expectText: /requires a successful x86_64 build/,
      setup: () =>
        getGstProperties.mockResolvedValue({
          available: false,
          reason: 'no_x86_64_build',
        }),
    },
    {
      title: 'introspection not captured (7.4)',
      testId: 'scan-unavailable-alert',
      expectText: /predates property capture/,
      setup: () =>
        getGstProperties.mockResolvedValue({
          available: false,
          reason: 'not_captured',
        }),
    },
    {
      title: 'introspection failed with diagnostic (7.2)',
      testId: 'scan-unavailable-alert',
      expectText: /no element factories registered/,
      setup: () =>
        getGstProperties.mockResolvedValue({
          available: false,
          reason: 'introspection_failed',
          message: 'no element factories registered',
        }),
    },
    {
      title: 'scan request rejection (7.3)',
      testId: 'scan-error-alert',
      expectText: /gateway timed out/,
      setup: () =>
        getGstProperties.mockRejectedValue(new Error('gateway timed out')),
    },
  ];

  for (const degraded of degradedCases) {
    it(`keeps Add parameter and step navigation working when scanning degrades: ${degraded.title} (5.5)`, async () => {
      degraded.setup();
      await renderToParametersStep();

      const alert = await screen.findByTestId(degraded.testId);
      expect(alert.textContent).toMatch(degraded.expectText);
      expect(screen.queryByTestId('scan-outcome')).toBeNull();

      // Step navigation is not blocked by the degraded scan (5.5).
      clickNext();
      await screen.findByText('Declare a mapping for this architecture');
      clickPrevious();
      await screen.findByRole('button', { name: 'Add parameter' });

      // The manual Add-parameter flow stays fully usable.
      fireEvent.click(screen.getByRole('button', { name: 'Add parameter' }));
      expect(screen.getByText('Parameter 1')).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Remove parameter 1' })
      ).toBeInTheDocument();
    });
  }
});
