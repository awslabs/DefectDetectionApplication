/**
 * Property-based test for the LabelerWorkspace completion-view steal loop
 * (labeling-job-cleanup-work-stealing-and-podium task 2.8, design
 * Property 11).
 *
 * Mocking follows the PromptTuningPreview / CreateLabelingJob property
 * suite precedent: a `vi.hoisted` mock table behind a Proxy over the API
 * service module, with an `ApiError` class carrying `status`.
 * `react-router-dom` is stubbed so `useSearchParams` reports `?job=` and
 * the workspace enters the labeling view directly. `AnnotationCanvas` is
 * stubbed to a marker div — Property 11 is about the completion view, not
 * the canvas (which has its own suites) — while `WinnerPodium` is the real
 * component, so the `winner-podium` testid asserted here is its own.
 *
 * Each fast-check run scripts the mocked apiService with per-run queues of
 * pool states (stealable counts, job_complete flags with and without
 * podium entries), steal outcomes (successes and none-remain 409s), and
 * next-task payloads (fresh completion payloads to keep the loop going, or
 * a presentable task). The run then drives the workspace round by round
 * and asserts the rendered completion surface against an oracle re-derived
 * from the acceptance criteria, plus exact API call counts and fully
 * drained queues — an unscripted or missing request fails the property.
 */
import { describe, expect, it, vi } from 'vitest';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import * as fc from 'fast-check';

import LabelerWorkspace from './LabelerWorkspace';
import { ApiError } from '../../services/api';
import type {
  LabelerJobPoolResponse,
  LabelerNextTaskResponse,
  PodiumEntry,
} from '../../services/api';

const JOB_ID = 'job-steal-loop';

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    getLabelerJobs: vi.fn(),
    getNextTask: vi.fn(),
    getLabelerJobPool: vi.fn(),
    stealTask: vi.fn(),
  },
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
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

// The workspace reads the active job from `?job=`; entering through the
// job-list table is not this property's concern.
vi.mock('react-router-dom', () => ({
  useSearchParams: () => [new URLSearchParams({ job: JOB_ID }), vi.fn()],
}));

// Property 11 is about the completion view, not the canvas. The stub
// surfaces the presented image URL so a stolen task's arrival through the
// existing next-task flow is observable (Requirement 6.3).
vi.mock('../../components/labeling/AnnotationCanvas', () => ({
  default: ({ imageUrl }: { imageUrl: string }) => (
    <div data-testid="annotation-canvas-stub">{imageUrl}</div>
  ),
}));

// ---------------------------------------------------------------------------
// Scripted payloads
// ---------------------------------------------------------------------------

/** A fresh completion payload — a new object per round so the pool effect
 * re-runs on every return to the completion state (Requirement 6.4). */
const completionPayload = (submitted: number): LabelerNextTaskResponse => ({
  complete: true,
  submitted_count: submitted,
  remaining_count: 0,
});

const STOLEN_TASK_ID = 'stolen-task-1';

/** The presentable task served after a successful steal (Requirement 6.3). */
const stolenTaskPayload = (): LabelerNextTaskResponse => ({
  complete: false,
  task_id: STOLEN_TASK_ID,
  job_id: JOB_ID,
  image_url: `https://images.example/${STOLEN_TASK_ID}.jpg`,
  image_url_expires_at: 4_000_000_000,
  task_type: 'Classification',
  label_set: ['normal', 'anomaly'],
  submitted_count: 5,
  remaining_count: 1,
});

/** An InProgress pool with teammate work remaining — the offer state. */
const offerPool = (count: number): LabelerJobPoolResponse => ({
  job_id: JOB_ID,
  stealable_count: count,
  job_complete: false,
});

// ---------------------------------------------------------------------------
// Scenario generator
// ---------------------------------------------------------------------------

/** A round that keeps the loop going: an offer whose activation either
 * succeeds into a fresh completion payload or answers none-remain 409. */
interface ContinuingStep {
  count: number;
  outcome: 'completion' | 'none-remain-409';
}

/** How a scenario ends: an arbitrary terminal pool state (podium, offer
 * left unclicked, or plain), a pool fetch failure, or a steal that
 * presents a task. */
type FinalStep =
  | { kind: 'pool'; pool: LabelerJobPoolResponse }
  | { kind: 'pool-failure' }
  | { kind: 'steal-to-task'; count: number };

interface Scenario {
  steps: ContinuingStep[];
  final: FinalStep;
}

/** Ranked podium entries (places 1..k), with and without emails. */
const podiumEntriesArb: fc.Arbitrary<PodiumEntry[]> = fc
  .array(
    fc.record({
      withEmail: fc.boolean(),
      submitted: fc.integer({ min: 1, max: 40 }),
    }),
    { minLength: 1, maxLength: 3 }
  )
  .map((rows) =>
    rows.map(
      (row, index): PodiumEntry => ({
        place: (index + 1) as 1 | 2 | 3,
        user_id: `labeler-${index}`,
        ...(row.withEmail ? { email: `labeler-${index}@example.com` } : {}),
        submitted: row.submitted,
        final_submitted_at: 1_700_000_000 + index,
      })
    )
  );

/**
 * Arbitrary terminal pool payloads, including adversarial combinations the
 * backend never produces (entries without job_complete; job_complete with
 * a positive stealable count) — the rendering must be the exact function
 * of the payload that Requirements 6.2, 6.6 and 8.3 pin.
 */
const terminalPoolArb: fc.Arbitrary<LabelerJobPoolResponse> = fc
  .record({
    stealableCount: fc.integer({ min: 0, max: 7 }),
    jobComplete: fc.boolean(),
    podiumMode: fc.constantFrom<'absent' | 'empty' | 'entries'>(
      'absent',
      'empty',
      'entries'
    ),
    entries: podiumEntriesArb,
  })
  .map(
    ({ stealableCount, jobComplete, podiumMode, entries }): LabelerJobPoolResponse => ({
      job_id: JOB_ID,
      stealable_count: stealableCount,
      job_complete: jobComplete,
      ...(podiumMode === 'absent'
        ? {}
        : { podium: podiumMode === 'empty' ? [] : entries }),
    })
  );

const continuingStepArb: fc.Arbitrary<ContinuingStep> = fc.record({
  count: fc.integer({ min: 1, max: 7 }),
  outcome: fc.constantFrom<'completion' | 'none-remain-409'>(
    'completion',
    'none-remain-409'
  ),
});

const finalStepArb: fc.Arbitrary<FinalStep> = fc.oneof(
  terminalPoolArb.map((pool): FinalStep => ({ kind: 'pool', pool })),
  fc.constant<FinalStep>({ kind: 'pool-failure' }),
  fc
    .integer({ min: 1, max: 7 })
    .map((count): FinalStep => ({ kind: 'steal-to-task', count }))
);

const scenarioArb: fc.Arbitrary<Scenario> = fc.record({
  steps: fc.array(continuingStepArb, { minLength: 0, maxLength: 2 }),
  final: finalStepArb,
});

// ---------------------------------------------------------------------------
// Per-run mock priming
// ---------------------------------------------------------------------------

type PoolScript =
  | { kind: 'ok'; pool: LabelerJobPoolResponse }
  | { kind: 'fail' };
type StealScript = { kind: 'ok' } | { kind: '409' };

/** Build the scripted queues for one scenario and (re)prime the mocks. */
function primeRun(scenario: Scenario) {
  const nextTaskQueue: LabelerNextTaskResponse[] = [completionPayload(4)];
  const poolQueue: PoolScript[] = [];
  const stealQueue: StealScript[] = [];

  let submitted = 5;
  for (const step of scenario.steps) {
    poolQueue.push({ kind: 'ok', pool: offerPool(step.count) });
    if (step.outcome === 'completion') {
      stealQueue.push({ kind: 'ok' });
      nextTaskQueue.push(completionPayload(submitted));
      submitted += 1;
    } else {
      stealQueue.push({ kind: '409' });
    }
  }
  if (scenario.final.kind === 'pool') {
    poolQueue.push({ kind: 'ok', pool: scenario.final.pool });
  } else if (scenario.final.kind === 'pool-failure') {
    poolQueue.push({ kind: 'fail' });
  } else {
    poolQueue.push({ kind: 'ok', pool: offerPool(scenario.final.count) });
    stealQueue.push({ kind: 'ok' });
    nextTaskQueue.push(stolenTaskPayload());
  }

  vi.clearAllMocks();
  apiMocks.getLabelerJobs.mockResolvedValue({
    jobs: [
      {
        job_id: JOB_ID,
        job_name: 'Steal Loop Job',
        task_type: 'Classification',
        label_set: ['normal', 'anomaly'],
        submitted_count: 4,
        remaining_count: 0,
      },
    ],
    count: 1,
  });
  apiMocks.getNextTask.mockImplementation(async (jobId: string) => {
    expect(jobId).toBe(JOB_ID);
    const next = nextTaskQueue.shift();
    if (!next) throw new Error('unscripted getNextTask call');
    return next;
  });
  apiMocks.getLabelerJobPool.mockImplementation(async (jobId: string) => {
    expect(jobId).toBe(JOB_ID);
    const script = poolQueue.shift();
    if (!script) throw new Error('unscripted getLabelerJobPool call');
    if (script.kind === 'fail') {
      throw new ApiError('The pool is unavailable', 500);
    }
    return script.pool;
  });
  apiMocks.stealTask.mockImplementation(async (jobId: string) => {
    expect(jobId).toBe(JOB_ID);
    const script = stealQueue.shift();
    if (!script) throw new Error('unscripted stealTask call');
    if (script.kind === '409') {
      throw new ApiError('No stealable tasks remain in this job', 409);
    }
    return {
      task_id: STOLEN_TASK_ID,
      job_id: JOB_ID,
      stolen_from: 'teammate-1',
      stealable_count: 0,
    };
  });

  return { nextTaskQueue, poolQueue, stealQueue };
}

// ---------------------------------------------------------------------------
// Assertion helpers
// ---------------------------------------------------------------------------

/** Flush the pending promise chain (mock resolution → setState → render). */
const flushAsync = async () => {
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
};

/** Whether the take-work offer names exactly `count` remaining images. */
function offerTextPresent(count: number): boolean {
  const expected = `Teammates still have ${count} unsubmitted ${
    count === 1 ? 'image' : 'images'
  }.`;
  return (
    screen.queryAllByText((_, element) => {
      const text = (element?.textContent ?? '').replace(/\s+/g, ' ').trim();
      return text === expected;
    }).length > 0
  );
}

/** No error alert of the workspace is on screen (Requirement 6.5). */
function expectNoErrorSurface() {
  expect(screen.queryByText('Failed to load task')).toBeNull();
  expect(screen.queryByText('Submission not saved')).toBeNull();
  expect(screen.queryByText('Failed to load jobs')).toBeNull();
}

/**
 * The oracle: the completion surface as a pure function of the pool
 * payload (null = unavailable / fetch failed). Podium exactly when
 * job_complete with non-empty entries (Requirement 8.3); the take-work
 * offer with the reported count exactly when stealable work remains and
 * the podium does not render (Requirements 6.2, 6.6); plain completion
 * otherwise (Requirement 6.6, pool-failure degradation).
 */
function assertCompletionSurface(pool: LabelerJobPoolResponse | null) {
  expect(screen.getByText('All done!')).toBeInTheDocument();
  const podiumExpected =
    pool !== null &&
    pool.job_complete &&
    !!pool.podium &&
    pool.podium.length > 0;
  const offerExpected =
    !podiumExpected && pool !== null && pool.stealable_count > 0;

  if (podiumExpected) {
    expect(screen.getByTestId('winner-podium')).toBeInTheDocument();
    expect(screen.queryAllByTestId(/^podium-place-/)).toHaveLength(
      (pool as LabelerJobPoolResponse).podium!.length
    );
  } else {
    expect(screen.queryByTestId('winner-podium')).toBeNull();
  }

  if (offerExpected) {
    expect(screen.getByTestId('steal-task-button')).toBeInTheDocument();
    expect(
      offerTextPresent((pool as LabelerJobPoolResponse).stealable_count)
    ).toBe(true);
  } else {
    expect(screen.queryByTestId('steal-task-button')).toBeNull();
  }
}

// ---------------------------------------------------------------------------
// Property 11 (task 2.8)
// ---------------------------------------------------------------------------

describe('Feature: labeling-job-cleanup-work-stealing-and-podium, Property 11: The completion view drives the steal loop from the pool state', () => {
  /**
   * *For any* scripted sequence of pool states and steal outcomes
   * (stealable counts, job_complete flags with and without podium entries,
   * steal successes and none-remain 409s), the completion view SHALL
   * render the Winner_Podium exactly when the pool reports job_complete
   * with entries, SHALL offer the take-work control with the reported
   * count exactly when stealable work remains, SHALL issue one
   * Steal_Request per activation followed by a next-task load on success,
   * SHALL refresh the pool without an error surface on a none-remain
   * answer, and SHALL render the plain completion message when neither
   * holds.
   *
   * **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 8.3**
   */
  it('renders podium/offer/plain per pool state and drives one steal plus a next-task load per activation', async () => {
    await fc.assert(
      fc.asyncProperty(scenarioArb, async (scenario) => {
        cleanup();
        const queues = primeRun(scenario);

        render(<LabelerWorkspace />);

        // Exact-call bookkeeping, advanced round by round.
        let poolCalls = 0;
        let stealCalls = 0;
        let nextCalls = 1; // entering the job loads the first payload

        await waitFor(() =>
          expect(apiMocks.getNextTask).toHaveBeenCalledTimes(1)
        );

        /** Wait for the next pool fetch and let its state land. */
        const settlePool = async () => {
          poolCalls += 1;
          await waitFor(() =>
            expect(apiMocks.getLabelerJobPool).toHaveBeenCalledTimes(poolCalls)
          );
          await flushAsync();
        };

        for (const step of scenario.steps) {
          // Every return to the completion state fetched the pool anew
          // (Requirement 6.4) and renders the offer with the reported
          // count (Requirement 6.2).
          await settlePool();
          assertCompletionSurface(offerPool(step.count));
          expectNoErrorSurface();

          fireEvent.click(screen.getByTestId('steal-task-button'));
          stealCalls += 1;
          await waitFor(() =>
            expect(apiMocks.stealTask).toHaveBeenCalledTimes(stealCalls)
          );

          if (step.outcome === 'completion') {
            // One Steal_Request, then the next-task load (Requirement
            // 6.3); the fresh completion payload re-fetches the pool
            // (Requirement 6.4), asserted by the next round's settlePool.
            nextCalls += 1;
            await waitFor(() =>
              expect(apiMocks.getNextTask).toHaveBeenCalledTimes(nextCalls)
            );
          } else {
            // None-remain 409: the pool is refetched without a next-task
            // load and without an error surface (Requirement 6.5); the
            // refetch is the next round's settlePool.
            await flushAsync();
            expect(apiMocks.getNextTask).toHaveBeenCalledTimes(nextCalls);
            expectNoErrorSurface();
          }
        }

        if (scenario.final.kind === 'pool') {
          await settlePool();
          assertCompletionSurface(scenario.final.pool);
          expectNoErrorSurface();
        } else if (scenario.final.kind === 'pool-failure') {
          // A pool fetch failure degrades to the plain completion view
          // with no error surface (Requirement 6.6 posture).
          await settlePool();
          assertCompletionSurface(null);
          expectNoErrorSurface();
        } else {
          // Successful steal whose next-task load presents the stolen
          // task through the existing flow (Requirement 6.3).
          await settlePool();
          assertCompletionSurface(offerPool(scenario.final.count));
          fireEvent.click(screen.getByTestId('steal-task-button'));
          stealCalls += 1;
          nextCalls += 1;
          await waitFor(() =>
            expect(apiMocks.stealTask).toHaveBeenCalledTimes(stealCalls)
          );
          await waitFor(() =>
            expect(apiMocks.getNextTask).toHaveBeenCalledTimes(nextCalls)
          );
          const stub = await screen.findByTestId('annotation-canvas-stub');
          expect(stub.textContent).toContain(STOLEN_TASK_ID);
          expect(screen.queryByText('All done!')).toBeNull();
          expect(screen.queryByTestId('steal-task-button')).toBeNull();
          expect(screen.queryByTestId('winner-podium')).toBeNull();
          // Presenting a task fetches no pool: the effect only runs on
          // completion payloads (checked by the exact counts below).
          await flushAsync();
        }

        // Exact-call discipline: every scripted response was consumed and
        // nothing unscripted was issued.
        expect(apiMocks.getLabelerJobPool).toHaveBeenCalledTimes(poolCalls);
        expect(apiMocks.stealTask).toHaveBeenCalledTimes(stealCalls);
        expect(apiMocks.getNextTask).toHaveBeenCalledTimes(nextCalls);
        expect(queues.nextTaskQueue).toHaveLength(0);
        expect(queues.poolQueue).toHaveLength(0);
        expect(queues.stealQueue).toHaveLength(0);
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});
