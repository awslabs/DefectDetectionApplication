/**
 * Component tests for PortScanPanel (port-guidance-and-pad-prepopulation
 * task 8.2, Requirements 6.1, 6.2, 6.3, 6.4, 6.6, 6.7, 6.10, 7.1, 7.2,
 * 7.3, 7.5, 7.6).
 *
 * Panel-level behavior against a lightweight harness holding the port
 * lists: auto-scan once over the Untouched_Defaults and never over
 * edited lists (6.1), the manual scan button with its disabled-while-
 * loading non-concurrency (6.3, 6.7), the outcome summary with applied
 * names per side, already-declared names, Unconfirmed_Suggestion caps +
 * confirmation guidance, and Unmapped_Pad caveats (6.2, 6.4, 6.10),
 * factory selection via preferredFactory (6.6), and each degraded state
 * rendered beside a still-usable manual flow (7.1–7.3, 7.5, 7.6).
 *
 * Wizard-level wiring (unconfirmed badge lifecycle, removal blocking,
 * step navigation) is covered in RegistrationWizard.test.tsx.
 */
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import PortScanPanel, { PortScanApplyResult } from './PortScanPanel';
import type { PortForm } from './declaration';
import type { GstPropertiesResponse, ScanElement } from './scan';
import type { PortSuggestion, UnmappedPad } from './portScan';

const { getGstProperties } = vi.hoisted(() => ({
  getGstProperties: vi.fn(),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: { getGstProperties },
}));

// ------------------------------------------------------------- fixtures

const VIDEO_IN: PortSuggestion = {
  name: 'video_in',
  direction: 'input',
  portType: 'VideoFrames',
  confident: true,
  caps: 'video/x-raw, format=(string)RGB',
  capsTruncated: false,
  reason: "the pad's caps begin with video/x-raw",
};

const VIDEO_OUT: PortSuggestion = {
  name: 'video_out',
  direction: 'output',
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

const REQUEST_PAD: UnmappedPad = {
  name: 'src_%u',
  direction: 'src',
  presence: 'request',
  caveat:
    'request pads are created at runtime and do not correspond to fixed declared Ports',
};

const AVAILABLE: GstPropertiesResponse = {
  available: true,
  gstVersion: '1.20.3',
  capturedAt: '2026-02-14T12:00:00Z',
  elements: [
    {
      factory: 'my_filter',
      suggestions: [],
      skipped: [],
      portSuggestions: [VIDEO_IN, VIDEO_OUT, META_OUT],
      unmappedPads: [REQUEST_PAD],
      padsReason: null,
      padsMessage: null,
    },
  ],
};

const MULTI: GstPropertiesResponse = {
  available: true,
  gstVersion: '1.20.3',
  capturedAt: '2026-02-14T12:00:00Z',
  elements: [
    {
      factory: 'alpha',
      suggestions: [],
      skipped: [],
      portSuggestions: [{ ...VIDEO_IN, name: 'alpha_in' }],
      unmappedPads: [],
      padsReason: null,
      padsMessage: null,
    },
    {
      factory: 'beta',
      suggestions: [],
      skipped: [],
      portSuggestions: [{ ...VIDEO_IN, name: 'beta_in' }, VIDEO_OUT],
      unmappedPads: [],
      padsReason: null,
      padsMessage: null,
    },
  ],
};

/** An available response whose sole element has the given pad fields. */
const elementResponse = (
  fields: Partial<ScanElement>
): GstPropertiesResponse => ({
  available: true,
  gstVersion: '1.20.3',
  capturedAt: '2026-02-14T12:00:00Z',
  elements: [
    {
      factory: 'my_filter',
      suggestions: [],
      skipped: [],
      ...fields,
    },
  ],
});

const DEFAULT_INPUTS: PortForm[] = [{ name: 'in', portType: 'VideoFrames' }];
const DEFAULT_OUTPUTS: PortForm[] = [{ name: 'out', portType: 'VideoFrames' }];

/** Minimal stand-in for the wizard's port lists + onApply wiring, with a
 *  manual add control proving the manual flow stays usable (7.6). */
function Harness({
  initialInputs = DEFAULT_INPUTS,
  initialOutputs = DEFAULT_OUTPUTS,
  preferredFactory = 'my_filter',
}: {
  initialInputs?: PortForm[];
  initialOutputs?: PortForm[];
  preferredFactory?: string;
}) {
  const [inputs, setInputs] = useState<PortForm[]>(initialInputs);
  const [outputs, setOutputs] = useState<PortForm[]>(initialOutputs);
  return (
    <>
      <PortScanPanel
        pluginId="p-1"
        version={3}
        preferredFactory={preferredFactory}
        inputs={inputs}
        outputs={outputs}
        onApply={(result: PortScanApplyResult) => {
          setInputs(result.inputs);
          setOutputs(result.outputs);
        }}
      />
      <button
        data-testid="manual-add-input"
        onClick={() =>
          setInputs((rows) => [...rows, { name: 'manual', portType: 'VideoFrames' }])
        }
      >
        add manual input
      </button>
      <ul data-testid="input-rows">
        {inputs.map((port, index) => (
          <li key={`${port.name}-${index}`}>{`${port.name}:${port.portType}`}</li>
        ))}
      </ul>
      <ul data-testid="output-rows">
        {outputs.map((port, index) => (
          <li key={`${port.name}-${index}`}>{`${port.name}:${port.portType}`}</li>
        ))}
      </ul>
    </>
  );
}

const rowNames = (side: 'input-rows' | 'output-rows') =>
  within(screen.getByTestId(side))
    .queryAllByRole('listitem')
    .map((item) => item.textContent);

const scanButton = () =>
  screen.getByRole('button', { name: 'Scan plugin pads' });

beforeEach(() => {
  vi.clearAllMocks();
});

describe('PortScanPanel', () => {
  it('auto-scans once over the untouched defaults and replaces them with the suggestions (6.1)', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    render(<Harness />);

    const outcome = await screen.findByTestId('port-scan-outcome');
    expect(getGstProperties).toHaveBeenCalledTimes(1);
    expect(getGstProperties).toHaveBeenCalledWith('p-1', 3);

    // The defaults are replaced by the suggestions partitioned by side.
    expect(rowNames('input-rows')).toEqual(['video_in:VideoFrames']);
    expect(rowNames('output-rows')).toEqual([
      'video_out:VideoFrames',
      'meta_out:VideoFrames',
    ]);
    expect(outcome.textContent).toContain('Applied 3 ports from');
    expect(outcome.textContent).toContain('my_filter');
    expect(outcome.textContent).toContain('Inputs: video_in');
    expect(outcome.textContent).toContain('Outputs: video_out, meta_out');
  });

  it('does not auto-apply when the port lists were edited (6.1)', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    render(
      <Harness
        initialInputs={[{ name: 'custom', portType: 'VideoFrames' }]}
      />
    );

    // Wait until the fetch is fully processed (capture footer renders).
    await screen.findByText(/Captured 2026-02-14T12:00:00Z/);
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();
    expect(rowNames('input-rows')).toEqual(['custom:VideoFrames']);
    expect(rowNames('output-rows')).toEqual(['out:VideoFrames']);
  });

  it('merges on demand via the scan button, keeping edits and reporting already-declared names (6.2, 6.3, 6.11)', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    render(
      <Harness
        initialInputs={[{ name: 'video_in', portType: 'InferenceMeta' }]}
      />
    );

    await screen.findByText(/Captured 2026-02-14T12:00:00Z/);
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();

    fireEvent.click(scanButton());

    const outcome = await screen.findByTestId('port-scan-outcome');
    // Rescan re-fetches the report (mount + manual).
    expect(getGstProperties).toHaveBeenCalledTimes(2);
    // The edited row is kept unchanged; only new names are appended.
    expect(rowNames('input-rows')).toEqual(['video_in:InferenceMeta']);
    expect(rowNames('output-rows')).toEqual([
      'out:VideoFrames',
      'video_out:VideoFrames',
      'meta_out:VideoFrames',
    ]);
    expect(outcome.textContent).toContain(
      '1 already declared (kept as declared): video_in'
    );
    expect(outcome.textContent).toContain('Outputs: video_out, meta_out');
  });

  it('disables the scan control while a scan is in progress and keeps the manual flow usable (6.7)', async () => {
    let resolveFetch: (value: GstPropertiesResponse) => void = () => {};
    getGstProperties.mockImplementation(
      () =>
        new Promise<GstPropertiesResponse>((resolve) => {
          resolveFetch = resolve;
        })
    );
    render(<Harness />);

    // Clicking during the in-flight mount fetch starts no second scan.
    fireEvent.click(scanButton());
    expect(getGstProperties).toHaveBeenCalledTimes(1);

    // The manual port controls stay usable while the scan is loading.
    fireEvent.click(screen.getByTestId('manual-add-input'));
    expect(rowNames('input-rows')).toEqual([
      'in:VideoFrames',
      'manual:VideoFrames',
    ]);

    resolveFetch(AVAILABLE);
    await screen.findByText(/Captured 2026-02-14T12:00:00Z/);

    // The lists were edited mid-scan, so the auto-apply was skipped;
    // once idle, the manual scan control works again.
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();
    fireEvent.click(scanButton());
    expect(getGstProperties).toHaveBeenCalledTimes(2);
    resolveFetch(AVAILABLE);
    await screen.findByTestId('port-scan-outcome');
  });

  it('renders unconfirmed suggestions with caps + guidance and unmapped pads with caveats (6.4)', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    render(<Harness />);

    const outcome = await screen.findByTestId('port-scan-outcome');
    // Unconfirmed_Suggestion: name, caps string, confirmation guidance.
    expect(outcome.textContent).toContain(
      'meta_out needs port type confirmation'
    );
    expect(outcome.textContent).toContain('application/x-dda-meta');
    expect(outcome.textContent).toContain('Confirm the port type');
    // Unmapped_Pad: name, direction, presence, caveat.
    expect(outcome.textContent).toContain('Not added: src_%u (src, request)');
    expect(outcome.textContent).toContain(REQUEST_PAD.caveat);
  });

  it('pre-picks the preferred factory on multi-element reports and scans it (6.6)', async () => {
    getGstProperties.mockResolvedValue(MULTI);
    render(<Harness preferredFactory="beta" />);

    const outcome = await screen.findByTestId('port-scan-outcome');
    const select = screen.getByTestId('port-scan-factory-select');
    expect(within(select).getByText('beta')).toBeInTheDocument();
    expect(outcome.textContent).toContain('beta');
    expect(rowNames('input-rows')).toEqual(['beta_in:VideoFrames']);
    expect(rowNames('output-rows')).toEqual(['video_out:VideoFrames']);
  });

  it('shows the informational build-first notice for no_x86_64_build beside a usable manual flow (7.1, 7.6)', async () => {
    getGstProperties.mockResolvedValue({
      available: false,
      reason: 'no_x86_64_build',
    });
    render(<Harness />);

    const alert = await screen.findByTestId('port-scan-unavailable-alert');
    expect(alert.textContent).toContain('requires a successful x86_64 build');
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();
    // Lists unchanged, manual controls and rescan still usable.
    expect(rowNames('input-rows')).toEqual(['in:VideoFrames']);
    fireEvent.click(screen.getByTestId('manual-add-input'));
    expect(rowNames('input-rows')).toEqual([
      'in:VideoFrames',
      'manual:VideoFrames',
    ]);
    expect(scanButton()).toBeEnabled();
  });

  it('shows the informational pad-data notice when the report predates pad capture (7.2, 7.6)', async () => {
    getGstProperties.mockResolvedValue(
      elementResponse({ padsReason: 'pads_not_captured' })
    );
    render(<Harness />);

    const alert = await screen.findByTestId('port-scan-pads-alert');
    expect(alert.textContent).toContain('predates pad capture');
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();
    expect(rowNames('input-rows')).toEqual(['in:VideoFrames']);
    expect(rowNames('output-rows')).toEqual(['out:VideoFrames']);
    expect(scanButton()).toBeEnabled();
  });

  it('shows the introspection failure with its diagnostic and keeps the retry control (7.3)', async () => {
    getGstProperties.mockResolvedValue({
      available: false,
      reason: 'introspection_failed',
      message: 'no element factories registered',
    });
    render(<Harness />);

    const alert = await screen.findByTestId('port-scan-unavailable-alert');
    expect(alert.textContent).toContain('Introspection failed');
    expect(alert.textContent).toContain('no element factories registered');
    expect(rowNames('input-rows')).toEqual(['in:VideoFrames']);
    expect(scanButton()).toBeEnabled();
  });

  it('shows the request error and retries via the scan button (7.3)', async () => {
    getGstProperties.mockRejectedValue(new Error('network exploded'));
    render(<Harness />);

    const alert = await screen.findByTestId('port-scan-error-alert');
    expect(alert.textContent).toContain('network exploded');
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();
    expect(rowNames('input-rows')).toEqual(['in:VideoFrames']);
    expect(scanButton()).toBeEnabled();

    // Retrying after the failure applies over the still-untouched lists.
    getGstProperties.mockResolvedValue(AVAILABLE);
    fireEvent.click(scanButton());
    await screen.findByTestId('port-scan-outcome');
    expect(screen.queryByTestId('port-scan-error-alert')).toBeNull();
    expect(rowNames('input-rows')).toEqual(['video_in:VideoFrames']);
    expect(rowNames('output-rows')).toEqual([
      'video_out:VideoFrames',
      'meta_out:VideoFrames',
    ]);
  });

  it('shows the no-pad-templates notice when the element declares none (7.5, 7.6)', async () => {
    getGstProperties.mockResolvedValue(
      elementResponse({ padsReason: 'no_pad_templates' })
    );
    render(<Harness />);

    const alert = await screen.findByTestId('port-scan-pads-alert');
    expect(alert.textContent).toContain('declares no static pad templates');
    expect(screen.queryByTestId('port-scan-outcome')).toBeNull();
    expect(rowNames('input-rows')).toEqual(['in:VideoFrames']);
    expect(rowNames('output-rows')).toEqual(['out:VideoFrames']);
    expect(scanButton()).toBeEnabled();
  });

  it('reports a zero-suggestion scan with unmapped pads and leaves the lists unchanged (6.10, 7.5)', async () => {
    getGstProperties.mockResolvedValue(
      elementResponse({
        portSuggestions: [],
        unmappedPads: [REQUEST_PAD],
        padsReason: null,
        padsMessage: null,
      })
    );
    render(<Harness />);

    const alert = await screen.findByTestId('port-scan-pads-alert');
    expect(alert.textContent).toContain('no always-present pads');
    // The Unmapped_Pads still surface with their caveats.
    expect(alert.textContent).toContain('src_%u');
    expect(alert.textContent).toContain(REQUEST_PAD.caveat);
    expect(rowNames('input-rows')).toEqual(['in:VideoFrames']);
    expect(rowNames('output-rows')).toEqual(['out:VideoFrames']);
    expect(scanButton()).toBeEnabled();
  });

  it('shows the empty-report notice when the plugin registered no elements', async () => {
    getGstProperties.mockResolvedValue({
      available: true,
      gstVersion: '1.20.3',
      capturedAt: '2026-02-14T12:00:00Z',
      elements: [],
    });
    render(<Harness />);

    const alert = await screen.findByTestId('port-scan-empty-alert');
    expect(alert.textContent).toContain('registered no elements');
    expect(rowNames('input-rows')).toEqual(['in:VideoFrames']);
    expect(rowNames('output-rows')).toEqual(['out:VideoFrames']);
  });
});
