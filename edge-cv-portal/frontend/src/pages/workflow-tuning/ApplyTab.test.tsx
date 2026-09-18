/**
 * Unit tests for the Apply tab (quality-prompt-tuning, task 8.6 —
 * Requirement 7.6, with 8.2, 8.4 and 8.5 as the confirmation's own rules).
 *
 * The confirmation: both Score_Summaries (the selection's and the
 * Baseline_Candidate's) side by side and the selection's false-pass count
 * stated prominently, because a false pass ships a defective part
 * (Requirement 7.6). Applying is offered only for a selection with a
 * completed Score_Run (8.2) and never while the node is no longer a
 * Tunable_Node of the latest version (8.4); success names the new version
 * and states that nothing was validated, packaged or deployed (8.5).
 *
 * No AWS and no network: `apiService` and `useNavigate` are mocked.
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
import ApplyTab, {
  NEEDS_COMPLETED_RUN,
  NODE_NOT_TUNABLE_MESSAGE,
  NOT_DEPLOYED_NOTE,
} from './ApplyTab';
import type {
  ApplyCandidateResponse,
  ScoreRunView,
  ScoreSummary,
  TuningCandidate,
  TuningSession,
} from './types';

const { navigateMock, applyTuningCandidate } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  applyTuningCandidate: vi.fn(),
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock('../../services/api', () => ({
  apiService: { applyTuningCandidate },
}));

// ------------------------------------------------------------------ fixtures

function summary(patch: Partial<ScoreSummary> = {}): ScoreSummary {
  return {
    samples: 10,
    invocations: 10,
    correct: 9,
    falsePass: 2,
    falseFail: 1,
    parseFailure: 0,
    invocationError: 0,
    accuracy: 0.9,
    unstable: 0,
    meanOutputTokens: 40,
    maxOutputTokens: 60,
    meanLatencyMs: 800,
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
    summary: summary({ correct: 5, accuracy: 0.5, falsePass: 4, falseFail: 1 }),
  }),
});

function session(patch: Partial<TuningSession> = {}): TuningSession {
  return {
    sessionId: 'ts-1',
    usecaseId: 'uc-1',
    workflowId: 'wf-1',
    nodeId: 'bedrock_1',
    nodeType: 'bedrock_inference',
    baselineVersion: 4,
    baselineCandidateId: 'baseline',
    baselineFingerprint: 'fp-base',
    selectedCandidateId: 'cand-1',
    syntheticNegativesEnabled: false,
    lastRefresh: null,
    latestTuningResult: null,
    ...patch,
  };
}

function applied(): ApplyCandidateResponse {
  return {
    session: session(),
    workflowId: 'wf-1',
    nodeId: 'bedrock_1',
    version: 5,
    newVersion: 5,
    previousVersion: 4,
    promptSet: { prompt: 'Compare.', systemPrompt: null, maxTokens: 512 },
    candidate: candidate(),
    run: run(),
    tuningResult: {
      appliedAt: 1_700_000_300,
      appliedBy: 'alice',
      newVersion: 5,
      previousVersion: 4,
      candidateId: 'cand-1',
      candidateName: 'Candidate 1',
      scoreRunId: 'run-1',
      summary: summary(),
      baselineSummary: summary({ accuracy: 0.5, falsePass: 4 }),
    },
  };
}

function renderTab(options: {
  session?: TuningSession | null;
  candidates?: TuningCandidate[];
  nodeStillTunable?: boolean;
  latestVersion?: number | null;
} = {}) {
  const onChanged = vi.fn();
  render(
    <ApplyTab
      sessionId="ts-1"
      session={options.session ?? session()}
      candidates={options.candidates ?? [BASELINE, candidate()]}
      nodeStillTunable={options.nodeStillTunable ?? true}
      latestVersion={options.latestVersion === undefined ? 4 : options.latestVersion}
      onChanged={onChanged}
    />
  );
  return { onChanged };
}

function confirmation(): HTMLElement {
  return screen.getByTestId('apply-confirmation');
}

beforeEach(() => {
  vi.clearAllMocks();
  applyTuningCandidate.mockResolvedValue(applied());
});

afterEach(() => {
  cleanup();
});

// ------------------------------------------------------- Requirements 7.6, 8.2

describe('Requirement 7.6: the confirmation states the false passes', () => {
  it('carries both Score_Summaries side by side', () => {
    renderTab();
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    const modal = confirmation();
    expect(within(modal).getByText('Selected: Candidate 1')).toBeTruthy();
    expect(within(modal).getByText('Baseline: Baseline (deployed)')).toBeTruthy();
    // Accuracy of the selection (90%) and of the baseline (50%).
    expect(modal.textContent).toContain('Accuracy: 90%');
    expect(modal.textContent).toContain('Accuracy: 50%');
    expect(modal.textContent).toContain('False passes: 2');
    expect(modal.textContent).toContain('False passes: 4');
  });

  it('states the selection\'s false passes prominently in the confirmation', () => {
    renderTab();
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    expect(screen.getByTestId('confirm-false-passes').textContent).toContain(
      'Candidate 1 has 2 false passes'
    );
    expect(screen.getByTestId('confirm-false-passes').textContent).toContain(
      'A false pass ships a defective part.'
    );
  });

  it('says so when the selection has no false pass', () => {
    renderTab({
      candidates: [BASELINE, candidate({ latestRun: run({ summary: summary({ falsePass: 0 }) }) })],
    });
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    expect(screen.getByTestId('confirm-false-passes').textContent).toBe(
      "This candidate's latest run has no false passes."
    );
  });

  it('warns about the false passes on the tab itself too', () => {
    renderTab();
    expect(screen.getByTestId('apply-false-passes').textContent).toContain(
      '2 false passes'
    );
  });

  it('names the version the apply will save and the node it changes', () => {
    renderTab({ latestVersion: 7 });
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    expect(confirmation().textContent).toContain(
      'Version 8 will carry the prompt, system prompt and max_tokens of "Candidate 1" on node bedrock_1'
    );
    expect(confirmation().textContent).toContain(
      'Every other node, parameter and connection stays byte-identical.'
    );
    expect(screen.getByTestId('confirm-apply').textContent).toContain('Save version 8');
  });

  it('applies the selection with its completed run', async () => {
    const { onChanged } = renderTab();
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    fireEvent.click(screen.getByTestId('confirm-apply'));
    await waitFor(() =>
      expect(applyTuningCandidate).toHaveBeenCalledWith('ts-1', {
        candidateId: 'cand-1',
        runId: 'run-1',
      })
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });
});

describe('Requirement 8.2: what may be applied', () => {
  it('refuses a selection without a completed run', () => {
    renderTab({
      candidates: [BASELINE, candidate({ latestRun: run({ status: 'running', done: 3 }) })],
    });
    expect(screen.getByTestId('needs-completed-run').textContent).toBe(
      NEEDS_COMPLETED_RUN
    );
    expect(screen.getByTestId('open-apply-confirmation')).toHaveProperty(
      'disabled',
      true
    );
  });

  it('refuses a never-scored selection', () => {
    renderTab({ candidates: [BASELINE, candidate({ latestRun: null })] });
    expect(screen.getByTestId('needs-completed-run')).toBeTruthy();
    expect(screen.getByTestId('open-apply-confirmation')).toHaveProperty(
      'disabled',
      true
    );
  });

  it('asks for a selection when none is set', () => {
    renderTab({ session: session({ selectedCandidateId: null }) });
    expect(screen.getByTestId('no-selection').textContent).toContain(
      'Select a candidate on the Compare tab'
    );
    expect(screen.getByTestId('open-apply-confirmation')).toHaveProperty(
      'disabled',
      true
    );
  });
});

describe('Requirement 8.4: a node that is no longer tunable', () => {
  it('states the reason and blocks the apply', () => {
    renderTab({ nodeStillTunable: false });
    expect(screen.getByTestId('node-not-tunable').textContent).toBe(
      NODE_NOT_TUNABLE_MESSAGE
    );
    expect(screen.getByTestId('open-apply-confirmation')).toHaveProperty(
      'disabled',
      true
    );
  });
});

describe('Requirement 8.5: what a successful apply says', () => {
  it('names the new version, the candidate and the untouched previous version', async () => {
    renderTab();
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    fireEvent.click(screen.getByTestId('confirm-apply'));
    await waitFor(() => expect(screen.getByTestId('apply-success')).toBeTruthy());
    const alert = screen.getByTestId('apply-success');
    expect(alert.textContent).toContain('Version 5 saved');
    expect(alert.textContent).toContain(
      'Workflow wf-1 version 5 carries the prompt set of "Candidate 1" on node bedrock_1; version 4 is unchanged.'
    );
    expect(alert.textContent).toContain(NOT_DEPLOYED_NOTE);
  });

  it('opens the new version in the designer', async () => {
    renderTab();
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    fireEvent.click(screen.getByTestId('confirm-apply'));
    await waitFor(() => expect(screen.getByTestId('apply-success')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Open in the designer' }));
    expect(navigateMock).toHaveBeenCalledWith('/workflows/builder/wf-1');
  });

  it('surfaces a refused apply and closes the confirmation', async () => {
    applyTuningCandidate.mockRejectedValue(new Error('NODE_NOT_TUNABLE'));
    renderTab();
    fireEvent.click(screen.getByTestId('open-apply-confirmation'));
    fireEvent.click(screen.getByTestId('confirm-apply'));
    await waitFor(() => expect(screen.getByText(/NODE_NOT_TUNABLE/)).toBeTruthy());
    expect(screen.queryByTestId('apply-success')).toBeNull();
  });

  it('states the session\'s previous apply', () => {
    renderTab({
      session: session({
        latestTuningResult: {
          appliedAt: 1_699_999_000,
          appliedBy: 'alice',
          newVersion: 4,
          candidateId: 'cand-0',
          candidateName: 'Earlier candidate',
        },
      }),
    });
    expect(screen.getByTestId('latest-tuning-result').textContent).toContain(
      'saved version 4 from candidate Earlier candidate'
    );
  });
});
