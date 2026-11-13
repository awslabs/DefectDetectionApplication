/**
 * The Samples tab for an inspection-format node (AC-9 and AC-17 frontend
 * halves).
 *
 * - An inspection-format sample's verdict is shown as the node published
 *   it, "Pass" or "Fail". A verdict the Portal recovered from a backfilled
 *   sample's recorded answer carries the badge "Recovered from the recorded
 *   answer".
 * - The session page passes `syntheticUnsupportedReason` for an
 *   inspection-format node: the Synthetic negatives toggle is then disabled
 *   and shows the reason.
 * - Legacy samples and the toggle without a reason render as today.
 *
 * At c299a6e1 a recovered pass reads "Normal" where "Pass" and the badge
 * are expected, and the toggle stays enabled.
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
import SamplesTab from './SamplesTab';
import type { LabelCounts, TuningSampleView } from './types';

const { listTuningSamples, setTuningSampleLabels, setTuningSyntheticNegatives } =
  vi.hoisted(() => ({
    listTuningSamples: vi.fn(),
    setTuningSampleLabels: vi.fn(),
    setTuningSyntheticNegatives: vi.fn(),
  }));

vi.mock('../../services/api', () => ({
  apiService: { listTuningSamples, setTuningSampleLabels, setTuningSyntheticNegatives },
}));

const REASON =
  'Not available for inspection-format nodes: their reference image (for '
  + 'example a legend) is not a per-part golden sample, so cross-paired '
  + 'images are not defects.';

const PASS_ANSWER =
  '{"verdict": "pass", "confidence": 0.93, "reason": "all parts present", "objects": []}';

const COUNTS: LabelCounts = {
  OK: 1, NOK: 1, EXCLUDE: 0, unlabelled: 3, synthetic: 0, duplicates: 0, total: 5,
};

function sample(patch: Partial<TuningSampleView> = {}): TuningSampleView {
  return {
    sampleId: 's-1',
    workflowId: 'wf-1',
    nodeId: 'check',
    nodeType: 'bedrock_inference',
    thingName: 'edge-01',
    executionId: 'exec-1',
    version: 7,
    exportedAt: 1_700_000_000,
    source: 'backfill',
    label: null,
    duplicateOf: null,
    differentPrompt: false,
    synthetic: false,
    sourceSampleId: null,
    siblingNodeId: null,
    detectionId: null,
    detectionSlot: null,
    promptFingerprint: 'fp-1',
    recorded: {
      isAnomalous: false,
      confidence: 0.93,
      answer: PASS_ANSWER,
      parseError: null,
      verdict: 'pass',
      recovered: true,
    },
    input: { key: 'a/input.jpg', url: 'https://example.invalid/input.jpg' },
    reference: null,
    singleImage: true,
    ...patch,
  };
}

async function renderTab(
  samples: TuningSampleView[],
  syntheticUnsupportedReason?: string
) {
  listTuningSamples.mockResolvedValue({
    sessionId: 'ts-1',
    samples,
    count: samples.length,
    matched: samples.length,
    nextCursor: null,
    expiresInSeconds: 1800,
    labelCounts: COUNTS,
  });
  render(
    <SamplesTab
      sessionId="ts-1"
      syntheticEnabled={false}
      labelCounts={COUNTS}
      onCountsChange={vi.fn()}
      onDevicesSeen={vi.fn()}
      onSyntheticChange={vi.fn()}
      syntheticUnsupportedReason={syntheticUnsupportedReason}
    />
  );
  await waitFor(() => expect(listTuningSamples).toHaveBeenCalled());
  if (samples.length) {
    await waitFor(() =>
      expect(screen.queryByTestId(`sample-card-${samples[0].sampleId}`)).toBeTruthy()
    );
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  setTuningSyntheticNegatives.mockResolvedValue({
    sessionId: 'ts-1', enabled: true, created: 0, removed: 0, labelCounts: COUNTS,
  });
});

afterEach(() => {
  cleanup();
});

describe('inspection-format verdicts', () => {
  it('shows a recovered verdict as Pass with the recovered badge', async () => {
    await renderTab([sample()]);
    const card = screen.getByTestId('sample-card-s-1');
    expect(within(card).getByText('Pass')).toBeTruthy();
    expect(within(card).getByTestId('recovered-badge-s-1').textContent).toBe(
      'Recovered from the recorded answer'
    );
    expect(within(card).getByText('0.93')).toBeTruthy();
  });

  it('shows a recorded verdict as Fail without the badge', async () => {
    await renderTab([
      sample({
        source: 'live',
        recorded: {
          isAnomalous: true,
          confidence: 0.81,
          answer: '{}',
          parseError: null,
          verdict: 'fail',
          recovered: false,
        },
      }),
    ]);
    const card = screen.getByTestId('sample-card-s-1');
    expect(within(card).getByText('Fail')).toBeTruthy();
    expect(within(card).queryByTestId('recovered-badge-s-1')).toBeNull();
  });

  it('shows a legacy verdict as today (preservation: passes at c299a6e1 too)', async () => {
    await renderTab([
      sample({
        recorded: {
          isAnomalous: true,
          confidence: 0.9,
          answer: '{"is_anomalous": true, "confidence": 0.9}',
          parseError: null,
        },
      }),
    ]);
    const card = screen.getByTestId('sample-card-s-1');
    expect(within(card).getByText('Anomalous')).toBeTruthy();
    expect(within(card).queryByTestId('recovered-badge-s-1')).toBeNull();
  });
});

describe('the Synthetic negatives toggle', () => {
  it('is disabled with the reason for an inspection-format node', async () => {
    await renderTab([sample()], REASON);
    const toggle = screen.getByTestId('synthetic-toggle');
    const checkbox = within(toggle).getByRole('checkbox');
    expect(checkbox).toBeDisabled();
    expect(within(toggle).getByText(REASON)).toBeTruthy();
    fireEvent.click(checkbox);
    expect(setTuningSyntheticNegatives).not.toHaveBeenCalled();
  });

  it('is enabled without a reason (preservation: passes at c299a6e1 too)', async () => {
    await renderTab([sample()]);
    const checkbox = within(screen.getByTestId('synthetic-toggle')).getByRole(
      'checkbox'
    );
    expect(checkbox).not.toBeDisabled();
    expect(screen.queryByText(REASON)).toBeNull();
  });
});
