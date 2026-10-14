/**
 * Example tests for the LabelerWorkspace completion-view pool surface
 * (labeling-job-cleanup-work-stealing-and-podium task 2.10).
 *
 * Plain vitest examples beside the Property 11 suite
 * (`LabelerWorkspace.steal.property.test.tsx`), reusing its mock
 * scaffolding: a `vi.hoisted` mock table behind a Proxy over the API
 * service module (with an `ApiError` class carrying `status`), a
 * `react-router-dom` stub whose `useSearchParams` reports `?job=` so the
 * workspace enters the labeling view directly, and an `AnnotationCanvas`
 * stub surfacing the presented image URL. `WinnerPodium` is the real
 * component, so the `winner-podium` testid asserted here is its own.
 *
 * The examples pin the five concrete completion-surface behaviors:
 * 1. Offer wiring — the take-work offer names the stealable count and one
 *    activation issues one `stealTask` then one `getNextTask`
 *    (Requirements 6.2, 6.3).
 * 2. Podium-on-complete — a complete job's podium renders in place of the
 *    take-work offer (Requirement 8.3).
 * 3. None-remain refresh — a 409 steal answer refetches the pool with no
 *    error surface (Requirement 6.5).
 * 4. Pool-failure degradation — a failed pool fetch leaves the existing
 *    completion view intact with no error surface (Requirement 6.6
 *    posture).
 * 5. Plain completion — zero stealable and an incomplete job add nothing
 *    to the existing completion message (Requirement 6.6).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

import LabelerWorkspace from './LabelerWorkspace';
import { ApiError } from '../../services/api';
import type {
  LabelerJobPoolResponse,
  LabelerNextTaskResponse,
  PodiumEntry,
} from '../../services/api';

const JOB_ID = 'job-pool-examples';
const STOLEN_TASK_ID = 'stolen-task-1';

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
// job-list table is not these examples' concern.
vi.mock('react-router-dom', () => ({
  useSearchParams: () => [new URLSearchParams({ job: JOB_ID }), vi.fn()],
}));

// These examples are about the completion view, not the canvas (which has
// its own suites). The stub surfaces the presented image URL so a stolen
// task's arrival through the existing next-task flow is observable.
vi.mock('../../components/labeling/AnnotationCanvas', () => ({
  default: ({ imageUrl }: { imageUrl: string }) => (
    <div data-testid="annotation-canvas-stub">{imageUrl}</div>
  ),
}));

// ---------------------------------------------------------------------------
// Payloads
// ---------------------------------------------------------------------------

/** The completion payload of `GET /labeler/jobs/{jobId}/next`. */
const completionPayload = (): LabelerNextTaskResponse => ({
  complete: true,
  submitted_count: 4,
  remaining_count: 0,
});

/** The presentable task served after a successful steal (Requirement 6.3). */
const stolenTaskPayload = (): LabelerNextTaskResponse => ({
  complete: false,
  task_id: STOLEN_TASK_ID,
  job_id: JOB_ID,
  image_url: `https://images.example/${STOLEN_TASK_ID}.jpg`,
  image_url_expires_at: 4_000_000_000,
  task_type: 'Classification',
  label_set: ['normal', 'anomaly'],
  submitted_count: 4,
  remaining_count: 1,
});

const poolPayload = (
  overrides: Partial<LabelerJobPoolResponse>
): LabelerJobPoolResponse => ({
  job_id: JOB_ID,
  stealable_count: 0,
  job_complete: false,
  ...overrides,
});

/** Three ranked podium entries — places 1..3 (Requirement 8.3). */
const PODIUM: PodiumEntry[] = [
  {
    place: 1,
    user_id: 'labeler-a',
    email: 'labeler-a@example.com',
    submitted: 6,
    final_submitted_at: 1_700_000_100,
  },
  {
    place: 2,
    user_id: 'labeler-b',
    email: 'labeler-b@example.com',
    submitted: 4,
    final_submitted_at: 1_700_000_200,
  },
  {
    place: 3,
    user_id: 'labeler-c',
    submitted: 2,
    final_submitted_at: 1_700_000_300,
  },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Flush the pending promise chain (mock resolution → setState → render). */
const flushAsync = async () => {
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
};

/** Elements whose normalized text equals the take-work offer wording. */
function queryOfferText(count: number) {
  const expected = `Teammates still have ${count} unsubmitted ${
    count === 1 ? 'image' : 'images'
  }.`;
  return screen.queryAllByText((_, element) => {
    const text = (element?.textContent ?? '').replace(/\s+/g, ' ').trim();
    return text === expected;
  });
}

/** No error alert of the workspace is on screen. */
function expectNoErrorSurface() {
  expect(screen.queryByText('Failed to load task')).toBeNull();
  expect(screen.queryByText('Submission not saved')).toBeNull();
  expect(screen.queryByText('Failed to load jobs')).toBeNull();
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getLabelerJobs.mockResolvedValue({
    jobs: [
      {
        job_id: JOB_ID,
        job_name: 'Pool Examples Job',
        task_type: 'Classification',
        label_set: ['normal', 'anomaly'],
        submitted_count: 4,
        remaining_count: 0,
      },
    ],
    count: 1,
  });
});

// ---------------------------------------------------------------------------
// Examples (task 2.10)
// ---------------------------------------------------------------------------

describe('LabelerWorkspace completion-view pool examples (labeling-job-cleanup-work-stealing-and-podium)', () => {
  /**
   * Offer wiring: a completion payload with a pool of 3 stealable tasks
   * renders the take-work offer naming the count (Requirement 6.2), and
   * activating it issues exactly one Steal_Request followed by one
   * next-task load presenting the stolen task (Requirement 6.3).
   */
  it('offers the take-work control with the count and drives one steal then one next-task load', async () => {
    apiMocks.getNextTask
      .mockResolvedValueOnce(completionPayload())
      .mockResolvedValueOnce(stolenTaskPayload());
    apiMocks.getLabelerJobPool.mockResolvedValueOnce(
      poolPayload({ stealable_count: 3, job_complete: false })
    );
    apiMocks.stealTask.mockResolvedValueOnce({
      task_id: STOLEN_TASK_ID,
      job_id: JOB_ID,
      stolen_from: 'teammate-1',
      stealable_count: 2,
    });

    render(<LabelerWorkspace />);

    await screen.findByText('All done!');
    const button = await screen.findByTestId('steal-task-button');
    expect(queryOfferText(3).length).toBeGreaterThan(0);

    fireEvent.click(button);

    // The stolen task arrives through the existing next-task flow.
    const stub = await screen.findByTestId('annotation-canvas-stub');
    expect(stub.textContent).toContain(STOLEN_TASK_ID);
    expect(screen.queryByText('All done!')).toBeNull();

    // Exactly one steal then one next-task load (plus the entry load).
    expect(apiMocks.stealTask).toHaveBeenCalledTimes(1);
    expect(apiMocks.stealTask).toHaveBeenCalledWith(JOB_ID);
    expect(apiMocks.getNextTask).toHaveBeenCalledTimes(2);
    expect(apiMocks.getNextTask).toHaveBeenLastCalledWith(JOB_ID);
  });

  /**
   * Podium-on-complete: a pool reporting job_complete with podium entries
   * renders the Winner_Podium in place of the take-work offer
   * (Requirement 8.3).
   */
  it('renders the winner podium instead of the take-work offer when the job is complete', async () => {
    apiMocks.getNextTask.mockResolvedValueOnce(completionPayload());
    apiMocks.getLabelerJobPool.mockResolvedValueOnce(
      poolPayload({ stealable_count: 0, job_complete: true, podium: PODIUM })
    );

    render(<LabelerWorkspace />);

    await screen.findByText('All done!');
    await screen.findByTestId('winner-podium');
    expect(screen.getByTestId('podium-place-1')).toBeInTheDocument();
    expect(screen.getByTestId('podium-place-2')).toBeInTheDocument();
    expect(screen.getByTestId('podium-place-3')).toBeInTheDocument();

    // The take-work offer is absent and no steal is ever issued.
    expect(screen.queryByTestId('steal-task-button')).toBeNull();
    expect(apiMocks.stealTask).not.toHaveBeenCalled();
    expectNoErrorSurface();
  });

  /**
   * None-remain refresh: a Steal_Request answering 409 refetches the pool
   * and re-renders the completion view without any error indication
   * (Requirement 6.5).
   */
  it('refetches the pool with no error surface when the steal answers none-remain 409', async () => {
    apiMocks.getNextTask.mockResolvedValueOnce(completionPayload());
    apiMocks.getLabelerJobPool
      .mockResolvedValueOnce(
        poolPayload({ stealable_count: 2, job_complete: false })
      )
      .mockResolvedValueOnce(
        poolPayload({ stealable_count: 0, job_complete: false })
      );
    apiMocks.stealTask.mockRejectedValueOnce(
      new ApiError('No stealable tasks remain in this job', 409)
    );

    render(<LabelerWorkspace />);

    const button = await screen.findByTestId('steal-task-button');
    fireEvent.click(button);

    // The pool is refetched (Requirement 6.5) …
    await waitFor(() =>
      expect(apiMocks.getLabelerJobPool).toHaveBeenCalledTimes(2)
    );
    // … and the refreshed zero-stealable state drops the offer.
    await waitFor(() =>
      expect(screen.queryByTestId('steal-task-button')).toBeNull()
    );

    // No next-task load happened and no error alert is anywhere.
    expect(apiMocks.getNextTask).toHaveBeenCalledTimes(1);
    expect(screen.getByText('All done!')).toBeInTheDocument();
    expectNoErrorSurface();
  });

  /**
   * Pool-failure degradation: a failed pool fetch leaves the existing
   * completion view (All done! + Back to jobs) with no error surface, no
   * offer, and no podium (Requirement 6.6 posture).
   */
  it('degrades to the existing completion view when the pool fetch fails', async () => {
    apiMocks.getNextTask.mockResolvedValueOnce(completionPayload());
    apiMocks.getLabelerJobPool.mockRejectedValueOnce(
      new ApiError('The pool is unavailable', 500)
    );

    render(<LabelerWorkspace />);

    await screen.findByText('All done!');
    await waitFor(() =>
      expect(apiMocks.getLabelerJobPool).toHaveBeenCalledTimes(1)
    );
    await flushAsync();

    // The existing completion view renders unchanged.
    expect(screen.getByText('All done!')).toBeInTheDocument();
    expect(
      screen.getAllByRole('button', { name: 'Back to jobs' }).length
    ).toBeGreaterThan(0);
    expect(screen.queryByTestId('steal-task-button')).toBeNull();
    expect(screen.queryByTestId('winner-podium')).toBeNull();
    expectNoErrorSurface();
  });

  /**
   * Plain completion: zero stealable tasks with the job incomplete leave
   * the existing completion message untouched — no button, no podium, no
   * offer text (Requirement 6.6).
   */
  it('leaves the existing completion message untouched when zero stealable and job incomplete', async () => {
    apiMocks.getNextTask.mockResolvedValueOnce(completionPayload());
    apiMocks.getLabelerJobPool.mockResolvedValueOnce(
      poolPayload({ stealable_count: 0, job_complete: false })
    );

    render(<LabelerWorkspace />);

    await screen.findByText('All done!');
    await waitFor(() =>
      expect(apiMocks.getLabelerJobPool).toHaveBeenCalledTimes(1)
    );
    await flushAsync();

    // The plain completion message carries the submitted count.
    const expectedMessage =
      'You have completed all your labeling tasks in this job. You submitted 4 images.';
    expect(
      screen.queryAllByText((_, element) => {
        const text = (element?.textContent ?? '').replace(/\s+/g, ' ').trim();
        return text === expectedMessage;
      }).length
    ).toBeGreaterThan(0);

    // Nothing else was added to the completion surface.
    expect(screen.queryByTestId('steal-task-button')).toBeNull();
    expect(screen.queryByTestId('winner-podium')).toBeNull();
    expect(queryOfferText(0)).toHaveLength(0);
    expectNoErrorSurface();
  });
});
