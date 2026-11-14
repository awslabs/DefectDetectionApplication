/**
 * List-form reference files in the Samples and Compare tabs (AC-36
 * frontend).
 *
 * A sample whose node sent several reference files carries `references`.
 * Both tabs then show the list in request order instead of their single
 * reference image: an image as an image captioned with its label, a
 * document as a link to its presigned URL that opens in a new tab, with the
 * text "{label} · {FORMAT} · {size}". The size rule is
 * `formatReferenceSize`: 12,288 bytes is "12 KB", 182,004 bytes "178 KB"
 * and 4,500,000 bytes "4.3 MB". Single-image samples render as today.
 *
 * `utils/referenceFiles.ts` is new with this change, so it is loaded inside
 * the test that needs it. At c299a6e1 both tabs render their one reference
 * slot, so `getByText("Reference 2 · TXT · 12 KB")` fails.
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
import CompareTab from './CompareTab';
import type {
  ScoreRunView,
  TuningCandidate,
  TuningSampleView,
} from './types';

const {
  listTuningSamples,
  setTuningSampleLabels,
  setTuningSyntheticNegatives,
  listTuningScoreRunOutcomes,
  diffTuningScoreRuns,
  setTuningSelection,
} = vi.hoisted(() => ({
  listTuningSamples: vi.fn(),
  setTuningSampleLabels: vi.fn(),
  setTuningSyntheticNegatives: vi.fn(),
  listTuningScoreRunOutcomes: vi.fn(),
  diffTuningScoreRuns: vi.fn(),
  setTuningSelection: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    listTuningSamples,
    setTuningSampleLabels,
    setTuningSyntheticNegatives,
    listTuningScoreRunOutcomes,
    diffTuningScoreRuns,
    setTuningSelection,
  },
}));

/** The new module, loaded at run time so only the test that needs it fails at c299a6e1. */
const REFERENCE_FILES_MODULE = '../../utils/referenceFiles';
function loadReferenceFiles(): Promise<typeof import('../../utils/referenceFiles')> {
  return import(/* @vite-ignore */ REFERENCE_FILES_MODULE);
}

const COUNTS = {
  OK: 1, NOK: 0, EXCLUDE: 0, unlabelled: 0, synthetic: 0, duplicates: 0, total: 1,
};

function listSample(patch: Partial<TuningSampleView> = {}): TuningSampleView {
  return {
    sampleId: 's-1',
    workflowId: 'wf-1',
    nodeId: 'check',
    nodeType: 'bedrock_inference',
    thingName: 'edge-01',
    executionId: 'exec-1',
    version: 7,
    exportedAt: 1_700_000_000,
    source: 'live',
    label: 'OK',
    duplicateOf: null,
    differentPrompt: false,
    synthetic: false,
    sourceSampleId: null,
    siblingNodeId: null,
    detectionId: null,
    detectionSlot: null,
    promptFingerprint: 'fp-1',
    recorded: {
      isAnomalous: false, confidence: 0.9, answer: '{}', parseError: null,
      verdict: 'pass', recovered: false,
    },
    input: { key: 'a/input.jpg', url: 'https://example.invalid/input.jpg' },
    reference: null,
    singleImage: false,
    references: [
      {
        index: 1, label: 'Reference 1', kind: 'image', format: 'jpeg',
        bytes: 48211, width: 800, height: 450,
        url: 'https://example.invalid/reference-1.jpg',
      },
      {
        index: 2, label: 'Reference 2', kind: 'document', format: 'txt',
        bytes: 12288, width: null, height: null,
        url: 'https://example.invalid/reference-2.txt',
      },
    ],
    ...patch,
  };
}

function run(): ScoreRunView {
  return {
    runId: 'run-1', sessionId: 'ts-1', candidateId: 'cand-1',
    candidateName: 'Candidate 1', status: 'completed', mode: 'bedrock',
    repeats: 1, plannedInvocations: 1, plannedSampleCount: 1, done: 1,
    cursor: 1, cancelRequested: false, deviceThingName: null, jobId: null,
    reportedJob: null, startedAt: 1, startedBy: 'alice', lastProgressAt: 2,
    finishedAt: 3, summary: null, error: null,
  };
}

const CANDIDATE: TuningCandidate = {
  candidateId: 'cand-1', name: 'Candidate 1', prompt: 'Check.',
  systemPrompt: null, maxTokens: 600, isBaseline: false, latestRun: run(),
};

async function renderSamples(sample: TuningSampleView) {
  listTuningSamples.mockResolvedValue({
    sessionId: 'ts-1', samples: [sample], count: 1, matched: 1,
    nextCursor: null, expiresInSeconds: 1800, labelCounts: COUNTS,
  });
  render(
    <SamplesTab
      sessionId="ts-1"
      syntheticEnabled={false}
      labelCounts={COUNTS}
      onCountsChange={vi.fn()}
      onDevicesSeen={vi.fn()}
      onSyntheticChange={vi.fn()}
    />
  );
  return waitFor(() => screen.getByTestId(`sample-card-${sample.sampleId}`));
}

async function renderCompare(sample: TuningSampleView) {
  listTuningScoreRunOutcomes.mockResolvedValue({
    runId: 'run-1',
    run: run(),
    outcomes: [{
      sampleId: sample.sampleId, repeat: 1, label: 'OK', category: 'correct',
      isAnomalous: false, confidence: 0.9, rawAnswer: '{}', parseError: null,
      outputTokens: 30, latencyMs: 800, error: null, thingName: 'edge-01',
    }],
    samples: { [sample.sampleId]: sample },
    count: 1,
    matched: 1,
    nextCursor: null,
    summary: null,
  });
  render(
    <CompareTab
      sessionId="ts-1"
      candidates={[CANDIDATE]}
      selectedCandidateId={null}
      onChanged={vi.fn()}
    />
  );
  fireEvent.click(screen.getByTestId('drill-cand-1'));
  return waitFor(() => screen.getByTestId(`outcome-${sample.sampleId}-1`));
}

function expectTheList(card: HTMLElement) {
  const link = within(card).getByText('Reference 2 · TXT · 12 KB').closest('a')!;
  expect(link.getAttribute('href')).toBe('https://example.invalid/reference-2.txt');
  expect(link.getAttribute('target')).toBe('_blank');
  expect(link.getAttribute('rel')).toContain('noopener');
  const images = within(card).getAllByRole('img');
  expect(images.map((image) => image.getAttribute('src'))).toEqual([
    'https://example.invalid/input.jpg',
    'https://example.invalid/reference-1.jpg',
  ]);
  expect(images[1].getAttribute('alt')).toBe('Reference 1');
  expect(within(card).getByText('Reference 1')).toBeTruthy();
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

describe('a list-form sample', () => {
  it('shows every reference file in the Samples tab', async () => {
    expectTheList(await renderSamples(listSample()));
  });

  it('shows the same list in the Compare tab', async () => {
    expectTheList(await renderCompare(listSample()));
  });
});

describe('a single-image sample (preservation: passes at c299a6e1 too)', () => {
  const single = listSample({
    references: undefined,
    reference: { key: 'a/reference.jpg', url: 'https://example.invalid/reference.jpg' },
  });

  it('keeps its one reference image in the Samples tab', async () => {
    const card = await renderSamples(single);
    expect(within(card).getAllByRole('img').map((i) => i.getAttribute('src'))).toEqual([
      'https://example.invalid/input.jpg',
      'https://example.invalid/reference.jpg',
    ]);
    expect(within(card).queryByRole('link')).toBeNull();
  });

  it('keeps its one reference image in the Compare tab', async () => {
    const card = await renderCompare(single);
    expect(within(card).getAllByRole('img').map((i) => i.getAttribute('src'))).toEqual([
      'https://example.invalid/input.jpg',
      'https://example.invalid/reference.jpg',
    ]);
  });
});

describe('formatReferenceSize', () => {
  it('gives whole kilobytes below 1 MiB and one decimal of megabytes above', async () => {
    const { formatReferenceSize } = await loadReferenceFiles();
    expect(formatReferenceSize(12_288)).toBe('12 KB');
    expect(formatReferenceSize(182_004)).toBe('178 KB');
    expect(formatReferenceSize(4_500_000)).toBe('4.3 MB');
    expect(formatReferenceSize(1_048_575)).toBe('1024 KB');
    expect(formatReferenceSize(1_048_576)).toBe('1.0 MB');
  });
});
