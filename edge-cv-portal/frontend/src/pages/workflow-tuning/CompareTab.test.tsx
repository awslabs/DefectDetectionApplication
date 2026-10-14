/**
 * Unit tests for the Compare tab (quality-prompt-tuning, task 8.6 —
 * Requirements 7.2, 7.3, 7.4, 7.6, with 7.1 and 7.5 as the table and the
 * selection they are read from).
 *
 * The comparison table (one row per Candidate's most recent Score_Run,
 * the Baseline_Candidate's row among them, the false-pass count emphasised);
 * the outcome drill-down with its category filter and confidence sort
 * reaching the route, and the raw answer plus the parser's rejection reason
 * shown character-for-character for a `parse_failure`; the two-run diff
 * naming the samples whose categories differ; and the selection with its
 * prominent false-pass statement.
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
import CompareTab, { CATEGORY_OPTIONS, falsePassWarning } from './CompareTab';
import type {
  SampleOutcomeView,
  ScoreRunDiffResponse,
  ScoreRunOutcomesResponse,
  ScoreRunView,
  ScoreSummary,
  TuningCandidate,
  TuningSampleView,
} from './types';

const {
  listTuningScoreRunOutcomes,
  diffTuningScoreRuns,
  setTuningSelection,
} = vi.hoisted(() => ({
  listTuningScoreRunOutcomes: vi.fn(),
  diffTuningScoreRuns: vi.fn(),
  setTuningSelection: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: { listTuningScoreRunOutcomes, diffTuningScoreRuns, setTuningSelection },
}));

// ------------------------------------------------------------------ fixtures

function summary(patch: Partial<ScoreSummary> = {}): ScoreSummary {
  return {
    samples: 10,
    invocations: 10,
    correct: 8,
    falsePass: 2,
    falseFail: 1,
    parseFailure: 1,
    invocationError: 0,
    accuracy: 0.8,
    unstable: 1,
    meanOutputTokens: 42.4,
    maxOutputTokens: 64,
    meanLatencyMs: 900,
    ...patch,
  };
}

function run(patch: Partial<ScoreRunView> = {}): ScoreRunView {
  return {
    runId: 'run-1',
    sessionId: 'ts-1',
    candidateId: 'cand-1',
    candidateName: 'Candidate 1',
    status: 'completed',
    mode: 'bedrock',
    repeats: 1,
    plannedInvocations: 10,
    plannedSampleCount: 10,
    done: 10,
    cursor: 10,
    cancelRequested: false,
    deviceThingName: null,
    jobId: null,
    reportedJob: null,
    startedAt: 1_700_000_000,
    startedBy: 'alice',
    lastProgressAt: 1_700_000_100,
    finishedAt: 1_700_000_200,
    summary: summary(),
    error: null,
    ...patch,
  };
}

function candidate(patch: Partial<TuningCandidate> = {}): TuningCandidate {
  return {
    candidateId: 'cand-1',
    name: 'Candidate 1',
    prompt: 'Compare.',
    systemPrompt: null,
    maxTokens: 512,
    isBaseline: false,
    latestRun: run(),
    ...patch,
  };
}

const BASELINE = candidate({
  candidateId: 'baseline',
  name: 'Baseline (deployed)',
  isBaseline: true,
  latestRun: run({
    runId: 'run-0',
    candidateId: 'baseline',
    candidateName: 'Baseline (deployed)',
    summary: summary({ correct: 5, accuracy: 0.5, falsePass: 4 }),
  }),
});

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
    label: 'NOK',
    duplicateOf: null,
    differentPrompt: false,
    synthetic: false,
    sourceSampleId: null,
    siblingNodeId: null,
    detectionId: null,
    detectionSlot: null,
    promptFingerprint: 'fp-1',
    recorded: { isAnomalous: true, confidence: 0.9, answer: 'x', parseError: null },
    input: { url: 'https://example.invalid/input.jpg' },
    reference: { url: 'https://example.invalid/reference.jpg' },
    singleImage: false,
    ...patch,
  };
}

function outcome(patch: Partial<SampleOutcomeView> = {}): SampleOutcomeView {
  return {
    sampleId: 's-1',
    repeat: 1,
    label: 'NOK',
    category: 'false_pass',
    isAnomalous: false,
    confidence: 0.31,
    rawAnswer: '{"is_anomalous": false, "confidence": 0.31}',
    parseError: null,
    outputTokens: 30,
    latencyMs: 812.4,
    error: null,
    thingName: 'edge-01',
    ...patch,
  };
}

function outcomesPage(
  outcomes: SampleOutcomeView[] = [outcome()],
  patch: Partial<ScoreRunOutcomesResponse> = {}
): ScoreRunOutcomesResponse {
  return {
    runId: 'run-1',
    run: run(),
    outcomes,
    samples: { 's-1': sample() },
    count: outcomes.length,
    matched: outcomes.length,
    nextCursor: null,
    summary: summary(),
    ...patch,
  };
}

function renderTab(options: {
  candidates?: TuningCandidate[];
  selectedCandidateId?: string | null;
} = {}) {
  const onChanged = vi.fn();
  render(
    <CompareTab
      sessionId="ts-1"
      candidates={options.candidates ?? [BASELINE, candidate()]}
      selectedCandidateId={options.selectedCandidateId ?? null}
      onChanged={onChanged}
    />
  );
  return { onChanged };
}

function chooseOption(testId: string, value: string) {
  const root = screen.getByTestId(testId);
  const select = createWrapper(root.parentElement as HTMLElement).findSelect()!;
  select.openDropdown();
  select.selectOptionByValue(value);
}

/** The query object of the most recent outcomes request. */
function lastOutcomesQuery(): Record<string, unknown> {
  const calls = listTuningScoreRunOutcomes.mock.calls;
  return calls[calls.length - 1][1] as Record<string, unknown>;
}

beforeEach(() => {
  vi.clearAllMocks();
  listTuningScoreRunOutcomes.mockResolvedValue(outcomesPage());
  setTuningSelection.mockImplementation(async (_s, candidateId) => ({
    session: {},
    selectedCandidateId: candidateId,
    latestRun: run(),
    falsePasses: candidateId === null ? null : 2,
  }));
  diffTuningScoreRuns.mockResolvedValue({
    a: run(),
    b: run({ runId: 'run-0', candidateName: 'Baseline (deployed)' }),
    count: 1,
    differing: [
      {
        sampleId: 's-1',
        label: 'NOK',
        a: { categories: ['correct'], outcomes: [] },
        b: { categories: ['false_pass'], outcomes: [] },
      },
    ],
  } as ScoreRunDiffResponse);
});

afterEach(() => {
  cleanup();
});

// -------------------------------------------------------- Requirements 7.1/7.6

describe('Requirements 7.1, 7.6: the comparison table', () => {
  it('carries one row per candidate, the baseline included', () => {
    renderTab();
    const table = screen.getByTestId('comparison-table');
    expect(within(table).getByText('Candidate 1')).toBeTruthy();
    expect(within(table).getByText('Baseline (deployed)')).toBeTruthy();
    expect(within(table).getByText('Baseline')).toBeTruthy();
  });

  it('shows every Score_Summary column of the latest run', () => {
    renderTab({ candidates: [candidate()] });
    const table = screen.getByTestId('comparison-table');
    for (const header of [
      'Candidate',
      'Run',
      'Invocations',
      'Accuracy',
      'False passes',
      'False fails',
      'Parse failures',
      'Errors',
      'Unstable',
      'Mean tokens',
    ]) {
      expect(within(table).getByText(header)).toBeTruthy();
    }
    const row = within(table).getAllByRole('row').find((r) =>
      r.textContent?.includes('Candidate 1')
    )!;
    expect(row.textContent).toContain('completed');
    expect(row.textContent).toContain('80%');
    expect(row.textContent).toContain('42'); // mean output tokens, rounded
  });

  it('emphasises a non-zero false-pass count', () => {
    renderTab({ candidates: [candidate()] });
    const table = screen.getByTestId('comparison-table');
    const emphasised = within(table)
      .getAllByText('2')
      .find((node) => node.className.includes('font-weight'));
    expect(emphasised).toBeTruthy();
  });

  it('states that nothing is scored yet when no candidate has a run', () => {
    renderTab({ candidates: [] });
    expect(screen.getByTestId('no-scored-runs')).toBeTruthy();
  });

  it('persists the selection and states its false passes prominently', async () => {
    const { onChanged } = renderTab();
    fireEvent.click(screen.getByTestId('select-cand-1'));
    await waitFor(() =>
      expect(setTuningSelection).toHaveBeenCalledWith('ts-1', 'cand-1')
    );
    await waitFor(() =>
      expect(screen.getByTestId('selection-false-passes').textContent).toContain(
        'false passes'
      )
    );
    expect(screen.getByTestId('selection-false-passes').textContent).toContain(
      'A false pass ships a defective part.'
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it('badges the selected candidate and clears the selection with null', async () => {
    renderTab({ selectedCandidateId: 'cand-1' });
    expect(
      within(screen.getByTestId('comparison-table')).getByText('Selected')
    ).toBeTruthy();
    fireEvent.click(screen.getByTestId('clear-selection'));
    await waitFor(() => expect(setTuningSelection).toHaveBeenCalledWith('ts-1', null));
  });

  it('words the false-pass statement per count', () => {
    expect(falsePassWarning(1, 'C')).toContain('C has 1 false pass:');
    expect(falsePassWarning(3, 'C')).toContain('C has 3 false passes:');
    expect(falsePassWarning(3, 'C')).toContain('A false pass ships a defective part.');
  });
});

// -------------------------------------------------------- Requirements 7.2/7.4

describe('Requirements 7.2, 7.4: the outcome drill-down', () => {
  async function openDrilldown() {
    renderTab();
    fireEvent.click(screen.getByTestId('drill-cand-1'));
    await waitFor(() => expect(listTuningScoreRunOutcomes).toHaveBeenCalledWith('run-1', {}));
    await waitFor(() => expect(screen.getByTestId('outcome-s-1-1')).toBeTruthy());
  }

  it('lists each outcome with its images, label, category, verdict and confidence', async () => {
    await openDrilldown();
    const card = screen.getByTestId('outcome-s-1-1');
    expect(within(card).getAllByRole('img').map((i) => i.getAttribute('src'))).toEqual([
      'https://example.invalid/input.jpg',
      'https://example.invalid/reference.jpg',
    ]);
    expect(card.textContent).toContain('Label NOK');
    expect(card.textContent).toContain('false_pass');
    expect(within(card).getByText('Normal')).toBeTruthy();
    expect(within(card).getByText('0.31')).toBeTruthy();
    expect(within(card).getByText('812 ms')).toBeTruthy();
    expect(screen.getByTestId('drill-false-pass').textContent).toBe('2');
  });

  it('carries the raw answer character for character', async () => {
    await openDrilldown();
    expect(screen.getByTestId('raw-s-1-1').textContent).toBe(
      '{"is_anomalous": false, "confidence": 0.31}'
    );
  });

  it('shows the parser\'s rejection reason and the raw answer of a parse failure', async () => {
    listTuningScoreRunOutcomes.mockResolvedValue(
      outcomesPage([
        outcome({
          category: 'parse_failure',
          isAnomalous: null,
          confidence: null,
          rawAnswer: 'The part looks fine to me.  ',
          parseError: 'no JSON object in the answer',
        }),
      ])
    );
    await openDrilldown();
    expect(screen.getByTestId('parse-error-s-1-1').textContent).toBe(
      'The verdict parser rejected this answer: no JSON object in the answer'
    );
    expect(screen.getByTestId('raw-s-1-1').textContent).toBe(
      'The part looks fine to me.  '
    );
  });

  it('shows an invocation error and the missing-answer placeholder', async () => {
    listTuningScoreRunOutcomes.mockResolvedValue(
      outcomesPage([
        outcome({
          category: 'invocation_error',
          isAnomalous: null,
          confidence: null,
          rawAnswer: null,
          error: 'ThrottlingException',
        }),
      ])
    );
    await openDrilldown();
    expect(screen.getByText('Invocation error: ThrottlingException')).toBeTruthy();
    expect(screen.getByTestId('raw-s-1-1').textContent).toBe('No answer was returned.');
  });

  it('sends the category filter to the route', async () => {
    await openDrilldown();
    chooseOption('outcome-category', 'false_pass');
    await waitFor(() => expect(lastOutcomesQuery()).toEqual({ category: 'false_pass' }));
    expect(CATEGORY_OPTIONS.map((option) => option.value)).toEqual([
      '',
      'correct',
      'false_pass',
      'false_fail',
      'parse_failure',
      'invocation_error',
    ]);
  });

  it('sends the confidence sort with its order', async () => {
    await openDrilldown();
    chooseOption('outcome-sort', 'desc');
    await waitFor(() =>
      expect(lastOutcomesQuery()).toEqual({ sort: 'confidence', order: 'desc' })
    );
    chooseOption('outcome-sort', 'asc');
    await waitFor(() =>
      expect(lastOutcomesQuery()).toEqual({ sort: 'confidence', order: 'asc' })
    );
  });

  it('states when the category matches no outcome, and closes', async () => {
    await openDrilldown();
    listTuningScoreRunOutcomes.mockResolvedValue(outcomesPage([], { matched: 0 }));
    chooseOption('outcome-category', 'invocation_error');
    await waitFor(() => expect(screen.getByTestId('no-outcomes')).toBeTruthy());
    fireEvent.click(screen.getByTestId('close-drilldown'));
    expect(screen.queryByTestId('no-outcomes')).toBeNull();
  });
});

// ------------------------------------------------------------- Requirement 7.3

describe('Requirement 7.3: the two-run diff', () => {
  it('compares the two chosen runs and names the differing samples', async () => {
    renderTab();
    chooseOption('diff-run-a', 'run-1');
    chooseOption('diff-run-b', 'run-0');
    fireEvent.click(screen.getByTestId('run-diff'));
    await waitFor(() =>
      expect(diffTuningScoreRuns).toHaveBeenCalledWith('run-1', 'run-0')
    );
    await waitFor(() => expect(screen.getByTestId('diff-table')).toBeTruthy());
    const table = screen.getByTestId('diff-table');
    expect(within(table).getByText('s-1')).toBeTruthy();
    expect(within(table).getByText('correct')).toBeTruthy();
    expect(within(table).getByText('false_pass')).toBeTruthy();
    // The columns name the two candidates being compared.
    expect(table.textContent).toContain('Baseline (deployed)');
  });

  it('cannot be run before both runs are chosen', () => {
    renderTab();
    expect(screen.getByTestId('run-diff')).toHaveProperty('disabled', true);
    fireEvent.click(screen.getByTestId('run-diff'));
    expect(diffTuningScoreRuns).not.toHaveBeenCalled();
  });

  it('says so when the two runs agreed on every sample', async () => {
    diffTuningScoreRuns.mockResolvedValue({
      a: run(),
      b: run({ runId: 'run-0' }),
      count: 0,
      differing: [],
    } as ScoreRunDiffResponse);
    renderTab();
    chooseOption('diff-run-a', 'run-1');
    chooseOption('diff-run-b', 'run-0');
    fireEvent.click(screen.getByTestId('run-diff'));
    await waitFor(() => expect(screen.getByTestId('diff-empty')).toBeTruthy());
  });
});
