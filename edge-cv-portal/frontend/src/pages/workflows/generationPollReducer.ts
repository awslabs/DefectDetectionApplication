/**
 * Pure poll-loop reducer for asynchronous workflow generation
 * (workflow-manager-gaps Requirements 4.1, 4.4, 4.6, 4.7).
 *
 * `GenerateChatPanel.tsx` submits a generation request and receives a
 * `job_id`; it then polls the status endpoint until the Generation_Job
 * reaches a terminal state. This module models that loop as a pure
 * state machine — `pollReducer(state, event)` — with every timing
 * decision injected through event timestamps, so the loop is fully
 * unit- and property-testable without timers or I/O:
 *
 *   - Polls are scheduled every {@link POLL_INTERVAL_MS} (3 s, within
 *     the ≤5 s bound of Req 4.1) via `nextPollAt`; the host issues a
 *     status request when that instant arrives.
 *   - Prompt submission is disabled exactly while a job is in flight
 *     (phase `polling`, Req 4.4) — see {@link isSubmissionDisabled}.
 *   - A terminal poll response stops the loop: `succeeded` carries the
 *     Generation_Result, `failed` carries the failure Error_Envelope.
 *   - Transport failures (network error or a non-success response that
 *     is not a job-failure envelope) increment a consecutive counter
 *     that resets on any successful poll; the third consecutive
 *     failure stops the loop (phase `transport-error`, Req 4.6).
 *   - A job still non-terminal {@link DEADLINE_MS} (300 s) after
 *     submission stops the loop (phase `timed-out`, Req 4.7). The
 *     deadline is enforced on every event timestamp, and the host can
 *     additionally dispatch a `tick` when its own deadline timer fires.
 *
 * The reducer is generic over the success/failure payload types so it
 * carries whatever the API client returns without importing it.
 */

// --------------------------------------------------------------------------
// Timing constants (all injected — the reducer never reads a clock)
// --------------------------------------------------------------------------

/** Interval between status polls; 3 s, within the ≤5 s bound (Req 4.1). */
export const POLL_INTERVAL_MS = 3_000;

/** Consecutive transport failures after which polling stops (Req 4.6). */
export const MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3;

/** Overall deadline for a job to reach a terminal state (Req 4.7). */
export const DEADLINE_MS = 300_000;

/** Message shown when polling stops on transport failures (Req 4.6). */
export const TRANSPORT_FAILURE_MESSAGE =
  'The generation status could not be retrieved. Your prompt is preserved below - you can retry.';

/** Message shown when polling stops on the overall deadline (Req 4.7). */
export const DEADLINE_MESSAGE =
  'Generation did not complete in time. Your prompt is preserved below - you can retry.';

// --------------------------------------------------------------------------
// State and events
// --------------------------------------------------------------------------

/**
 * Loop phases. `idle` before any submission; `polling` while the job is
 * in flight; the four remaining phases are stopped (absorbing) states:
 * `succeeded` / `failed` are job-terminal outcomes, `transport-error`
 * and `timed-out` are client-side stops (Req 4.6, 4.7).
 */
export type PollPhase =
  | 'idle'
  | 'polling'
  | 'succeeded'
  | 'failed'
  | 'transport-error'
  | 'timed-out';

export interface PollState<TResult = unknown, TFailure = unknown> {
  phase: PollPhase;
  /** The Generation_Job being polled; null before the first submission. */
  jobId: string | null;
  /** Timestamp (ms) of the accepted submission; anchors the deadline. */
  submittedAt: number | null;
  /**
   * When the host should issue the next status poll (ms); null when the
   * loop is stopped. Consecutive polls are POLL_INTERVAL_MS apart.
   */
  nextPollAt: number | null;
  /** Consecutive transport failures; resets on any successful poll. */
  consecutiveTransportFailures: number;
  /** The Generation_Result payload; set only in phase `succeeded`. */
  result: TResult | null;
  /** The failure Error_Envelope; set only in phase `failed`. */
  failure: TFailure | null;
}

export type PollEvent<TResult = unknown, TFailure = unknown> =
  /** The submit endpoint accepted the request (HTTP 202) at `at`. */
  | { type: 'submitted'; jobId: string; at: number }
  /** A status poll returned pending/running at `at`. */
  | { type: 'poll-in-progress'; at: number }
  /** A status poll returned the succeeded state with its result. */
  | { type: 'poll-succeeded'; result: TResult; at: number }
  /** A status poll returned the failed state with its Error_Envelope. */
  | { type: 'poll-failed'; failure: TFailure; at: number }
  /**
   * A status poll failed in transport: network error or a non-success
   * response that is not a Generation_Job failure envelope (Req 4.6).
   */
  | { type: 'poll-transport-failure'; at: number }
  /** A host timer tick; lets the deadline fire while a poll is in flight. */
  | { type: 'tick'; at: number };

/** The state before any submission. */
export function initialPollState<TResult = unknown, TFailure = unknown>(): PollState<
  TResult,
  TFailure
> {
  return {
    phase: 'idle',
    jobId: null,
    submittedAt: null,
    nextPollAt: null,
    consecutiveTransportFailures: 0,
    result: null,
    failure: null,
  };
}

// --------------------------------------------------------------------------
// Derived predicates
// --------------------------------------------------------------------------

/**
 * Prompt submission is disabled exactly while a Generation_Job is in
 * flight (Req 4.4): only the `polling` phase blocks resubmission.
 */
export function isSubmissionDisabled(state: PollState<unknown, unknown>): boolean {
  return state.phase === 'polling';
}

/** True in every stopped phase (job-terminal or client-side stop). */
export function isStopped(state: PollState<unknown, unknown>): boolean {
  return state.phase !== 'idle' && state.phase !== 'polling';
}

/**
 * The prompt is retained for retry on every non-success termination
 * (Req 4.3, 4.6, 4.7) and cleared only on success (Req 4.2).
 */
export function shouldRetainPrompt(state: PollState<unknown, unknown>): boolean {
  return isStopped(state) && state.phase !== 'succeeded';
}

/** The deadline instant for the in-flight job; null when not polling. */
export function deadlineAt(state: PollState<unknown, unknown>): number | null {
  return state.submittedAt === null ? null : state.submittedAt + DEADLINE_MS;
}

// --------------------------------------------------------------------------
// Reducer
// --------------------------------------------------------------------------

/**
 * Advance the poll loop by one event. Pure: no I/O, no clocks — every
 * timing input arrives on the event's `at` timestamp.
 *
 * Stopped phases are absorbing for everything except a new
 * `submitted` event, which starts a fresh loop (a resubmission after a
 * terminal outcome). While `polling`, a `submitted` event is ignored:
 * submission is disabled in flight (Req 4.4), so it cannot occur.
 */
export function pollReducer<TResult = unknown, TFailure = unknown>(
  state: PollState<TResult, TFailure>,
  event: PollEvent<TResult, TFailure>
): PollState<TResult, TFailure> {
  if (event.type === 'submitted') {
    if (state.phase === 'polling') {
      return state;
    }
    return {
      phase: 'polling',
      jobId: event.jobId,
      submittedAt: event.at,
      nextPollAt: event.at + POLL_INTERVAL_MS,
      consecutiveTransportFailures: 0,
      result: null,
      failure: null,
    };
  }

  // Every other event only matters while a job is in flight.
  if (state.phase !== 'polling' || state.submittedAt === null) {
    return state;
  }

  // Overall deadline (Req 4.7): a job that has not reached a terminal
  // state within DEADLINE_MS of submission stops the loop. Enforced on
  // every event timestamp, so a response observed past the deadline —
  // even a terminal one — cannot beat a deadline stop that should
  // already have fired.
  if (event.at >= state.submittedAt + DEADLINE_MS) {
    return { ...state, phase: 'timed-out', nextPollAt: null };
  }

  switch (event.type) {
    case 'tick':
      // In-deadline tick: nothing to do.
      return state;

    case 'poll-in-progress':
      // Successful poll: the failure counter resets (Req 4.6) and the
      // next poll is scheduled POLL_INTERVAL_MS out (Req 4.1).
      return {
        ...state,
        consecutiveTransportFailures: 0,
        nextPollAt: event.at + POLL_INTERVAL_MS,
      };

    case 'poll-succeeded':
      return {
        ...state,
        phase: 'succeeded',
        nextPollAt: null,
        consecutiveTransportFailures: 0,
        result: event.result,
        failure: null,
      };

    case 'poll-failed':
      return {
        ...state,
        phase: 'failed',
        nextPollAt: null,
        consecutiveTransportFailures: 0,
        result: null,
        failure: event.failure,
      };

    case 'poll-transport-failure': {
      const failures = state.consecutiveTransportFailures + 1;
      if (failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES) {
        // Third consecutive transport failure: stop (Req 4.6).
        return {
          ...state,
          phase: 'transport-error',
          nextPollAt: null,
          consecutiveTransportFailures: failures,
        };
      }
      return {
        ...state,
        consecutiveTransportFailures: failures,
        nextPollAt: event.at + POLL_INTERVAL_MS,
      };
    }
  }
}
