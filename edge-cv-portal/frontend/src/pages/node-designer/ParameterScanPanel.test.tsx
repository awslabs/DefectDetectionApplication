/**
 * Component tests for ParameterScanPanel (gst-parameter-prepopulation
 * task 7.4, Requirements 5.1, 5.2, 5.3, 5.4, 6.3, 7.1, 7.2, 7.3, 7.4,
 * 2.5).
 *
 * Panel-level behavior against a lightweight harness holding the
 * parameter rows: auto-scan on an empty list (5.1), no auto-merge when
 * rows already exist, manual rescan (5.2), the factory selector on
 * multi-element reports pre-picked via pickElement (5.4), the scan
 * outcome summary with added/alreadyDeclared/skipped (5.3, 6.3, 2.5),
 * and one notice per degraded reason (7.1–7.4).
 *
 * Wizard-level wiring (badge lifecycle, Add-parameter usability, step
 * navigation) is covered in RegistrationWizard.test.tsx and
 * CreateWizard.test.tsx.
 */
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import ParameterScanPanel, {
  ParameterScanMergeResult,
} from './ParameterScanPanel';
import type { ParameterForm } from './declaration';
import type { GstPropertiesResponse } from './scan';

const { getGstProperties } = vi.hoisted(() => ({
  getGstProperties: vi.fn(),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: { getGstProperties },
}));

// ------------------------------------------------------------- fixtures

const RADIUS = {
  name: 'radius',
  paramType: 'int',
  required: false,
  default: 5,
  constraints: { min: 0, max: 100 },
  description: 'Blur radius in pixels',
  examples: [5],
};

const MODE = {
  name: 'mode',
  paramType: 'enum',
  required: false,
  default: 'gaussian',
  constraints: { values: ['gaussian', 'box'] },
  description: 'Blur mode',
  examples: ['gaussian'],
};

const AVAILABLE: GstPropertiesResponse = {
  available: true,
  gstVersion: '1.20.3',
  capturedAt: '2026-02-14T12:00:00Z',
  elements: [
    {
      factory: 'my_blur',
      suggestions: [RADIUS, MODE],
      skipped: [{ name: 'caps', reason: 'unsupported GType GstCaps' }],
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
      suggestions: [
        {
          name: 'alpha_only',
          paramType: 'bool',
          required: false,
          default: true,
          description: 'Alpha element property',
          examples: [true],
        },
      ],
      skipped: [],
    },
    { factory: 'beta', suggestions: [RADIUS], skipped: [] },
  ],
};

const manualRow = (name: string): ParameterForm => ({
  name,
  paramType: 'string',
  required: false,
  defaultValue: '',
  description: 'manually declared',
  example: 'x',
  enumValues: '',
});

/** Minimal stand-in for the wizard's parameters state + onMerge wiring. */
function Harness({
  initial = [],
  preferredFactory = 'my_blur',
}: {
  initial?: ParameterForm[];
  preferredFactory?: string;
}) {
  const [parameters, setParameters] = useState<ParameterForm[]>(initial);
  return (
    <>
      <ParameterScanPanel
        pluginId="p-1"
        version={3}
        preferredFactory={preferredFactory}
        parameters={parameters}
        onMerge={(result: ParameterScanMergeResult) =>
          setParameters(result.parameters)
        }
      />
      <ul data-testid="rows">
        {parameters.map((parameter) => (
          <li key={parameter.name} data-testid={`row-${parameter.name}`}>
            {parameter.name}:{parameter.paramType}
          </li>
        ))}
      </ul>
    </>
  );
}

const rowNames = () =>
  within(screen.getByTestId('rows'))
    .queryAllByRole('listitem')
    .map((item) => item.textContent);

const scanButton = () =>
  screen.getByRole('button', { name: 'Scan plugin properties' });

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ParameterScanPanel', () => {
  it('auto-scans an empty parameter list on mount and shows the outcome with skipped reasons (5.1, 5.3, 2.5)', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    render(<Harness />);

    const outcome = await screen.findByTestId('scan-outcome');
    expect(getGstProperties).toHaveBeenCalledWith('p-1', 3);
    expect(rowNames()).toEqual(['radius:int', 'mode:enum']);

    // Outcome summary: added count, factory, skipped with reasons.
    expect(outcome.textContent).toContain('Added 2 parameters from');
    expect(outcome.textContent).toContain('my_blur');
    expect(outcome.textContent).toContain('radius, mode');
    expect(outcome.textContent).toContain(
      '1 skipped: caps (unsupported GType GstCaps)'
    );
  });

  it('does not auto-merge when parameter rows already exist', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    render(<Harness initial={[manualRow('threshold')]} />);

    // Wait for the fetch to be fully processed (capture footer renders).
    await screen.findByText(/Captured 2026-02-14T12:00:00Z/);
    expect(screen.queryByTestId('scan-outcome')).toBeNull();
    expect(rowNames()).toEqual(['threshold:string']);
  });

  it('merges on demand via the scan button and reports already-declared names (5.2, 6.3)', async () => {
    getGstProperties.mockResolvedValue(AVAILABLE);
    render(<Harness initial={[manualRow('radius')]} />);

    await screen.findByText(/Captured 2026-02-14T12:00:00Z/);
    expect(screen.queryByTestId('scan-outcome')).toBeNull();

    fireEvent.click(scanButton());

    const outcome = await screen.findByTestId('scan-outcome');
    // Rescan re-fetches the report (mount + manual).
    expect(getGstProperties).toHaveBeenCalledTimes(2);
    // The manual row is kept as declared; only the new name is appended.
    expect(rowNames()).toEqual(['radius:string', 'mode:enum']);
    expect(outcome.textContent).toContain('Added 1 parameter from');
    expect(outcome.textContent).toContain(
      '1 already declared (kept as declared): radius'
    );
  });

  it('renders the factory selector on multi-element reports pre-picked via the preferred factory (5.4)', async () => {
    getGstProperties.mockResolvedValue(MULTI);
    render(<Harness preferredFactory="beta" />);

    const outcome = await screen.findByTestId('scan-outcome');
    const select = screen.getByTestId('scan-factory-select');
    // Pre-picked to the wizard's factory, and the auto-merge used it.
    expect(within(select).getByText('beta')).toBeInTheDocument();
    expect(outcome.textContent).toContain('beta');
    expect(rowNames()).toEqual(['radius:int']);
  });

  it('shows the informational build-first notice for no_x86_64_build (7.1)', async () => {
    getGstProperties.mockResolvedValue({
      available: false,
      reason: 'no_x86_64_build',
    });
    render(<Harness />);

    const alert = await screen.findByTestId('scan-unavailable-alert');
    expect(alert.textContent).toContain(
      'requires a successful x86_64 build'
    );
    expect(screen.queryByTestId('scan-outcome')).toBeNull();
    // The manual retry stays available.
    expect(scanButton()).toBeEnabled();
  });

  it('shows the informational not-captured notice for builds predating capture (7.4)', async () => {
    getGstProperties.mockResolvedValue({
      available: false,
      reason: 'not_captured',
    });
    render(<Harness />);

    const alert = await screen.findByTestId('scan-unavailable-alert');
    expect(alert.textContent).toContain('predates property capture');
    expect(alert.textContent).toContain('Rebuild the plugin');
    expect(scanButton()).toBeEnabled();
  });

  it('shows the introspection failure with its diagnostic message (7.2)', async () => {
    getGstProperties.mockResolvedValue({
      available: false,
      reason: 'introspection_failed',
      message: 'no element factories registered',
    });
    render(<Harness />);

    const alert = await screen.findByTestId('scan-unavailable-alert');
    expect(alert.textContent).toContain('Property introspection failed');
    expect(alert.textContent).toContain('no element factories registered');
    expect(scanButton()).toBeEnabled();
  });

  it('shows the request error when the scan fetch rejects (7.3)', async () => {
    getGstProperties.mockRejectedValue(new Error('network exploded'));
    render(<Harness />);

    const alert = await screen.findByTestId('scan-error-alert');
    expect(alert.textContent).toContain('network exploded');
    expect(screen.queryByTestId('scan-outcome')).toBeNull();
    expect(scanButton()).toBeEnabled();

    // Retrying after the failure works (5.2, 7.3).
    getGstProperties.mockResolvedValue(AVAILABLE);
    fireEvent.click(scanButton());
    await screen.findByTestId('scan-outcome');
    expect(screen.queryByTestId('scan-error-alert')).toBeNull();
    expect(rowNames()).toEqual(['radius:int', 'mode:enum']);
  });
});
