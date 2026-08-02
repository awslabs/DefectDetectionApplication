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
 *
 * Ports-step wiring (port-guidance-and-pad-prepopulation task 8.5,
 * Requirements 1.5, 6.5, 6.7, 6.8, 6.9, 7.6): the Port_Guidance panel
 * renders on the Ports step beside the scan panel and the manual port
 * controls (1.5, 7.6), unconfirmed applied ports carry the "confirm
 * type" badge until a name or type edit — including re-selecting the
 * same type — confirms them (6.5), applied ports are edited and
 * removed through the ordinary row controls (6.8), update mode blocks
 * removing ports the registered declaration depends on with the reason
 * displayed (6.9), and step navigation is never blocked during or
 * after a scan (6.7).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import RegistrationWizard from './RegistrationWizard';
import type { GstPropertiesResponse } from './scan';
import type { PortSuggestion } from './portScan';
import type { NodeTypeDetail } from './types';

const {
  navigateMock,
  listUseCases,
  getPlugin,
  listNodeTypes,
  getNodeType,
  getGstProperties,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listUseCases: vi.fn(),
  getPlugin: vi.fn(),
  listNodeTypes: vi.fn(),
  getNodeType: vi.fn(),
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
    getNodeType,
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

// Port-scan fixtures (task 8.5): one confident input suggestion and one
// unconfirmed output suggestion derived for the wizard's preferred
// factory (defaultElementFactory('My Blur') === 'my_blur').

const VIDEO_IN: PortSuggestion = {
  name: 'video_in',
  direction: 'input',
  portType: 'VideoFrames',
  confident: true,
  caps: 'video/x-raw, format=(string)RGB',
  capsTruncated: false,
  reason: "the pad's caps begin with video/x-raw",
};

const META_OUT: PortSuggestion = {
  name: 'meta_out',
  direction: 'output',
  portType: 'VideoFrames',
  confident: false,
  caps: 'application/x-dda-meta',
  capsTruncated: false,
  reason:
    'InferenceMeta and EventSignal are DDA semantic concepts GStreamer ' +
    'caps cannot express; confirm the port type.',
};

const PORTS_AVAILABLE: GstPropertiesResponse = {
  available: true,
  gstVersion: '1.20.3',
  capturedAt: '2026-02-14T12:00:00Z',
  elements: [
    {
      factory: 'my_blur',
      suggestions: [],
      skipped: [],
      portSuggestions: [VIDEO_IN, META_OUT],
      unmappedPads: [],
      padsReason: null,
      padsMessage: null,
    },
  ],
};

/** A report predating pad capture: available, but nothing to apply. */
const PADS_NOT_CAPTURED: GstPropertiesResponse = {
  available: true,
  gstVersion: '1.20.3',
  capturedAt: '2026-02-14T12:00:00Z',
  elements: [
    {
      factory: 'my_blur',
      suggestions: [],
      skipped: [],
      padsReason: 'pads_not_captured',
    },
  ],
};

// Update-mode fixtures (6.9): the plugin already backs a registered
// Custom_Node_Type whose declaration depends on frames_in/frames_out.

const EXISTING: NodeTypeDetail = {
  node_type_id: 'custom.my_blur',
  version: 2,
  usecase_id: 'uc-1',
  usecase_ids: ['uc-1'],
  plugin_id: 'p-1',
  plugin_version: 2,
  declaration: {
    typeId: 'custom.my_blur',
    displayName: 'My Blur',
    category: 'preprocessing',
    inputs: [{ name: 'frames_in', portType: 'VideoFrames' }],
    outputs: [{ name: 'frames_out', portType: 'VideoFrames' }],
    parameters: [],
    mappings: [
      {
        arch: 'x86_64',
        elementChain: [{ factory: 'my_blur', argsTemplate: {} }],
        pluginDependencies: [],
      },
    ],
    hardwareDependent: false,
  },
  deprecated: false,
  created_by: 'user',
  created_at: 1,
  updated_at: 1,
};

const EXISTING_SUMMARY = {
  node_type_id: EXISTING.node_type_id,
  version: EXISTING.version,
  usecase_id: 'uc-1',
  plugin_id: 'p-1',
  plugin_version: 2,
  display_name: 'My Blur',
  category: 'preprocessing',
  deprecated: false,
  updated_at: 1,
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

/** Render the wizard and walk to the Ports step. */
async function renderToPortsStep(headerText = 'Register custom node type') {
  const view = render(<RegistrationWizard />);
  await screen.findByText(headerText);
  clickNext(); // Node details -> Ports
  await screen.findByText('Input ports');
  return view;
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

describe('RegistrationWizard ports step wiring', () => {
  it('renders the Port_Guidance beside the scan panel and the manual port controls on the Ports step (1.5, 7.6)', async () => {
    getGstProperties.mockResolvedValue(PORTS_AVAILABLE);
    await renderToPortsStep();

    // The shared static guidance content (identical to the Create
    // wizard's, Requirement 1.5): definition + connection rule, the
    // input/output distinction, and the three Port_Type descriptions.
    expect(screen.getByTestId('port-guidance-panel')).toBeInTheDocument();
    expect(
      screen.getByTestId('port-guidance-definition').textContent
    ).toContain('A port is one declared connection point');
    expect(
      screen.getByTestId('port-guidance-distinction').textContent
    ).toContain('Input ports receive data from an upstream node');
    for (const portType of ['VideoFrames', 'InferenceMeta', 'EventSignal']) {
      expect(
        screen.getByTestId(`port-guidance-type-${portType}`)
      ).toBeInTheDocument();
    }

    // The scan panel and the manual port controls render beside — never
    // instead of — the guidance (7.6).
    expect(screen.getByTestId('port-scan-panel')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Add input port' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Add output port' })
    ).toBeInTheDocument();
  });

  it('marks unconfirmed applied ports with the confirm-type badge and clears it on a name edit (6.5, 6.8)', async () => {
    getGstProperties.mockResolvedValue(PORTS_AVAILABLE);
    await renderToPortsStep();
    await screen.findByTestId('port-scan-outcome');

    // Auto-apply replaced the untouched defaults through the ordinary
    // patch path: the applied rows are ordinary, editable port rows.
    expect(screen.getByDisplayValue('video_in')).toBeInTheDocument();
    expect(screen.getByDisplayValue('meta_out')).toBeInTheDocument();

    // Only the unconfirmed applied port carries the badge and its
    // inline confirmation guidance (6.5).
    expect(screen.getAllByText('confirm type')).toHaveLength(1);
    expect(
      screen.getByText(/Confirm it by re-selecting the type/)
    ).toBeInTheDocument();

    // Editing the port's name confirms it: the badge disappears and the
    // row keeps behaving exactly like a manual row (6.5, 6.8).
    fireEvent.change(screen.getByDisplayValue('meta_out'), {
      target: { value: 'meta' },
    });
    expect(screen.getByDisplayValue('meta')).toBeInTheDocument();
    expect(screen.queryByText('confirm type')).toBeNull();
  });

  it('confirms an unconfirmed port when its type is re-selected, keeping the type unchanged (6.5)', async () => {
    getGstProperties.mockResolvedValue(PORTS_AVAILABLE);
    const { container } = await renderToPortsStep();
    await screen.findByTestId('port-scan-outcome');
    expect(screen.getAllByText('confirm type')).toHaveLength(1);

    // Port-type selects on the step: [0] video_in (input side),
    // [1] meta_out (output side).
    const typeSelects = createWrapper(container).findAllSelects();
    expect(typeSelects).toHaveLength(2);
    const metaOutType = typeSelects[1];
    expect(metaOutType.findTrigger().getElement().textContent).toContain(
      'VideoFrames'
    );

    // Re-selecting the same type is the confirmation gesture (6.5).
    metaOutType.openDropdown();
    metaOutType.selectOptionByValue('VideoFrames');

    expect(screen.queryByText('confirm type')).toBeNull();
    expect(screen.getByDisplayValue('meta_out')).toBeInTheDocument();
    expect(metaOutType.findTrigger().getElement().textContent).toContain(
      'VideoFrames'
    );
  });

  it('leaves applied ports editable and removable through the ordinary row controls (6.8)', async () => {
    getGstProperties.mockResolvedValue(PORTS_AVAILABLE);
    await renderToPortsStep();
    await screen.findByTestId('port-scan-outcome');

    // Editing an applied port works through the ordinary name input.
    fireEvent.change(screen.getByDisplayValue('video_in'), {
      target: { value: 'frames' },
    });
    expect(screen.getByDisplayValue('frames')).toBeInTheDocument();

    // Removing an applied port works through the ordinary remove
    // control (no update mode: nothing blocks removal) and clears the
    // removed port's unconfirmed badge.
    const removeOutput = screen.getByRole('button', {
      name: 'Remove outputs port 1',
    });
    expect(removeOutput).toBeEnabled();
    fireEvent.click(removeOutput);
    expect(screen.queryByDisplayValue('meta_out')).toBeNull();
    expect(screen.queryByText('confirm type')).toBeNull();
    expect(screen.getByText('No outputs declared.')).toBeInTheDocument();
  });

  it('blocks removing a port the registered declaration depends on and displays the reason in update mode (6.9)', async () => {
    listNodeTypes.mockResolvedValue({ nodeTypes: [EXISTING_SUMMARY], count: 1 });
    getNodeType.mockResolvedValue({ nodeType: EXISTING });
    getGstProperties.mockResolvedValue(PADS_NOT_CAPTURED);
    await renderToPortsStep('Update custom node type');

    // The registered declaration's ports pre-fill the lists.
    expect(screen.getByDisplayValue('frames_in')).toBeInTheDocument();
    expect(screen.getByDisplayValue('frames_out')).toBeInTheDocument();

    // Both declaration-dependent ports are protected: the remove
    // control is disabled and the reason is displayed (6.9).
    expect(
      screen.getByRole('button', { name: 'Remove inputs port 1' })
    ).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Remove outputs port 1' })
    ).toBeDisabled();
    expect(
      screen.getByText(/declares the input port "frames_in"/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/declares the output port "frames_out"/)
    ).toBeInTheDocument();

    // A newly added port carries no protection and stays removable
    // through the ordinary control.
    fireEvent.click(screen.getByRole('button', { name: 'Add input port' }));
    const addedRemove = screen.getByRole('button', {
      name: 'Remove inputs port 2',
    });
    expect(addedRemove).toBeEnabled();
    fireEvent.click(addedRemove);
    expect(
      screen.queryByRole('button', { name: 'Remove inputs port 2' })
    ).toBeNull();
  });

  it('keeps the manual flow and step navigation unblocked while a scan is in progress and after it completes (6.7)', async () => {
    const resolvers: Array<(value: GstPropertiesResponse) => void> = [];
    getGstProperties.mockImplementation(
      () =>
        new Promise<GstPropertiesResponse>((resolve) => {
          resolvers.push(resolve);
        })
    );
    await renderToPortsStep();

    // The mount scan is in flight: the scan control is disabled so no
    // second scan starts concurrently, while the manual port controls
    // stay usable (6.7).
    expect(
      screen.getByRole('button', { name: 'Scan plugin pads' })
    ).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Add input port' }));
    const added = screen
      .getAllByPlaceholderText('in')
      .find((element) => (element as HTMLInputElement).value === '')!;
    fireEvent.change(added, { target: { value: 'extra_in' } });
    expect(screen.getByDisplayValue('extra_in')).toBeInTheDocument();

    // Step navigation is never blocked on the in-flight scan (6.7).
    clickNext();
    await screen.findByRole('button', { name: 'Add parameter' });
    clickPrevious();
    await screen.findByText('Input ports');

    // Completing the scan (over the now-edited lists: nothing is
    // auto-applied) leaves the scan control and navigation working.
    resolvers.forEach((resolve) => resolve(PORTS_AVAILABLE));
    await screen.findByText(/Captured 2026-02-14T12:00:00Z/);
    expect(
      screen.getByRole('button', { name: 'Scan plugin pads' })
    ).toBeEnabled();
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();
    clickNext();
    await screen.findByRole('button', { name: 'Add parameter' });
  });

  it('keeps the guidance, the manual flow, and navigation when the scan request fails (7.6)', async () => {
    getGstProperties.mockRejectedValue(new Error('gateway timed out'));
    await renderToPortsStep();

    const alert = await screen.findByTestId('port-scan-error-alert');
    expect(alert.textContent).toContain('gateway timed out');

    // The guidance still renders and the lists are unchanged.
    expect(screen.getByTestId('port-guidance-panel')).toBeInTheDocument();
    expect(screen.getByDisplayValue('in')).toBeInTheDocument();
    expect(screen.getByDisplayValue('out')).toBeInTheDocument();

    // The manual flow stays usable and navigation is not blocked (7.6).
    fireEvent.change(screen.getByDisplayValue('in'), {
      target: { value: 'frames_in' },
    });
    expect(screen.getByDisplayValue('frames_in')).toBeInTheDocument();
    clickNext();
    await screen.findByRole('button', { name: 'Add parameter' });
  });
});

/**
 * Category-driven default ports (workflow-designer-bugfixes Bug 2,
 * Requirements 1.4, 2.4, 2.5, 2.6).
 *
 * BUG CONDITION EXPLORATION (task 4): these tests encode the EXPECTED
 * behavior and are expected to FAIL on the unfixed code — the wizard
 * seeds one "in" input and one "out" output regardless of the selected
 * palette category and never rewrites the untouched default rows on a
 * category change (isBugCondition2 in the workflow-designer-bugfixes
 * design). They validate the fix when they pass.
 */
describe('RegistrationWizard category-driven default ports (workflow-designer-bugfixes Bug 2)', () => {
  it('presents no input rows and one VideoFrames output after selecting the input category on untouched defaults (1.4, 2.4, 2.5, 2.6)', async () => {
    // A report predating pad capture: the scan applies nothing, so the
    // port rows stay the wizard-seeded Untouched_Defaults.
    getGstProperties.mockResolvedValue(PADS_NOT_CAPTURED);
    const { container } = render(<RegistrationWizard />);
    await screen.findByText('Register custom node type');

    // The details step's only Select is the palette category.
    const categorySelect = createWrapper(container).findAllSelects()[0];
    categorySelect.openDropdown();
    categorySelect.selectOptionByValue('input');
    clickNext();
    await screen.findByText('Input ports');

    // Input (source) nodes: no input port rows (2.4).
    expect(screen.getByText('No inputs declared.')).toBeInTheDocument();
    expect(container.querySelectorAll('input[placeholder="in"]')).toHaveLength(0);

    // Exactly one VideoFrames output with a non-empty name (2.4).
    const outputNames = container.querySelectorAll('input[placeholder="out"]');
    expect(outputNames).toHaveLength(1);
    expect((outputNames[0] as HTMLInputElement).value.trim()).not.toBe('');
    const typeSelects = createWrapper(container)
      .findAllSelects()
      .filter(
        (select) =>
          select.getElement().getAttribute('data-testid') !==
          'port-scan-factory-select'
      );
    expect(typeSelects).toHaveLength(1);
    expect(typeSelects[0].findTrigger().getElement().textContent).toContain(
      'VideoFrames'
    );

    // The ports step states the category's input/output requirements
    // (2.6) — the design's fix interface: PortGuidancePanel renders them
    // under data-testid="port-guidance-requirements".
    const requirements = screen.getByTestId('port-guidance-requirements');
    expect(requirements.textContent).toMatch(/inputs/i);
    expect(requirements.textContent).toMatch(/outputs/i);
  });
});
