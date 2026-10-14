/**
 * Unit tests for the Score runs tab (quality-prompt-tuning, task 8.6 —
 * Requirement 6.6, with 6.7, 6.9, 6.10, 6.11 and 6.13 as the surrounding
 * start/progress behaviour).
 *
 * The start dialog: the invocation count it states and starts with
 * ((OK + NOK) × repeats), the 1..3 repeats, the VLM device picker fed by the
 * devices that exported the samples and narrowed by a refused start's
 * eligibility details, the 600-invocation refusal and the
 * nothing-to-score refusal — all stated before the attempt.
 *
 * The running run: the progress the tab shows, the running Score_Summary,
 * the "at most one run" note, and Cancel keeping the outcomes already
 * produced.
 *
 * No AWS and no network: `apiService` is mocked; the poll timer is never let
 * run (each test asserts on the response the tab already holds).
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
import ScoreRunsTab, {
  CANCEL_NOTE,
  MAX_PLANNED_INVOCATIONS,
  REPEAT_OPTIONS,
  RUN_POLL_INTERVAL_MS,
  runTooLargeMessage,
} from './ScoreRunsTab';
import type {
  LabelCounts,
  ScoreRunView,
  ScoreSummary,
  TuningCandidate,
  TuningNodeView,
} from './types';

const {
  startTuningScoreRun,
  getTuningScoreRun,
  cancelTuningScoreRun,
  ApiErrorClass,
} = vi.hoisted(() => {
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
  return {
    startTuningScoreRun: vi.fn(),
    getTuningScoreRun: vi.fn(),
    cancelTuningScoreRun: vi.fn(),
    ApiErrorClass: ApiError,
  };
});

vi.mock('../../services/api', () => ({
  ApiError: ApiErrorClass,
  apiService: { startTuningScoreRun, getTuningScoreRun, cancelTuningScoreRun },
}));

// ------------------------------------------------------------------ fixtures

const BEDROCK_NODE: TuningNodeView = {
  nodeId: 'bedrock_1',
  nodeType: 'bedrock_inference',
  model: 'anthropic.claude-3-5-sonnet',
  parameters: {},
  promptSet: { prompt: 'Inspect.', systemPrompt: null, maxTokens: 256 },
  maxTokensBounds: { min: 1, max: 4096 },
};

const VLM_NODE: TuningNodeView = { ...BEDROCK_NODE, nodeType: 'llm_inference' };

function counts(patch: Partial<LabelCounts> = {}): LabelCounts {
  return {
    OK: 6,
    NOK: 4,
    EXCLUDE: 2,
    unlabelled: 0,
    synthetic: 0,
    duplicates: 0,
    total: 12,
    ...patch,
  };
}

function summary(patch: Partial<ScoreSummary> = {}): ScoreSummary {
  return {
    samples: 10,
    invocations: 10,
    correct: 8,
    falsePass: 1,
    falseFail: 1,
    parseFailure: 0,
    invocationError: 0,
    accuracy: 0.8,
    unstable: 0,
    meanOutputTokens: 40,
    maxOutputTokens: 60,
    meanLatencyMs: 1234.6,
    ...patch,
  };
}

function run(patch: Partial<ScoreRunView> = {}): ScoreRunView {
  return {
    runId: 'run-1',
    sessionId: 'ts-1',
    candidateId: 'cand-1',
    candidateName: 'Candidate 1',
    status: 'running',
    mode: 'bedrock',
    repeats: 1,
    plannedInvocations: 10,
    plannedSampleCount: 10,
    done: 4,
    cursor: 4,
    cancelRequested: false,
    deviceThingName: null,
    jobId: null,
    reportedJob: null,
    startedAt: 1_700_000_000,
    startedBy: 'alice',
    lastProgressAt: 1_700_000_050,
    finishedAt: null,
    summary: summary({ correct: 3, falsePass: 1, invocations: 4, accuracy: 0.75 }),
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
    latestRun: null,
    ...patch,
  };
}

function renderTab(options: {
  node?: TuningNodeView | null;
  candidates?: TuningCandidate[];
  labelCounts?: LabelCounts;
  devicesSeen?: string[];
} = {}) {
  const onChanged = vi.fn();
  render(
    <ScoreRunsTab
      sessionId="ts-1"
      node={options.node ?? BEDROCK_NODE}
      candidates={options.candidates ?? [candidate()]}
      labelCounts={options.labelCounts ?? counts()}
      devicesSeen={options.devicesSeen ?? ['edge-01', 'edge-02']}
      onChanged={onChanged}
    />
  );
  return { onChanged };
}

function openDialog() {
  fireEvent.click(screen.getByTestId('start-score-run'));
}

function chooseOption(testId: string, value: string) {
  const root = screen.getByTestId(testId);
  const select = createWrapper(root.parentElement as HTMLElement).findSelect()!;
  select.openDropdown();
  select.selectOptionByValue(value);
}

beforeEach(() => {
  vi.clearAllMocks();
  startTuningScoreRun.mockResolvedValue({ run: run() });
  cancelTuningScoreRun.mockResolvedValue({
    run: run({ status: 'cancelled', done: 4, finishedAt: 1_700_000_100 }),
  });
  getTuningScoreRun.mockResolvedValue({
    run: run({ status: 'completed', done: 10 }),
    session: {},
    candidate: null,
  });
});

afterEach(() => {
  cleanup();
});

// ------------------------------------------------------------- Requirement 6.6

describe('Requirement 6.6: the start dialog states the invocation count', () => {
  it('states (OK + NOK) × repeats and carries it on the confirm action', async () => {
    renderTab();
    openDialog();
    await waitFor(() => expect(screen.getByTestId('planned-invocations')).toBeTruthy());
    expect(screen.getByTestId('planned-invocations').textContent).toBe(
      'This run will issue 10 invocations: 10 labelled samples × 1 repeat.'
    );
    expect(screen.getByTestId('confirm-start-run').textContent).toContain(
      'Start 10 invocations'
    );
  });

  it('excludes EXCLUDE and unlabelled samples from the count', async () => {
    renderTab({ labelCounts: counts({ OK: 2, NOK: 1, EXCLUDE: 5, unlabelled: 7 }) });
    openDialog();
    await waitFor(() =>
      expect(screen.getByTestId('planned-invocations').textContent).toContain(
        'This run will issue 3 invocations: 3 labelled samples × 1 repeat'
      )
    );
  });

  it('recomputes the count for 2 and 3 repeats', async () => {
    renderTab();
    openDialog();
    chooseOption('run-repeats', '3');
    await waitFor(() =>
      expect(screen.getByTestId('planned-invocations').textContent).toContain(
        '30 invocations: 10 labelled samples × 3 repeats'
      )
    );
    expect(screen.getByTestId('confirm-start-run').textContent).toContain(
      'Start 30 invocations'
    );
  });

  it('offers exactly the repeats 1..3 (Requirement 6.7)', () => {
    expect(REPEAT_OPTIONS.map((option) => option.value)).toEqual(['1', '2', '3']);
  });

  it('starts the run with the candidate and the repeats', async () => {
    renderTab({ candidates: [candidate(), candidate({ candidateId: 'cand-2', name: 'Candidate 2' })] });
    openDialog();
    chooseOption('run-candidate', 'cand-2');
    chooseOption('run-repeats', '2');
    fireEvent.click(screen.getByTestId('confirm-start-run'));
    await waitFor(() =>
      expect(startTuningScoreRun).toHaveBeenCalledWith('ts-1', {
        candidateId: 'cand-2',
        repeats: 2,
      })
    );
  });

  it('refuses a run above the 600-invocation bound before attempting it', async () => {
    renderTab({ labelCounts: counts({ OK: 300, NOK: 100 }) });
    openDialog();
    chooseOption('run-repeats', '2');
    await waitFor(() =>
      expect(screen.getByTestId('run-too-large').textContent).toBe(
        runTooLargeMessage(800)
      )
    );
    expect(MAX_PLANNED_INVOCATIONS).toBe(600);
    expect(screen.getByTestId('confirm-start-run')).toHaveProperty('disabled', true);
    fireEvent.click(screen.getByTestId('confirm-start-run'));
    expect(startTuningScoreRun).not.toHaveBeenCalled();
  });

  it('refuses a run with no labelled sample', async () => {
    renderTab({ labelCounts: counts({ OK: 0, NOK: 0 }) });
    expect(screen.getByTestId('missing-class-warning')).toBeTruthy();
    openDialog();
    await waitFor(() =>
      expect(
        screen.getByText('No sample is labelled OK or NOK, so there is nothing to score.')
      ).toBeTruthy()
    );
    fireEvent.click(screen.getByTestId('confirm-start-run'));
    expect(startTuningScoreRun).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------- Requirement 6.9

describe('Requirement 6.9: the VLM device picker', () => {
  it('is absent for a Bedrock node', async () => {
    renderTab();
    openDialog();
    await waitFor(() => expect(screen.getByTestId('planned-invocations')).toBeTruthy());
    expect(screen.queryByTestId('run-device')).toBeNull();
  });

  it('offers the devices that exported the samples and sends the chosen one', async () => {
    renderTab({ node: VLM_NODE, devicesSeen: ['edge-01', 'edge-02'] });
    openDialog();
    await waitFor(() => expect(screen.getByTestId('run-device')).toBeTruthy());
    chooseOption('run-device', 'edge-02');
    await waitFor(() =>
      expect(screen.getByTestId('run-device-note').textContent).toContain(
        'executed on device edge-02'
      )
    );
    fireEvent.click(screen.getByTestId('confirm-start-run'));
    await waitFor(() =>
      expect(startTuningScoreRun).toHaveBeenCalledWith('ts-1', {
        candidateId: 'cand-1',
        repeats: 1,
        deviceThingName: 'edge-02',
      })
    );
  });

  it('says so when no device exported samples for the node', async () => {
    renderTab({ node: VLM_NODE, devicesSeen: [] });
    openDialog();
    await waitFor(() =>
      expect(
        screen.getByText('Pick the device that will execute the run.')
      ).toBeTruthy()
    );
    expect(
      within(screen.getByTestId('run-device')).getByText(
        'No device exported samples for this node'
      )
    ).toBeTruthy();
  });

  it('narrows the picker to the eligible devices a refused start names', async () => {
    renderTab({ node: VLM_NODE, devicesSeen: ['edge-01', 'edge-02', 'edge-03'] });
    startTuningScoreRun.mockRejectedValueOnce(
      new ApiErrorClass('Pick a device', 400, 'DEVICE_REQUIRED', {
        exported: ['edge-01', 'edge-02', 'edge-03'],
        registered: ['edge-01', 'edge-02'],
        eligible: ['edge-01'],
        ineligible: ['edge-02', 'edge-03'],
      })
    );
    openDialog();
    fireEvent.click(screen.getByTestId('confirm-start-run'));
    await waitFor(() =>
      expect(screen.getByTestId('start-run-error').textContent).toContain(
        'Pick a device'
      )
    );
    expect(screen.getByTestId('ineligible-devices').textContent).toContain(
      'edge-02, edge-03'
    );
    // The second attempt only offers the eligible device.
    const select = createWrapper(
      screen.getByTestId('run-device').parentElement as HTMLElement
    ).findSelect()!;
    select.openDropdown();
    expect(
      select.findDropdown().findOptions().map((option) => option.getElement().textContent)
    ).toEqual(['edge-01']);
  });

  it('reports a dispatch failure as a failed run rather than progress', async () => {
    startTuningScoreRun.mockResolvedValue({
      run: run({ status: 'failed', done: 0, error: 'shadow update refused' }),
      dispatchFailed: true,
    });
    renderTab({ node: VLM_NODE });
    openDialog();
    fireEvent.click(screen.getByTestId('confirm-start-run'));
    await waitFor(() =>
      expect(screen.getByTestId('run-error').textContent).toBe(
        'shadow update refused'
      )
    );
    expect(screen.queryByTestId('cancel-score-run')).toBeNull();
  });
});

// ------------------------------------------------------ Requirements 6.8, 6.11

describe('Requirements 6.8, 6.11: progress, the running summary and cancel', () => {
  it('shows the progress and the running Score_Summary of the in-progress run', () => {
    renderTab({ candidates: [candidate({ latestRun: run() })] });
    expect(screen.getByText('4 of 10 invocations')).toBeTruthy();
    expect(screen.getByTestId('live-correct').textContent).toBe('3');
    expect(screen.getByTestId('live-false-pass').textContent).toBe('1');
    expect(screen.getAllByText('75%').length).toBeGreaterThan(0);
    expect(screen.getByTestId('run-in-progress').textContent).toContain(
      'At most one run per session may be in progress.'
    );
    // Requirement 6.10: a second run cannot be started while one runs.
    expect(screen.getByTestId('start-score-run')).toHaveProperty('disabled', true);
  });

  it('polls the running run on a fixed cadence', () => {
    expect(RUN_POLL_INTERVAL_MS).toBe(5000);
    vi.useFakeTimers();
    try {
      renderTab({ candidates: [candidate({ latestRun: run() })] });
      expect(getTuningScoreRun).not.toHaveBeenCalled();
      vi.advanceTimersByTime(RUN_POLL_INTERVAL_MS);
      expect(getTuningScoreRun).toHaveBeenCalledWith('run-1');
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels the run, keeping the outcomes it already produced', async () => {
    const { onChanged } = renderTab({ candidates: [candidate({ latestRun: run() })] });
    expect(screen.getByText(CANCEL_NOTE)).toBeTruthy();
    fireEvent.click(screen.getByTestId('cancel-score-run'));
    await waitFor(() => expect(cancelTuningScoreRun).toHaveBeenCalledWith('run-1'));
    await waitFor(() => expect(screen.getByText('cancelled')).toBeTruthy());
    // The partial progress stays on screen; nothing is discarded.
    expect(screen.getByText('4 of 10 invocations')).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
  });

  it('offers no cancel for a finished run', () => {
    renderTab({ candidates: [candidate({ latestRun: run({ status: 'completed', done: 10 }) })] });
    expect(screen.queryByTestId('cancel-score-run')).toBeNull();
    expect(screen.getByText('completed')).toBeTruthy();
  });

  it('lists the latest run per candidate with its mode and accuracy', () => {
    renderTab({
      candidates: [
        candidate({
          candidateId: 'baseline',
          name: 'Baseline',
          isBaseline: true,
          latestRun: run({
            runId: 'run-0',
            status: 'completed',
            done: 10,
            mode: 'device',
            deviceThingName: 'edge-01',
            summary: summary({ accuracy: 0.5 }),
          }),
        }),
        candidate({ candidateId: 'cand-9', name: 'Never scored' }),
      ],
    });
    const table = screen.getByRole('table');
    expect(within(table).getByText('Baseline (baseline)')).toBeTruthy();
    expect(within(table).getByText('device edge-01')).toBeTruthy();
    expect(within(table).getByText('10 / 10')).toBeTruthy();
    expect(within(table).getByText('50%')).toBeTruthy();
    expect(within(table).getByText('never scored')).toBeTruthy();
  });

  it('states that no run has been started yet', () => {
    renderTab();
    expect(screen.getByTestId('no-runs')).toBeTruthy();
  });
});
