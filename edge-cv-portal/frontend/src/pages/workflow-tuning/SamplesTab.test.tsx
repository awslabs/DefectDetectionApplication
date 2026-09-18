/**
 * Unit tests for the Samples tab of the Tuning_Session workspace
 * (quality-prompt-tuning, task 8.6 — Requirements 4.1, 4.2, 4.3, 4.4, 4.7).
 *
 * The pair card's fields (both presigned images or the single-image
 * indication, the recorded verdict and confidence, the raw answer on demand,
 * device, execution, version, detection slot, source, Label, and the
 * duplicate / synthetic / different-prompt badges — Requirement 4.1); the
 * per-card and multi-selection Label writes and the clear, each with the
 * exact request body (Requirements 4.2, 4.3); the eight filters reaching the
 * route as query parameters, and only when set (Requirement 4.4); and the
 * counts panel with its zero-OK / zero-NOK warning plus the
 * Synthetic_Negatives toggle (Requirements 4.5, 4.7).
 *
 * No AWS and no network: `apiService` is mocked.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import SamplesTab, { UNLABELLED_TEXT, ZERO_CLASS_WARNING } from './SamplesTab';
import type {
  LabelCounts,
  ListTuningSamplesResponse,
  TuningSampleView,
} from './types';

const { listTuningSamples, setTuningSampleLabels, setTuningSyntheticNegatives } =
  vi.hoisted(() => ({
    listTuningSamples: vi.fn(),
    setTuningSampleLabels: vi.fn(),
    setTuningSyntheticNegatives: vi.fn(),
  }));

vi.mock('../../services/api', () => ({
  apiService: {
    listTuningSamples,
    setTuningSampleLabels,
    setTuningSyntheticNegatives,
  },
}));

// ------------------------------------------------------------------ fixtures

function counts(patch: Partial<LabelCounts> = {}): LabelCounts {
  return {
    OK: 3,
    NOK: 2,
    EXCLUDE: 1,
    unlabelled: 4,
    synthetic: 0,
    duplicates: 1,
    total: 10,
    ...patch,
  };
}

function sample(patch: Partial<TuningSampleView> = {}): TuningSampleView {
  return {
    sampleId: 's-1',
    workflowId: 'wf-1',
    nodeId: 'bedrock_1',
    nodeType: 'bedrock_inference',
    thingName: 'edge-01',
    executionId: 'exec-1',
    version: 3,
    exportedAt: 1_700_000_000,
    source: 'live',
    label: null,
    duplicateOf: null,
    differentPrompt: false,
    synthetic: false,
    sourceSampleId: null,
    siblingNodeId: null,
    detectionId: null,
    detectionSlot: 2,
    promptFingerprint: 'fp-1',
    recorded: {
      isAnomalous: true,
      confidence: 0.87,
      answer: '{"is_anomalous": true, "confidence": 0.87}',
      parseError: null,
    },
    input: { key: 'a/input.jpg', url: 'https://example.invalid/input.jpg' },
    reference: {
      key: 'a/reference.jpg',
      url: 'https://example.invalid/reference.jpg',
    },
    singleImage: false,
    ...patch,
  };
}

function page(
  samples: TuningSampleView[],
  patch: Partial<ListTuningSamplesResponse> = {}
): ListTuningSamplesResponse {
  return {
    sessionId: 'ts-1',
    samples,
    count: samples.length,
    matched: samples.length,
    nextCursor: null,
    expiresInSeconds: 900,
    labelCounts: counts(),
    ...patch,
  };
}

async function renderTab(
  samples: TuningSampleView[] = [sample()],
  options: { labelCounts?: LabelCounts; syntheticEnabled?: boolean } = {}
) {
  listTuningSamples.mockResolvedValue(page(samples));
  const handlers = {
    onCountsChange: vi.fn(),
    onDevicesSeen: vi.fn(),
    onSyntheticChange: vi.fn(),
  };
  render(
    <SamplesTab
      sessionId="ts-1"
      syntheticEnabled={options.syntheticEnabled ?? false}
      labelCounts={options.labelCounts ?? counts()}
      onCountsChange={(next: LabelCounts) => handlers.onCountsChange(next)}
      onDevicesSeen={(devices: string[]) => handlers.onDevicesSeen(devices)}
      onSyntheticChange={(enabled: boolean) => handlers.onSyntheticChange(enabled)}
    />
  );
  await waitFor(() => expect(listTuningSamples).toHaveBeenCalled());
  if (samples.length) {
    await waitFor(() =>
      expect(screen.queryByTestId(`sample-card-${samples[0].sampleId}`)).toBeTruthy()
    );
  }
  return handlers;
}

/** The query object of the most recent `listTuningSamples` call. */
function lastQuery(): Record<string, unknown> {
  const calls = listTuningSamples.mock.calls;
  return calls[calls.length - 1][1] as Record<string, unknown>;
}

/** Pick a filter's option by value on the Cloudscape select. */
function chooseFilter(testId: string, value: string) {
  const root = screen.getByTestId(testId);
  const select = createWrapper(root.parentElement as HTMLElement).findSelect()!;
  select.openDropdown();
  select.selectOptionByValue(value);
}

beforeEach(() => {
  vi.clearAllMocks();
  setTuningSampleLabels.mockImplementation(async (_id: string, body: unknown) => ({
    sessionId: 'ts-1',
    label: (body as { label: string | null }).label,
    updated: (body as { sampleIds: string[] }).sampleIds,
    missing: [],
    labelCounts: counts({ OK: 4 }),
  }));
  setTuningSyntheticNegatives.mockImplementation(
    async (_id: string, enabled: boolean) => ({
      sessionId: 'ts-1',
      enabled,
      created: enabled ? 3 : 0,
      removed: enabled ? 0 : 3,
      labelCounts: counts({ synthetic: enabled ? 3 : 0 }),
    })
  );
});

afterEach(() => {
  cleanup();
});

// ------------------------------------------------------------- Requirement 4.1

describe('Requirement 4.1: the pair card', () => {
  it('shows both presigned images', async () => {
    await renderTab();
    const card = screen.getByTestId('sample-card-s-1');
    const images = within(card).getAllByRole('img');
    expect(images.map((image) => image.getAttribute('src'))).toEqual([
      'https://example.invalid/input.jpg',
      'https://example.invalid/reference.jpg',
    ]);
    expect(within(card).getByText('Input')).toBeTruthy();
    expect(within(card).getByText('Reference')).toBeTruthy();
  });

  it('indicates a single-image inspection instead of a reference image', async () => {
    await renderTab([sample({ singleImage: true, reference: null })]);
    expect(screen.getByTestId('single-image-s-1').textContent).toContain(
      'Single-image inspection'
    );
    expect(within(screen.getByTestId('sample-card-s-1')).getAllByRole('img')).toHaveLength(
      1
    );
  });

  it('renders a placeholder for an image whose presigned URL is missing', async () => {
    await renderTab([sample({ input: { key: 'a/input.jpg', url: null } })]);
    expect(screen.getByText('Image unavailable')).toBeTruthy();
  });

  it('shows the recorded verdict, confidence, device, execution, version, slot and source', async () => {
    await renderTab();
    const card = screen.getByTestId('sample-card-s-1');
    expect(within(card).getByText('Anomalous')).toBeTruthy();
    expect(within(card).getByText('0.87')).toBeTruthy();
    expect(within(card).getByText('edge-01')).toBeTruthy();
    expect(within(card).getAllByText('exec-1').length).toBeGreaterThan(0);
    expect(within(card).getByText('3')).toBeTruthy();
    expect(within(card).getByText('live')).toBeTruthy();
    expect(within(card).getByText('Detection slot')).toBeTruthy();
    expect(within(card).getByText('2')).toBeTruthy();
  });

  it('names the parser reason when the deployed node recorded no verdict', async () => {
    await renderTab([
      sample({
        recorded: {
          isAnomalous: null,
          confidence: null,
          answer: 'I am not sure.',
          parseError: 'no JSON object found',
        },
      }),
    ]);
    const card = screen.getByTestId('sample-card-s-1');
    expect(within(card).getByText('No verdict (parser: no JSON object found)')).toBeTruthy();
    expect(within(card).getByText('—')).toBeTruthy();
  });

  it('carries the raw answer verbatim', async () => {
    await renderTab();
    expect(screen.getByTestId('raw-answer-s-1').textContent).toBe(
      '{"is_anomalous": true, "confidence": 0.87}'
    );
  });

  it('badges duplicates, synthetic negatives and a different deployed prompt', async () => {
    await renderTab([
      sample({
        sampleId: 's-dup',
        duplicateOf: 's-1',
        synthetic: true,
        differentPrompt: true,
      }),
    ]);
    const card = screen.getByTestId('sample-card-s-dup');
    expect(within(card).getByText('Duplicate')).toBeTruthy();
    expect(within(card).getByText('Synthetic')).toBeTruthy();
    expect(within(card).getByText('Different prompt')).toBeTruthy();
  });

  it('carries no badge for a plain live sample', async () => {
    await renderTab();
    const card = screen.getByTestId('sample-card-s-1');
    expect(within(card).queryByText('Duplicate')).toBeNull();
    expect(within(card).queryByText('Synthetic')).toBeNull();
    expect(within(card).queryByText('Different prompt')).toBeNull();
  });

  it('reports the devices that exported the samples for the VLM picker', async () => {
    const handlers = await renderTab([
      sample({ sampleId: 's-1', thingName: 'edge-02' }),
      sample({ sampleId: 's-2', thingName: 'edge-01' }),
      sample({ sampleId: 's-3', thingName: 'edge-01' }),
    ]);
    await waitFor(() =>
      expect(handlers.onDevicesSeen).toHaveBeenCalledWith(['edge-01', 'edge-02'])
    );
  });
});

// -------------------------------------------------------- Requirements 4.2/4.3

describe('Requirements 4.2, 4.3: labelling', () => {
  it('says an unlabelled sample is excluded from scoring', async () => {
    await renderTab();
    expect(screen.getByText(UNLABELLED_TEXT)).toBeTruthy();
  });

  it('persists a per-card OK / NOK / EXCLUDE immediately', async () => {
    await renderTab();
    const card = screen.getByTestId('sample-card-s-1');
    for (const label of ['OK', 'NOK', 'EXCLUDE'] as const) {
      setTuningSampleLabels.mockClear();
      fireEvent.click(within(card).getByRole('button', { name: label }));
      await waitFor(() =>
        expect(setTuningSampleLabels).toHaveBeenCalledWith('ts-1', {
          sampleIds: ['s-1'],
          label,
        })
      );
    }
  });

  it('clears a Label with label: null', async () => {
    await renderTab([sample({ label: 'OK' })]);
    fireEvent.click(
      within(screen.getByTestId('sample-card-s-1')).getByRole('button', {
        name: 'Clear',
      })
    );
    await waitFor(() =>
      expect(setTuningSampleLabels).toHaveBeenCalledWith('ts-1', {
        sampleIds: ['s-1'],
        label: null,
      })
    );
  });

  it('shows the persisted Label on the card and hands the fresh counts up', async () => {
    const handlers = await renderTab();
    fireEvent.click(
      within(screen.getByTestId('sample-card-s-1')).getByRole('button', {
        name: 'NOK',
      })
    );
    await waitFor(() => expect(screen.getByText('Label: NOK')).toBeTruthy());
    expect(handlers.onCountsChange).toHaveBeenCalledWith(counts({ OK: 4 }));
  });

  it('labels a multi-selection in one request', async () => {
    await renderTab([
      sample({ sampleId: 's-1' }),
      sample({ sampleId: 's-2' }),
      sample({ sampleId: 's-3' }),
    ]);
    fireEvent.click(screen.getByLabelText('Select sample s-1'));
    fireEvent.click(screen.getByLabelText('Select sample s-3'));
    fireEvent.click(screen.getByRole('button', { name: 'Label 2 NOK' }));
    await waitFor(() =>
      expect(setTuningSampleLabels).toHaveBeenCalledWith('ts-1', {
        sampleIds: ['s-1', 's-3'],
        label: 'NOK',
      })
    );
  });

  it('selects every shown sample and clears the selection', async () => {
    await renderTab([sample({ sampleId: 's-1' }), sample({ sampleId: 's-2' })]);
    fireEvent.click(screen.getByRole('button', { name: 'Select all shown' }));
    expect(screen.getByRole('button', { name: 'Label 2 OK' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    expect(screen.getByRole('button', { name: 'Label 0 OK' })).toBeTruthy();
  });

  it('surfaces a failed label write', async () => {
    await renderTab();
    setTuningSampleLabels.mockRejectedValue(new Error('label write refused'));
    fireEvent.click(
      within(screen.getByTestId('sample-card-s-1')).getByRole('button', {
        name: 'OK',
      })
    );
    await waitFor(() => expect(screen.getByText(/label write refused/)).toBeTruthy());
  });
});

// ------------------------------------------------------------- Requirement 4.4

describe('Requirement 4.4: the filters', () => {
  it('sends no filter until one is set', async () => {
    await renderTab();
    expect(lastQuery()).toEqual({ limit: 24 });
  });

  it('sends the Label filter, "Unlabelled" included', async () => {
    await renderTab();
    chooseFilter('filter-label', 'NOK');
    await waitFor(() => expect(lastQuery().label).toBe('NOK'));
    chooseFilter('filter-label', 'UNLABELLED');
    await waitFor(() => expect(lastQuery().label).toBe('UNLABELLED'));
  });

  it('sends the recorded-verdict filter', async () => {
    await renderTab();
    chooseFilter('filter-verdict', 'anomalous');
    await waitFor(() => expect(lastQuery().verdict).toBe('anomalous'));
  });

  it('sends the source filter', async () => {
    await renderTab();
    chooseFilter('filter-source', 'backfill');
    await waitFor(() => expect(lastQuery().source).toBe('backfill'));
  });

  it('sends the duplicate, different-prompt and disagreement flags as booleans', async () => {
    await renderTab();
    chooseFilter('filter-duplicates', 'false');
    await waitFor(() => expect(lastQuery().duplicates).toBe(false));
    chooseFilter('filter-different-prompt', 'true');
    await waitFor(() => expect(lastQuery().differentPrompt).toBe(true));
    chooseFilter('filter-disagree', 'true');
    await waitFor(() => expect(lastQuery().disagree).toBe(true));
  });

  it('sends the device and version filters trimmed, and drops them when blank', async () => {
    await renderTab();
    const device = within(screen.getByTestId('filter-device')).getByRole('textbox');
    fireEvent.change(device, { target: { value: '  edge-01 ' } });
    await waitFor(() => expect(lastQuery().device).toBe('edge-01'));
    const version = within(screen.getByTestId('filter-version')).getByRole('textbox');
    fireEvent.change(version, { target: { value: '4' } });
    await waitFor(() => expect(lastQuery().version).toBe('4'));
    fireEvent.change(device, { target: { value: '   ' } });
    await waitFor(() => expect(lastQuery().device).toBeUndefined());
  });

  it('states that nothing matches when a filter empties the page', async () => {
    await renderTab();
    listTuningSamples.mockResolvedValue(page([], { matched: 0 }));
    chooseFilter('filter-label', 'EXCLUDE');
    await waitFor(() => expect(screen.getByTestId('no-samples')).toBeTruthy());
  });
});

// -------------------------------------------------------- Requirements 4.5/4.7

describe('Requirements 4.5, 4.7: counts, the zero-class warning and synthetics', () => {
  it('shows every count the route reports', async () => {
    await renderTab();
    expect(screen.getByTestId('count-ok').textContent).toBe('3');
    expect(screen.getByTestId('count-nok').textContent).toBe('2');
    expect(screen.getByTestId('count-exclude').textContent).toBe('1');
    expect(screen.getByTestId('count-unlabelled').textContent).toBe('4');
    expect(screen.getByTestId('count-synthetic').textContent).toBe('0');
    expect(screen.getByTestId('count-total').textContent).toBe('10');
  });

  it('warns while no sample is labelled OK', async () => {
    await renderTab([sample()], { labelCounts: counts({ OK: 0 }) });
    expect(screen.getByTestId('zero-class-warning').textContent).toBe(
      ZERO_CLASS_WARNING
    );
  });

  it('warns while no sample is labelled NOK', async () => {
    await renderTab([sample()], { labelCounts: counts({ NOK: 0 }) });
    expect(screen.getByTestId('zero-class-warning')).toBeTruthy();
  });

  it('does not warn once both classes are present', async () => {
    await renderTab();
    expect(screen.queryByTestId('zero-class-warning')).toBeNull();
  });

  it('creates and removes the synthetic negatives through the toggle', async () => {
    const handlers = await renderTab();
    const toggle = within(screen.getByTestId('synthetic-toggle')).getByRole(
      'checkbox'
    );
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(setTuningSyntheticNegatives).toHaveBeenCalledWith('ts-1', true)
    );
    await waitFor(() => expect(handlers.onSyntheticChange).toHaveBeenCalledWith(true));
    // Enabling re-reads the page so the new samples appear.
    expect(listTuningSamples.mock.calls.length).toBeGreaterThan(1);

    cleanup();
    const second = await renderTab([sample()], { syntheticEnabled: true });
    fireEvent.click(
      within(screen.getByTestId('synthetic-toggle')).getByRole('checkbox')
    );
    await waitFor(() =>
      expect(setTuningSyntheticNegatives).toHaveBeenLastCalledWith('ts-1', false)
    );
    await waitFor(() => expect(second.onSyntheticChange).toHaveBeenCalledWith(false));
  });
});
