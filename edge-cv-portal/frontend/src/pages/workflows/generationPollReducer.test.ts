/**
 * Example-based unit tests for the pure generation poll reducer
 * (workflow-manager-gaps Requirements 4.1, 4.4, 4.6, 4.7).
 *
 * The exhaustive input coverage lives in the fast-check property suite
 * (`generationPollReducer.property.test.ts`, Property 21); these tests
 * pin the concrete transitions the design names.
 */

import { describe, expect, it } from 'vitest';
import {
  DEADLINE_MS,
  MAX_CONSECUTIVE_TRANSPORT_FAILURES,
  POLL_INTERVAL_MS,
  initialPollState,
  isSubmissionDisabled,
  isStopped,
  pollReducer,
  shouldRetainPrompt,
  type PollEvent,
  type PollState,
} from './generationPollReducer';

/** Fold a sequence of events over the initial state. */
function run(events: PollEvent<string, string>[]): PollState<string, string> {
  return events.reduce(pollReducer, initialPollState<string, string>());
}

describe('generationPollReducer', () => {
  it('schedules the first poll 3 s after submission and disables submission', () => {
    const state = run([{ type: 'submitted', jobId: 'job-1', at: 1_000 }]);
    expect(state.phase).toBe('polling');
    expect(state.jobId).toBe('job-1');
    expect(state.nextPollAt).toBe(1_000 + POLL_INTERVAL_MS);
    expect(POLL_INTERVAL_MS).toBeLessThanOrEqual(5_000);
    expect(isSubmissionDisabled(state)).toBe(true);
  });

  it('keeps polling on in-progress responses, 3 s apart', () => {
    const state = run([
      { type: 'submitted', jobId: 'job-1', at: 0 },
      { type: 'poll-in-progress', at: 3_000 },
      { type: 'poll-in-progress', at: 6_000 },
    ]);
    expect(state.phase).toBe('polling');
    expect(state.nextPollAt).toBe(6_000 + POLL_INTERVAL_MS);
  });

  it('stops on terminal success carrying the result and clears the prompt retention', () => {
    const state = run([
      { type: 'submitted', jobId: 'job-1', at: 0 },
      { type: 'poll-succeeded', result: 'the-result', at: 3_000 },
    ]);
    expect(state.phase).toBe('succeeded');
    expect(state.result).toBe('the-result');
    expect(state.nextPollAt).toBeNull();
    expect(isStopped(state)).toBe(true);
    expect(isSubmissionDisabled(state)).toBe(false);
    expect(shouldRetainPrompt(state)).toBe(false);
  });

  it('stops on terminal failure carrying the envelope and retains the prompt', () => {
    const state = run([
      { type: 'submitted', jobId: 'job-1', at: 0 },
      { type: 'poll-failed', failure: 'the-envelope', at: 3_000 },
    ]);
    expect(state.phase).toBe('failed');
    expect(state.failure).toBe('the-envelope');
    expect(state.nextPollAt).toBeNull();
    expect(shouldRetainPrompt(state)).toBe(true);
  });

  it('stops on the third consecutive transport failure', () => {
    const failures: PollEvent<string, string>[] = [
      { type: 'submitted', jobId: 'job-1', at: 0 },
      { type: 'poll-transport-failure', at: 3_000 },
      { type: 'poll-transport-failure', at: 6_000 },
    ];
    const twoFailures = run(failures);
    expect(twoFailures.phase).toBe('polling');
    expect(twoFailures.consecutiveTransportFailures).toBe(2);

    const stopped = pollReducer(twoFailures, { type: 'poll-transport-failure', at: 9_000 });
    expect(stopped.phase).toBe('transport-error');
    expect(stopped.consecutiveTransportFailures).toBe(MAX_CONSECUTIVE_TRANSPORT_FAILURES);
    expect(stopped.nextPollAt).toBeNull();
    expect(shouldRetainPrompt(stopped)).toBe(true);
  });

  it('resets the transport-failure counter on any successful poll', () => {
    const state = run([
      { type: 'submitted', jobId: 'job-1', at: 0 },
      { type: 'poll-transport-failure', at: 3_000 },
      { type: 'poll-transport-failure', at: 6_000 },
      { type: 'poll-in-progress', at: 9_000 },
      { type: 'poll-transport-failure', at: 12_000 },
      { type: 'poll-transport-failure', at: 15_000 },
    ]);
    // Two failures, reset, then two more: still polling (never hit 3).
    expect(state.phase).toBe('polling');
    expect(state.consecutiveTransportFailures).toBe(2);
  });

  it('stops on the 300 s deadline even when the late response is terminal', () => {
    const submitted = run([{ type: 'submitted', jobId: 'job-1', at: 0 }]);

    const tickPastDeadline = pollReducer(submitted, { type: 'tick', at: DEADLINE_MS });
    expect(tickPastDeadline.phase).toBe('timed-out');
    expect(tickPastDeadline.nextPollAt).toBeNull();
    expect(shouldRetainPrompt(tickPastDeadline)).toBe(true);

    const lateSuccess = pollReducer(submitted, {
      type: 'poll-succeeded',
      result: 'late',
      at: DEADLINE_MS + 1,
    });
    expect(lateSuccess.phase).toBe('timed-out');
    expect(lateSuccess.result).toBeNull();
  });

  it('ignores poll events once stopped, and a new submission starts a fresh loop', () => {
    const stopped = run([
      { type: 'submitted', jobId: 'job-1', at: 0 },
      { type: 'poll-failed', failure: 'envelope', at: 3_000 },
    ]);
    const afterStrayPoll = pollReducer(stopped, { type: 'poll-in-progress', at: 6_000 });
    expect(afterStrayPoll).toEqual(stopped);

    const resubmitted = pollReducer(stopped, { type: 'submitted', jobId: 'job-2', at: 10_000 });
    expect(resubmitted.phase).toBe('polling');
    expect(resubmitted.jobId).toBe('job-2');
    expect(resubmitted.failure).toBeNull();
    expect(resubmitted.consecutiveTransportFailures).toBe(0);
  });
});
