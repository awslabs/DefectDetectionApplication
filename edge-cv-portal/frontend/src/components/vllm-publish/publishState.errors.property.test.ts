/**
 * Property-based tests for the `publishState.ts` API error mapping
 * (vllm-package-publish-gui, Property 9): `toSessionError` folds every
 * invocation-failure shape — structured vLLM error envelopes with and
 * without `failed_step`, plain errors, network errors (fetch rejecting
 * with a TypeError), and 30-second `AbortSignal.timeout` aborts — into
 * the `SessionError` the page displays, and `publishReducer` +
 * `derivePanelState` surface it while re-enabling the initiating action.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { ApiError, VllmPublishedComponent } from '../../services/api';
import { UserRole } from '../../types';
import {
  derivePanelState,
  publishReducer,
  toSessionError,
  REQUEST_NOT_COMPLETED_MESSAGE,
  VLLM_PACKAGE_ROLES,
  type PackagedComponentEntry,
  type PublishSession,
  type SessionError,
  type VllmPublishRecord,
} from './publishState';

// ------------------------------------------------------------- generators

/** Non-empty backend error messages. */
const errorMessageArb: fc.Arbitrary<string> = fc.string({
  minLength: 1,
  maxLength: 60,
});

/** The `failed_step` values the vLLM packaging/publish envelopes record. */
const failedStepArb: fc.Arbitrary<string> = fc.constantFrom(
  'repository_generation',
  'artifact_upload',
  'record_update',
  'greengrass_registration'
);

/** HTTP statuses the packaging/publish paths return on failure. */
const errorStatusArb: fc.Arbitrary<number> = fc.constantFrom(
  400,
  403,
  409,
  500,
  502
);

/**
 * One invocation-failure scenario: the raw rejection value plus the
 * message/failing-step the page must display for it (the oracle derived
 * straight from the requirement text, not from the module under test).
 */
interface ErrorScenario {
  err: unknown;
  expected: { message: string; failedStep?: string };
}

/** Structured envelope carrying `failed_step`: api.ts `request()` rides
 *  the full parsed body along as `ApiError.details`, so the step is at
 *  `details.failed_step` (Req 3.1). */
const envelopeWithStepArb: fc.Arbitrary<ErrorScenario> = fc
  .tuple(errorMessageArb, failedStepArb, errorStatusArb)
  .map(([message, step, status]) => ({
    err: new ApiError(message, status, undefined, {
      error: message,
      failed_step: step,
    }),
    expected: { message, failedStep: step },
  }));

/** Structured envelope without `failed_step` (e.g. 400 validation, 403
 *  permission): only the message is displayed (Req 3.1, 3.5). */
const envelopeWithoutStepArb: fc.Arbitrary<ErrorScenario> = fc
  .tuple(errorMessageArb, errorStatusArb, fc.boolean())
  .map(([message, status, withDetails]) => ({
    err: new ApiError(
      message,
      status,
      undefined,
      withDetails ? { error: message } : undefined
    ),
    expected: { message },
  }));

/** Plain thrown errors surface their own message with no step. */
const plainErrorArb: fc.Arbitrary<ErrorScenario> = errorMessageArb.map(
  (message) => ({
    err: new Error(message),
    expected: { message },
  })
);

/** Network failures: fetch rejects with a TypeError → the
 *  request-did-not-complete message (Req 3.6). */
const networkErrorArb: fc.Arbitrary<ErrorScenario> = fc
  .constantFrom('Failed to fetch', 'NetworkError when attempting to fetch', 'Load failed')
  .map((message) => ({
    err: new TypeError(message),
    expected: { message: REQUEST_NOT_COMPLETED_MESSAGE },
  }));

/** 30-second request-cap aborts: `AbortSignal.timeout(REQUEST_TIMEOUT_MS)`
 *  rejects with a 'TimeoutError' (or 'AbortError' for manual aborts) —
 *  both map to the request-did-not-complete message (Req 3.6). */
const abortErrorArb: fc.Arbitrary<ErrorScenario> = fc
  .tuple(
    fc.constantFrom('AbortError', 'TimeoutError'),
    fc.constantFrom('The operation was aborted.', 'signal timed out')
  )
  .map(([name, message]) => ({
    err: Object.assign(new Error(message), { name }),
    expected: { message: REQUEST_NOT_COMPLETED_MESSAGE },
  }));

/** Every failure shape a packaging or publish-retry invocation can
 *  produce, weighted toward the structured envelopes the backend sends. */
const errorScenarioArb: fc.Arbitrary<ErrorScenario> = fc.oneof(
  { arbitrary: envelopeWithStepArb, weight: 3 },
  { arbitrary: envelopeWithoutStepArb, weight: 2 },
  { arbitrary: plainErrorArb, weight: 1 },
  { arbitrary: networkErrorArb, weight: 1 },
  { arbitrary: abortErrorArb, weight: 1 }
);

/** Roles permitted to package (the action must come back enabled for
 *  them after a failure, Req 3.1, 3.5, 3.6). */
const permittedRoleArb: fc.Arbitrary<UserRole> = fc.constantFrom(
  ...VLLM_PACKAGE_ROLES
);

/** Component versions in the backend's `N.0.0` progression. */
const versionArb: fc.Arbitrary<string> = fc
  .integer({ min: 1, max: 40 })
  .map((major) => `${major}.0.0`);

/** A `published_component` write-back as greengrass_publish.py records it. */
const publishedComponentArb: fc.Arbitrary<VllmPublishedComponent> = fc.record({
  component_name: fc
    .stringMatching(/^[a-z0-9-]{1,20}$/)
    .map((s) => `model-vllm-${s}`),
  component_version: versionArb,
  supported_architectures: fc.array(
    fc.constantFrom('arm64_jp6', 'arm64_jp5', 'arm64_jp7', 'x86_64'),
    { maxLength: 3 }
  ),
  runtime: fc.constant('vllm'),
  component_arns: fc.dictionary(
    fc.constantFrom('jetson-xavier-jp6', 'jetson-orin-jp6'),
    fc.string({ minLength: 1, maxLength: 40 }),
    { maxKeys: 2 }
  ),
  published_at: fc.integer({ min: 1_600_000_000_000, max: 1_900_000_000_000 }),
});

/** Successfully packaged entries only, so the record itself carries no
 *  failure and the displayed error is unambiguously the session's. */
const successfulEntryArb: fc.Arbitrary<PackagedComponentEntry> = fc.record({
  target: fc.constantFrom('jetson-xavier-jp6', 'jetson-orin-jp6'),
  status: fc.constant('packaged'),
});

/** Event timestamps in epoch ms. */
const nowArb: fc.Arbitrary<number> = fc.integer({
  min: 1_600_000_000_000,
  max: 1_900_000_000_000,
});

/** A vLLM record with no published_component (first-publish packaging
 *  path; also the publish-retry shape when entries are present). */
const unpublishedRecordArb: fc.Arbitrary<VllmPublishRecord> = fc
  .array(successfulEntryArb, { maxLength: 3 })
  .map((entries) => ({
    model_type: 'vllm',
    ...(entries.length > 0 ? { packaged_components: entries } : {}),
  }));

/** A vLLM record with a successfully packaged entry and no
 *  published_component — the shape that offers the publish-only retry
 *  action (Req 3.3). */
const retryableRecordArb: fc.Arbitrary<VllmPublishRecord> = fc
  .array(successfulEntryArb, { minLength: 1, maxLength: 3 })
  .map((entries) => ({
    model_type: 'vllm',
    packaged_components: entries,
  }));

/** A vLLM record with a published_component (confirmed re-publish path). */
const publishedRecordArb: fc.Arbitrary<VllmPublishRecord> = fc
  .tuple(unpublishedRecordArb, publishedComponentArb)
  .map(([record, published]) => ({
    ...record,
    published_component: published,
  }));

/**
 * One failing invocation walked through the real activation flow: the
 * record, the events that reach `requesting`, and the SessionError
 * source the controller would tag the failure with.
 */
interface InvocationPath {
  record: VllmPublishRecord;
  source: 'package' | 'publish-retry';
  activate: (now: number) => PublishSession;
}

function reduceToRequesting(
  events: Parameters<typeof publishReducer>[1][]
): PublishSession {
  let state: PublishSession = { kind: 'idle' };
  for (const event of events) {
    state = publishReducer(state, event).state;
  }
  return state;
}

/** The three ways an invocation can start: direct packaging on an
 *  unpublished record, confirmed re-publish, and the publish-only retry. */
const invocationPathArb: fc.Arbitrary<InvocationPath> = fc.oneof(
  unpublishedRecordArb.map((record) => ({
    record,
    source: 'package' as const,
    activate: (now: number) =>
      reduceToRequesting([{ type: 'ACTIVATE', record, now }]),
  })),
  publishedRecordArb.map((record) => ({
    record,
    source: 'package' as const,
    activate: (now: number) =>
      reduceToRequesting([
        { type: 'ACTIVATE', record, now },
        { type: 'CONFIRM', now },
      ]),
  })),
  retryableRecordArb.map((record) => ({
    record,
    source: 'publish-retry' as const,
    activate: (now: number) =>
      reduceToRequesting([{ type: 'ACTIVATE_PUBLISH_RETRY', record, now }]),
  }))
);

// ------------------------------------------------------------------ tests

/**
 * **Feature: vllm-package-publish-gui, Property 9: API error mapping surfaces message and failing step and re-enables the action**
 *
 * For any API error (structured envelope with or without `failed_step`,
 * plain error, network error, or 30-second abort) from a packaging or
 * publish-retry invocation, `toSessionError` + `derivePanelState` SHALL
 * yield a displayed error containing the error message (and the failing
 * step when present, or the request-did-not-complete message for
 * aborts/network errors) with the initiating action enabled again.
 *
 * **Validates: Requirements 3.1, 3.5, 3.6**
 */

describe('Property 9: API error mapping surfaces message and failing step and re-enables the action', () => {
  it(
    'toSessionError maps every failure shape to exactly the displayed ' +
      'message and failing step the oracle expects: envelope message + ' +
      'failed_step when recorded, plain-error messages verbatim, and the ' +
      'request-did-not-complete message for network errors and aborts',
    () => {
      fc.assert(
        fc.property(
          errorScenarioArb,
          fc.constantFrom('package' as const, 'publish-retry' as const),
          ({ err, expected }, source) => {
            const mapped = toSessionError(err, source);

            // The displayed message is the envelope/plain message, or
            // the request-did-not-complete text for aborts and network
            // failures (Req 3.1, 3.5, 3.6).
            expect(mapped.message).toBe(expected.message);

            // The failing step surfaces exactly when the envelope
            // recorded one (Req 3.1); never invented otherwise.
            expect(mapped.failedStep).toBe(expected.failedStep);

            // The initiating action is identified so the derivation can
            // re-enable it (Req 3.1, 3.5).
            expect(mapped.source).toBe(source);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'for any failing packaging or publish-retry invocation driven ' +
      'through the real activation flow, REQUEST_FAILED lands in a ' +
      'failed session whose error derivePanelState displays while the ' +
      'initiating action is enabled again and no longer loading',
    () => {
      fc.assert(
        fc.property(
          invocationPathArb,
          errorScenarioArb,
          permittedRoleArb,
          nowArb,
          (path, { err, expected }, role, now) => {
            // Reach `requesting` exactly as the UI does (ACTIVATE /
            // ACTIVATE+CONFIRM / ACTIVATE_PUBLISH_RETRY).
            const requesting = path.activate(now);
            expect(requesting.kind).toBe('requesting');

            // The controller maps the rejection and dispatches
            // REQUEST_FAILED (Req 3.1, 3.5, 3.6).
            const sessionError: SessionError = toSessionError(
              err,
              path.source
            );
            const { state: failed, commands } = publishReducer(requesting, {
              type: 'REQUEST_FAILED',
              error: sessionError,
            });
            expect(commands).toEqual([]);
            expect(failed).toEqual({ kind: 'failed', error: sessionError });

            const panel = derivePanelState(path.record, role, failed);

            // The failure is displayed with its message and, when the
            // envelope recorded one, the failing step (Req 3.1, 3.5);
            // aborts/network errors show the request-did-not-complete
            // message (Req 3.6).
            expect(panel.error).toBeDefined();
            expect(panel.error?.message).toBe(expected.message);
            expect(panel.error?.failedStep).toBe(expected.failedStep);

            // The Package_Publish_Action is enabled again for retry and
            // no longer loading (Req 3.1, 3.6).
            expect(panel.action.enabled).toBe(true);
            expect(panel.action.loading).toBe(false);

            // A failed publish-only retry re-enables that action too
            // (Req 3.5, 3.6) — it is offered by this record shape
            // (packaged success, no published_component, Req 3.3).
            if (path.source === 'publish-retry') {
              expect(panel.publishRetry).toBeDefined();
              expect(panel.publishRetry?.enabled).toBe(true);
              expect(panel.publishRetry?.loading).toBe(false);
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});
