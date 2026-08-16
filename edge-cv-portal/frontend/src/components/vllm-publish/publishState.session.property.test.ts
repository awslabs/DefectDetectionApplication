/**
 * Property-based tests for the `publishState.ts` session machinery
 * (vllm-package-publish-gui): the publish completion predicate and — in
 * later tasks — the activation protocol, polling session anchoring,
 * poll-tick outcomes, poll-failure absorption, retry failure clearing,
 * and the publish-only retry session (Properties 4–8, 10, 11).
 *
 * Shared generators live at the top of the file so subsequent property
 * describe blocks can be appended without duplication.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { VllmPublishedComponent } from '../../services/api';
import {
  isPublishComplete,
  publishReducer,
  POLL_TIMEOUT_MS,
  type Baseline,
  type PackagedComponentEntry,
  type PublishEvent,
  type PublishSession,
  type SessionError,
  type VllmPublishRecord,
} from './publishState';

// ------------------------------------------------------------- generators

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

/** One `packaged_components` entry as packaging.py writes it. */
const packagedEntryArb: fc.Arbitrary<PackagedComponentEntry> = fc
  .record({
    target: fc.constantFrom('jetson-xavier-jp6', 'jetson-orin-jp6', 'x86_64'),
    status: fc.constantFrom('packaged', 'failed', 'error', 'in_progress'),
    error: fc.option(fc.string({ minLength: 1, maxLength: 40 }), {
      nil: undefined,
    }),
    supported_architectures: fc.option(
      fc.array(fc.constantFrom('arm64_jp6', 'arm64_jp5'), { maxLength: 2 }),
      { nil: undefined }
    ),
  })
  .map(
    (entry) =>
      Object.fromEntries(
        Object.entries(entry).filter(([, value]) => value !== undefined)
      ) as unknown as PackagedComponentEntry
  );

/** vLLM records with and without packaged/published state. */
const vllmRecordArb: fc.Arbitrary<VllmPublishRecord> = fc
  .record({
    model_type: fc.constant('vllm'),
    packaged_components: fc.option(
      fc.array(packagedEntryArb, { maxLength: 4 }),
      { nil: undefined }
    ),
    published_component: fc.option(publishedComponentArb, { nil: undefined }),
  })
  .map(
    (record) =>
      Object.fromEntries(
        Object.entries(record).filter(([, value]) => value !== undefined)
      ) as unknown as VllmPublishRecord
  );

/** Baselines across the first-publish (null) and re-publish (version)
 *  cases (Req 2.1). */
const baselineArb: fc.Arbitrary<Baseline> = fc.option(versionArb, {
  nil: null,
});

/** A vLLM record guaranteed to carry the given published component. */
function withPublished(
  record: VllmPublishRecord,
  published: VllmPublishedComponent
): VllmPublishRecord {
  return { ...record, published_component: published };
}

/** A vLLM record guaranteed to carry no published component. */
function withoutPublished(record: VllmPublishRecord): VllmPublishRecord {
  const bare = { ...record };
  delete bare.published_component;
  return bare;
}

// ------------------------------------------------------------------ tests

/**
 * **Feature: vllm-package-publish-gui, Property 4: Publish completion predicate**
 *
 * For any baseline (a component version string or absent) and any polled
 * record, `isPublishComplete` SHALL return true if and only if the record
 * contains a `published_component` and either the baseline was absent or
 * the record's component version differs from the baseline. In
 * particular, a record still carrying the same version as the baseline is
 * never complete (re-publish detection).
 *
 * **Validates: Requirements 2.1, 2.3**
 *
 * The oracle is re-derived directly from the raw record fields and the
 * baseline definition — not via any module helper: complete iff
 * `published_component` exists and (baseline null or versions differ).
 */

describe('Property 4: Publish completion predicate', () => {
  it(
    'for any baseline and polled record, completion holds iff the record ' +
      'has a published_component and the baseline was absent or the ' +
      'versions differ',
    () => {
      // The pair generator deliberately over-weights the boundary cases:
      // no published_component, first publish (null baseline), re-publish
      // with the record still on the baseline version, and re-publish
      // with a changed version.
      const pairArb: fc.Arbitrary<{
        baseline: Baseline;
        record: VllmPublishRecord;
      }> = fc.oneof(
        // Polled record with no published_component yet, any baseline.
        fc
          .tuple(baselineArb, vllmRecordArb)
          .map(([baseline, record]) => ({
            baseline,
            record: withoutPublished(record),
          })),
        // First publish: absent baseline, record already published.
        fc
          .tuple(vllmRecordArb, publishedComponentArb)
          .map(([record, published]) => ({
            baseline: null,
            record: withPublished(record, published),
          })),
        // Re-publish, record still on the baseline version (never
        // complete).
        fc
          .tuple(vllmRecordArb, publishedComponentArb)
          .map(([record, published]) => ({
            baseline: published.component_version,
            record: withPublished(record, published),
          })),
        // Present baseline against an arbitrary published version
        // (differing or, occasionally, colliding by chance).
        fc
          .tuple(baselineArb, vllmRecordArb, publishedComponentArb)
          .map(([baseline, record, published]) => ({
            baseline,
            record: withPublished(record, published),
          }))
      );

      fc.assert(
        fc.property(pairArb, ({ baseline, record }) => {
          // Independent oracle straight off the definition (Req 2.1, 2.3).
          const published = record.published_component;
          const expected =
            published !== undefined &&
            (baseline === null ||
              published.component_version !== baseline);

          expect(isPublishComplete(baseline, record)).toBe(expected);
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'a record still carrying the same version as a present baseline is ' +
      'never complete (re-publish detection)',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          publishedComponentArb,
          (record, published) => {
            const polled = withPublished(record, published);
            // Baseline captured at re-publish invocation time equals the
            // record's current version → not complete until the version
            // changes (Req 2.3).
            expect(
              isPublishComplete(published.component_version, polled)
            ).toBe(false);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'an absent baseline (first publish) completes as soon as any ' +
      'published_component appears, and never before',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          publishedComponentArb,
          (record, published) => {
            // No published_component yet → not complete (Req 2.1).
            expect(isPublishComplete(null, withoutPublished(record))).toBe(
              false
            );
            // Any published_component at all → complete (Req 2.3).
            expect(
              isPublishComplete(null, withPublished(record, published))
            ).toBe(true);
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

// -------------------------------------------- generators (Property 5)

/** Event timestamps in the same epoch-ms range as `published_at`. */
const nowArb: fc.Arbitrary<number> = fc.integer({
  min: 1_600_000_000_000,
  max: 1_900_000_000_000,
});

/** Session errors as produced by `toSessionError` / `recordFailure`. */
const sessionErrorArb: fc.Arbitrary<SessionError> = fc
  .record({
    message: fc.string({ minLength: 1, maxLength: 40 }),
    failedStep: fc.option(
      fc.constantFrom(
        'repository_generation',
        'artifact_upload',
        'record_update',
        'greengrass_registration'
      ),
      { nil: undefined }
    ),
    source: fc.constantFrom(
      'package' as const,
      'publish-retry' as const,
      'record' as const
    ),
  })
  .map(
    (error) =>
      Object.fromEntries(
        Object.entries(error).filter(([, value]) => value !== undefined)
      ) as unknown as SessionError
  );

/** In-flight sessions: the states that must reject further activations
 *  (Req 1.5, 3.7). */
const inFlightSessionArb: fc.Arbitrary<PublishSession> = fc.oneof(
  fc.record({
    kind: fc.constant('requesting' as const),
    action: fc.constantFrom('package' as const, 'publish-retry' as const),
    baseline: baselineArb,
  }),
  fc.record({
    kind: fc.constant('confirming' as const),
    baseline: baselineArb,
  }),
  fc.tuple(baselineArb, nowArb).map(([baseline, startedAt]) => ({
    kind: 'polling' as const,
    baseline,
    startedAt,
    deadline: startedAt + POLL_TIMEOUT_MS,
  }))
);

/** Any reducer event, biased toward the activation protocol
 *  (`ACTIVATE` / `CONFIRM` / `CANCEL_CONFIRM` / `ACTIVATE_PUBLISH_RETRY`)
 *  while still interleaving request and poll lifecycle events so the
 *  walk reaches every session state. */
const activationEventArb: fc.Arbitrary<PublishEvent> = fc.oneof(
  { arbitrary: activateEventArb(), weight: 4 },
  {
    arbitrary: nowArb.map((now) => ({ type: 'CONFIRM' as const, now })),
    weight: 3,
  },
  {
    arbitrary: fc.constant({ type: 'CANCEL_CONFIRM' as const }),
    weight: 1,
  },
  {
    arbitrary: fc
      .tuple(vllmRecordArb, nowArb)
      .map(([record, now]) => ({
        type: 'ACTIVATE_PUBLISH_RETRY' as const,
        record,
        now,
      })),
    weight: 3,
  },
  {
    arbitrary: nowArb.map((now) => ({
      type: 'REQUEST_SUCCEEDED' as const,
      now,
    })),
    weight: 2,
  },
  {
    arbitrary: sessionErrorArb.map((error) => ({
      type: 'REQUEST_FAILED' as const,
      error,
    })),
    weight: 1,
  },
  {
    arbitrary: fc
      .tuple(vllmRecordArb, nowArb)
      .map(([record, now]) => ({
        type: 'POLL_RESULT' as const,
        record,
        now,
      })),
    weight: 1,
  },
  {
    arbitrary: nowArb.map((now) => ({ type: 'POLL_FAILED' as const, now })),
    weight: 1,
  }
);

/** `ACTIVATE` events over records with and without a published
 *  component, both cases weighted equally so confirmation gating is
 *  exercised as often as the direct-invoke path. */
function activateEventArb(): fc.Arbitrary<PublishEvent> {
  return fc.oneof(
    fc
      .tuple(vllmRecordArb, nowArb)
      .map(([record, now]) => ({
        type: 'ACTIVATE' as const,
        record: withoutPublished(record),
        now,
      })),
    fc
      .tuple(vllmRecordArb, publishedComponentArb, nowArb)
      .map(([record, published, now]) => ({
        type: 'ACTIVATE' as const,
        record: withPublished(record, published),
        now,
      }))
  );
}

/** True for the states in which further activations must be rejected. */
function sessionInFlight(state: PublishSession): boolean {
  return (
    state.kind === 'requesting' ||
    state.kind === 'confirming' ||
    state.kind === 'polling'
  );
}

/**
 * **Feature: vllm-package-publish-gui, Property 5: Activation protocol — confirmation gating and single in-flight invocation**
 *
 * For any record and any sequence of activation events, the reducer
 * SHALL emit an `INVOKE_PACKAGING` command only via `ACTIVATE` on a
 * record without a `published_component`, or via `CONFIRM` following an
 * `ACTIVATE` on a record with one (never directly from that `ACTIVATE`);
 * and while a session is in `requesting`, `confirming`, or `polling`,
 * any further `ACTIVATE` or `ACTIVATE_PUBLISH_RETRY` events SHALL emit
 * no commands and change no state — so at most one API invocation is in
 * flight per record at any time.
 *
 * **Validates: Requirements 1.2, 1.5, 1.7, 3.7**
 */

describe('Property 5: Activation protocol — confirmation gating and single in-flight invocation', () => {
  it(
    'over any event sequence from idle, INVOKE_PACKAGING is emitted only ' +
      'by ACTIVATE on an unpublished record outside an in-flight session ' +
      'or by CONFIRM from confirming, in-flight activations are complete ' +
      'no-ops, and every invocation enters requesting from a ' +
      'non-in-flight request state',
    () => {
      fc.assert(
        fc.property(
          fc.array(activationEventArb, { minLength: 1, maxLength: 25 }),
          (events) => {
            let state: PublishSession = { kind: 'idle' };

            for (const event of events) {
              const before = state;
              const inFlightBefore = sessionInFlight(before);
              const { state: next, commands } = publishReducer(before, event);

              const invocations = commands.filter(
                (command) =>
                  command.type === 'INVOKE_PACKAGING' ||
                  command.type === 'INVOKE_PUBLISH'
              );

              // A single transition never fires more than one API
              // invocation (Req 1.2 "exactly once" per activation).
              expect(invocations.length).toBeLessThanOrEqual(1);

              // While requesting/confirming/polling, further activations
              // change no state and emit no commands (Req 1.5, 3.7).
              if (
                inFlightBefore &&
                (event.type === 'ACTIVATE' ||
                  event.type === 'ACTIVATE_PUBLISH_RETRY')
              ) {
                expect(next).toEqual(before);
                expect(commands).toEqual([]);
              }

              // INVOKE_PACKAGING is reachable only via the two legal
              // doors (Req 1.2, 1.7).
              if (
                commands.some(
                  (command) => command.type === 'INVOKE_PACKAGING'
                )
              ) {
                const viaDirectActivate =
                  event.type === 'ACTIVATE' &&
                  !inFlightBefore &&
                  event.record.published_component === undefined;
                const viaConfirm =
                  event.type === 'CONFIRM' && before.kind === 'confirming';
                expect(viaDirectActivate || viaConfirm).toBe(true);
              }

              // Any invocation command starts a fresh request: it never
              // fires while a request or poll is already in flight, and
              // it always lands in `requesting` — so at most one API
              // invocation is in flight at any time (Req 1.5, 3.7).
              if (invocations.length === 1) {
                expect(
                  before.kind === 'requesting' || before.kind === 'polling'
                ).toBe(false);
                expect(next.kind).toBe('requesting');
              }

              state = next;
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'ACTIVATE on a record with a published_component never invokes ' +
      'directly: it opens confirmation with the captured baseline, and ' +
      'only CONFIRM then emits exactly one INVOKE_PACKAGING (CANCEL ' +
      'returns to idle with no commands)',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          publishedComponentArb,
          nowArb,
          nowArb,
          fc.boolean(),
          (record, published, activateNow, decideNow, confirms) => {
            const publishedRecord = withPublished(record, published);

            // Activation gates on confirmation: no invoke yet (Req 1.7),
            // baseline captured from the record (Req 2.1 capture rule).
            const gated = publishReducer(
              { kind: 'idle' },
              { type: 'ACTIVATE', record: publishedRecord, now: activateNow }
            );
            expect(gated.commands).toEqual([]);
            expect(gated.state).toEqual({
              kind: 'confirming',
              baseline: published.component_version,
            });

            if (confirms) {
              // Only after explicit confirmation does packaging get
              // invoked, exactly once (Req 1.2, 1.7).
              const confirmed = publishReducer(gated.state, {
                type: 'CONFIRM',
                now: decideNow,
              });
              expect(confirmed.commands).toEqual([
                { type: 'INVOKE_PACKAGING' },
              ]);
              expect(confirmed.state).toEqual({
                kind: 'requesting',
                action: 'package',
                baseline: published.component_version,
              });
            } else {
              // Cancelling invokes nothing (Req 1.7).
              const cancelled = publishReducer(gated.state, {
                type: 'CANCEL_CONFIRM',
              });
              expect(cancelled.commands).toEqual([]);
              expect(cancelled.state).toEqual({ kind: 'idle' });
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'for any in-flight session, ACTIVATE and ACTIVATE_PUBLISH_RETRY are ' +
      'rejected: identical state, zero commands',
    () => {
      fc.assert(
        fc.property(
          inFlightSessionArb,
          vllmRecordArb,
          nowArb,
          fc.boolean(),
          (session, record, now, viaRetry) => {
            const event: PublishEvent = viaRetry
              ? { type: 'ACTIVATE_PUBLISH_RETRY', record, now }
              : { type: 'ACTIVATE', record, now };

            const { state, commands } = publishReducer(session, event);

            // No state change and no commands while in flight — at most
            // one API invocation per record (Req 1.5, 3.7).
            expect(state).toEqual(session);
            expect(commands).toEqual([]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

/**
 * **Feature: vllm-package-publish-gui, Property 6: Successful invocation starts an anchored polling session with the invocation-time baseline**
 *
 * For any record and any invocation time, dispatching `REQUEST_SUCCEEDED`
 * after an activation SHALL transition to `polling` with `baseline` equal
 * to the record's published component version at activation time (or null
 * if absent), `startedAt` equal to the success time, and `deadline`
 * exactly `POLL_TIMEOUT_MS` after it, emitting `START_POLLING`.
 *
 * **Validates: Requirements 2.1**
 */

describe('Property 6: Successful invocation starts an anchored polling session with the invocation-time baseline', () => {
  it(
    'direct activation on an unpublished record followed by ' +
      'REQUEST_SUCCEEDED anchors polling at the success time with a null ' +
      'baseline and emits START_POLLING',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          nowArb,
          nowArb,
          (record, activateNow, successNow) => {
            // Activation on a record with no published_component invokes
            // directly and captures the absent baseline (Req 2.1).
            const activated = publishReducer(
              { kind: 'idle' },
              {
                type: 'ACTIVATE',
                record: withoutPublished(record),
                now: activateNow,
              }
            );
            expect(activated.state.kind).toBe('requesting');

            const { state, commands } = publishReducer(activated.state, {
              type: 'REQUEST_SUCCEEDED',
              now: successNow,
            });

            // Anchored session: null baseline, startedAt = success time,
            // deadline exactly POLL_TIMEOUT_MS later (Req 2.1).
            expect(state).toEqual({
              kind: 'polling',
              baseline: null,
              startedAt: successNow,
              deadline: successNow + POLL_TIMEOUT_MS,
            });
            expect(commands).toEqual([{ type: 'START_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'confirmed re-publish activation followed by REQUEST_SUCCEEDED ' +
      'anchors polling with the published component version at ' +
      'activation time as the baseline',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          publishedComponentArb,
          nowArb,
          nowArb,
          nowArb,
          (record, published, activateNow, confirmNow, successNow) => {
            const publishedRecord = withPublished(record, published);

            // ACTIVATE on a published record gates on confirmation with
            // the baseline captured at activation time (Req 1.7, 2.1).
            const gated = publishReducer(
              { kind: 'idle' },
              { type: 'ACTIVATE', record: publishedRecord, now: activateNow }
            );
            const confirmed = publishReducer(gated.state, {
              type: 'CONFIRM',
              now: confirmNow,
            });
            expect(confirmed.state.kind).toBe('requesting');

            const { state, commands } = publishReducer(confirmed.state, {
              type: 'REQUEST_SUCCEEDED',
              now: successNow,
            });

            // The baseline is the version present at activation time —
            // not absent, not any later value (Req 2.1).
            expect(state).toEqual({
              kind: 'polling',
              baseline: published.component_version,
              startedAt: successNow,
              deadline: successNow + POLL_TIMEOUT_MS,
            });
            expect(commands).toEqual([{ type: 'START_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'publish-only retry activation followed by REQUEST_SUCCEEDED anchors ' +
      'polling with the record baseline at activation time',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          nowArb,
          nowArb,
          (record, activateNow, successNow) => {
            const activated = publishReducer(
              { kind: 'idle' },
              { type: 'ACTIVATE_PUBLISH_RETRY', record, now: activateNow }
            );
            expect(activated.state.kind).toBe('requesting');

            const { state, commands } = publishReducer(activated.state, {
              type: 'REQUEST_SUCCEEDED',
              now: successNow,
            });

            expect(state).toEqual({
              kind: 'polling',
              baseline: record.published_component?.component_version ?? null,
              startedAt: successNow,
              deadline: successNow + POLL_TIMEOUT_MS,
            });
            expect(commands).toEqual([{ type: 'START_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'for any requesting session, REQUEST_SUCCEEDED preserves the ' +
      'captured baseline and anchors startedAt/deadline to the success ' +
      'time, emitting exactly START_POLLING',
    () => {
      fc.assert(
        fc.property(
          baselineArb,
          fc.constantFrom('package' as const, 'publish-retry' as const),
          nowArb,
          (baseline, action, successNow) => {
            const requesting: PublishSession = {
              kind: 'requesting',
              action,
              baseline,
            };

            const { state, commands } = publishReducer(requesting, {
              type: 'REQUEST_SUCCEEDED',
              now: successNow,
            });

            expect(state).toEqual({
              kind: 'polling',
              baseline,
              startedAt: successNow,
              deadline: successNow + POLL_TIMEOUT_MS,
            });
            expect(commands).toEqual([{ type: 'START_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

// -------------------------------------------- generators (Property 7)

/** A `polling` session, always anchored `deadline = startedAt +
 *  POLL_TIMEOUT_MS` as the reducer creates it. */
type PollingSession = Extract<PublishSession, { kind: 'polling' }>;

function makePollingSession(
  baseline: Baseline,
  startedAt: number
): PollingSession {
  return {
    kind: 'polling',
    baseline,
    startedAt,
    deadline: startedAt + POLL_TIMEOUT_MS,
  };
}

/** Tick offsets from the poll start spanning strictly-before, exactly-at,
 *  and past the deadline so every time branch is exercised. */
const tickOffsetArb: fc.Arbitrary<number> = fc.oneof(
  fc.integer({ min: 0, max: POLL_TIMEOUT_MS - 1 }),
  fc.constant(POLL_TIMEOUT_MS),
  fc.integer({ min: POLL_TIMEOUT_MS + 1, max: 2 * POLL_TIMEOUT_MS })
);

/** A packaged entry guaranteed successful ('packaged'). */
const successfulEntryArb: fc.Arbitrary<PackagedComponentEntry> =
  packagedEntryArb.map((entry) => ({ ...entry, status: 'packaged' }));

/** A packaged entry guaranteed to be a recorded failure (any
 *  non-'packaged' status packaging.py could leave behind). */
const failedEntryArb: fc.Arbitrary<PackagedComponentEntry> = fc
  .tuple(packagedEntryArb, fc.constantFrom('failed', 'error', 'in_progress'))
  .map(([entry, status]) => ({ ...entry, status }));

/** Records that can neither complete nor carry a recorded failure: no
 *  published_component and only successfully packaged entries — the pure
 *  deadline / continue branches. */
const nonTerminatingRecordArb: fc.Arbitrary<VllmPublishRecord> = fc
  .array(successfulEntryArb, { maxLength: 3 })
  .map((entries) => ({
    model_type: 'vllm',
    ...(entries.length > 0 ? { packaged_components: entries } : {}),
  }));

/** One poll tick: an anchored polling session, the polled record, and
 *  the tick time. Variants are weighted so completion, same-version
 *  re-publish (never complete), recorded failure, deadline, and continue
 *  outcomes all occur. */
interface PollTickScenario {
  session: PollingSession;
  record: VllmPublishRecord;
  tickNow: number;
}

const pollTickScenarioArb: fc.Arbitrary<PollTickScenario> = fc.oneof(
  // Published component present: completes unless the baseline matches.
  fc
    .tuple(vllmRecordArb, publishedComponentArb, baselineArb, nowArb, tickOffsetArb)
    .map(([record, published, baseline, startedAt, offset]) => ({
      session: makePollingSession(baseline, startedAt),
      record: withPublished(record, published),
      tickNow: startedAt + offset,
    })),
  // Same-version re-publish: published present but never complete, so
  // the failure / deadline / continue branches stay reachable.
  fc
    .tuple(vllmRecordArb, publishedComponentArb, nowArb, tickOffsetArb)
    .map(([record, published, startedAt, offset]) => ({
      session: makePollingSession(published.component_version, startedAt),
      record: withPublished(record, published),
      tickNow: startedAt + offset,
    })),
  // No published component, arbitrary packaged entries: recorded
  // failure / deadline / continue.
  fc
    .tuple(vllmRecordArb, baselineArb, nowArb, tickOffsetArb)
    .map(([record, baseline, startedAt, offset]) => ({
      session: makePollingSession(baseline, startedAt),
      record: withoutPublished(record),
      tickNow: startedAt + offset,
    })),
  // No published component and only successful entries: pure deadline /
  // continue.
  fc
    .tuple(nonTerminatingRecordArb, baselineArb, nowArb, tickOffsetArb)
    .map(([record, baseline, startedAt, offset]) => ({
      session: makePollingSession(baseline, startedAt),
      record,
      tickNow: startedAt + offset,
    }))
);

/**
 * **Feature: vllm-package-publish-gui, Property 7: Poll-tick outcomes**
 *
 * For any polling session and any polled record: if the completion
 * predicate holds, the reducer SHALL transition to `published` and stop
 * polling; otherwise, if the record carries a failed packaged entry, it
 * SHALL transition to `failed` carrying the recorded failing step and
 * message and stop polling (leaving the action re-enabled); otherwise,
 * if the tick time is at or past the deadline, it SHALL transition to
 * `timed-out` with the pending message and stop polling; otherwise it
 * SHALL remain `polling` with an unchanged deadline.
 *
 * **Validates: Requirements 2.2, 2.3, 2.5, 3.2**
 *
 * The oracle re-derives each branch straight off the raw record fields
 * and the tick time — completion iff a `published_component` exists and
 * is new or version-changed against the session baseline; recorded
 * failure iff any packaged entry's status is not 'packaged' — applying
 * the reducer's documented branch order: completion first, then record
 * failure, then deadline, then continue.
 */

describe('Property 7: Poll-tick outcomes', () => {
  it(
    'for any polling session and polled record, POLL_RESULT lands on ' +
      'exactly the branch the independent oracle selects: published / ' +
      'failed / timed-out / continue',
    () => {
      fc.assert(
        fc.property(pollTickScenarioArb, ({ session, record, tickNow }) => {
          const { state, commands } = publishReducer(session, {
            type: 'POLL_RESULT',
            record,
            now: tickNow,
          });

          // Independent oracle from the raw record and branch order.
          const published = record.published_component;
          const complete =
            published !== undefined &&
            (session.baseline === null ||
              published.component_version !== session.baseline);
          const failingEntry = (record.packaged_components ?? []).find(
            (entry) => entry.status !== 'packaged'
          );

          if (complete) {
            // Completion observed → published, polling stops (Req 2.3).
            expect(state).toEqual({ kind: 'published', component: published });
            expect(commands).toEqual([{ type: 'STOP_POLLING' }]);
          } else if (failingEntry) {
            // Recorded failure → failed with the recorded step and
            // message, polling stops (Req 3.2).
            expect(state.kind).toBe('failed');
            if (state.kind === 'failed') {
              expect(state.error.source).toBe('record');
              expect(state.error.failedStep).toContain(failingEntry.target);
              expect(state.error.message).toBe(
                failingEntry.error ??
                  `Packaging failed for target ${failingEntry.target}`
              );
            }
            expect(commands).toEqual([{ type: 'STOP_POLLING' }]);
          } else if (tickNow >= session.deadline) {
            // At/past the deadline without completion → timed-out,
            // polling stops (Req 2.5).
            expect(state).toEqual({ kind: 'timed-out' });
            expect(commands).toEqual([{ type: 'STOP_POLLING' }]);
          } else {
            // Otherwise keep polling with the identical session — the
            // deadline never moves (Req 2.2, 2.5).
            expect(state).toEqual(session);
            expect(commands).toEqual([]);
          }
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'branch order: completion wins over a recorded failure and a lapsed ' +
      'deadline, and a recorded failure wins over a lapsed deadline',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          publishedComponentArb,
          failedEntryArb,
          baselineArb,
          nowArb,
          fc.integer({ min: 0, max: POLL_TIMEOUT_MS }),
          (record, published, failedEntry, rawBaseline, startedAt, extra) => {
            // Normalize the baseline so completion is guaranteed.
            const baseline =
              rawBaseline === published.component_version ? null : rawBaseline;
            const session = makePollingSession(baseline, startedAt);
            const pastDeadline = session.deadline + extra;

            // Record that is simultaneously complete, carrying a failed
            // entry, observed past the deadline → completion wins.
            const completingRecord: VllmPublishRecord = {
              ...withPublished(record, published),
              packaged_components: [
                failedEntry,
                ...(record.packaged_components ?? []),
              ],
            };
            const won = publishReducer(session, {
              type: 'POLL_RESULT',
              record: completingRecord,
              now: pastDeadline,
            });
            expect(won.state).toEqual({
              kind: 'published',
              component: published,
            });
            expect(won.commands).toEqual([{ type: 'STOP_POLLING' }]);

            // Same record without completion → the recorded failure
            // beats the lapsed deadline (Req 3.2 over Req 2.5).
            const failingRecord: VllmPublishRecord = {
              ...withoutPublished(record),
              packaged_components: [
                failedEntry,
                ...(record.packaged_components ?? []),
              ],
            };
            const failed = publishReducer(session, {
              type: 'POLL_RESULT',
              record: failingRecord,
              now: pastDeadline,
            });
            expect(failed.state.kind).toBe('failed');
            expect(failed.commands).toEqual([{ type: 'STOP_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'a non-completing, non-failing tick at or past the deadline — ' +
      'including exactly at it — times out and stops polling',
    () => {
      fc.assert(
        fc.property(
          nonTerminatingRecordArb,
          baselineArb,
          nowArb,
          fc.oneof(
            fc.constant(0),
            fc.integer({ min: 0, max: POLL_TIMEOUT_MS })
          ),
          (record, baseline, startedAt, extra) => {
            const session = makePollingSession(baseline, startedAt);

            const { state, commands } = publishReducer(session, {
              type: 'POLL_RESULT',
              record,
              now: session.deadline + extra,
            });

            // Deadline reached without completion → timed-out (Req 2.5).
            expect(state).toEqual({ kind: 'timed-out' });
            expect(commands).toEqual([{ type: 'STOP_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'a non-completing, non-failing tick strictly before the deadline ' +
      'continues polling with the identical session and no commands',
    () => {
      fc.assert(
        fc.property(
          nonTerminatingRecordArb,
          baselineArb,
          nowArb,
          fc.integer({ min: 0, max: POLL_TIMEOUT_MS - 1 }),
          (record, baseline, startedAt, offset) => {
            const session = makePollingSession(baseline, startedAt);

            const { state, commands } = publishReducer(session, {
              type: 'POLL_RESULT',
              record,
              now: startedAt + offset,
            });

            // Continue branch: unchanged session — same baseline, same
            // startedAt, and crucially the same deadline (Req 2.2, 2.5).
            expect(state).toEqual(session);
            expect(commands).toEqual([]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

// -------------------------------------------- generators (Property 8)

/** One absorbed poll tick: either a failed poll or a successful poll
 *  whose record neither completes nor carries a recorded failure —
 *  the only two shapes Property 8 quantifies over. `offset` is the tick
 *  time relative to the session's `startedAt`. */
interface AbsorptionStep {
  kind: 'POLL_FAILED' | 'POLL_RESULT';
  record?: VllmPublishRecord;
  offset: number;
}

/** Non-completing, non-failing polled records for a given session
 *  baseline: either no `published_component` with only successfully
 *  packaged entries, or — for a present baseline — a record still on
 *  the exact baseline version (a same-version re-publish record is
 *  never complete, Req 2.3). */
function nonCompletingRecordArb(
  baseline: Baseline
): fc.Arbitrary<VllmPublishRecord> {
  if (baseline === null) {
    return nonTerminatingRecordArb;
  }
  return fc.oneof(
    nonTerminatingRecordArb,
    fc
      .tuple(nonTerminatingRecordArb, publishedComponentArb)
      .map(([record, published]) => ({
        ...record,
        published_component: {
          ...published,
          component_version: baseline,
        },
      }))
  );
}

/** One absorption step with a tick offset drawn from `offsetArb`. */
function absorptionStepArb(
  baseline: Baseline,
  offsetArb: fc.Arbitrary<number>
): fc.Arbitrary<AbsorptionStep> {
  return fc.oneof(
    offsetArb.map((offset) => ({
      kind: 'POLL_FAILED' as const,
      offset,
    })),
    fc
      .tuple(nonCompletingRecordArb(baseline), offsetArb)
      .map(([record, offset]) => ({
        kind: 'POLL_RESULT' as const,
        record,
        offset,
      }))
  );
}

/** The reducer event for an absorption step at absolute time `now`. */
function absorptionEvent(step: AbsorptionStep, now: number): PublishEvent {
  return step.kind === 'POLL_FAILED'
    ? { type: 'POLL_FAILED', now }
    : { type: 'POLL_RESULT', record: step.record as VllmPublishRecord, now };
}

/** Tick offsets strictly before the deadline (absorbed region). */
const beforeDeadlineOffsetArb: fc.Arbitrary<number> = fc.integer({
  min: 0,
  max: POLL_TIMEOUT_MS - 1,
});

/** Tick offsets at or past the deadline — including exactly at it. */
const atOrPastDeadlineOffsetArb: fc.Arbitrary<number> = fc.oneof(
  fc.constant(POLL_TIMEOUT_MS),
  fc.integer({ min: POLL_TIMEOUT_MS, max: 2 * POLL_TIMEOUT_MS })
);

/** An anchored polling session plus an arbitrary interleaving of
 *  failed polls and non-completing poll results, every tick strictly
 *  before the deadline. */
interface AbsorptionScenario {
  session: PollingSession;
  steps: AbsorptionStep[];
}

const absorptionScenarioArb: fc.Arbitrary<AbsorptionScenario> = fc
  .tuple(baselineArb, nowArb)
  .chain(([baseline, startedAt]) =>
    fc
      .array(absorptionStepArb(baseline, beforeDeadlineOffsetArb), {
        minLength: 1,
        maxLength: 30,
      })
      .map((steps) => ({
        session: makePollingSession(baseline, startedAt),
        steps,
      }))
  );

/**
 * **Feature: vllm-package-publish-gui, Property 8: Poll failures are absorbed without extending the timeout**
 *
 * For any polling session and any sequence of `POLL_FAILED` and
 * non-completing `POLL_RESULT` events, the session SHALL remain
 * `polling` with the original deadline unchanged and no failure
 * surfaced, until an event at or past the deadline transitions it to
 * `timed-out`; the number and order of failed polls SHALL never alter
 * the deadline.
 *
 * **Validates: Requirements 2.5, 2.7**
 */

describe('Property 8: Poll failures are absorbed without extending the timeout', () => {
  it(
    'any interleaving of POLL_FAILED and non-completing POLL_RESULT ' +
      'events strictly before the deadline leaves the session ' +
      'byte-identical at every step — same deadline, no failure ' +
      'surfaced, no commands',
    () => {
      fc.assert(
        fc.property(absorptionScenarioArb, ({ session, steps }) => {
          let state: PublishSession = session;

          for (const step of steps) {
            const { state: next, commands } = publishReducer(
              state,
              absorptionEvent(step, session.startedAt + step.offset)
            );

            // Absorbed without surfacing a failure or emitting any
            // effect (Req 2.7): the whole session — baseline,
            // startedAt, and crucially the deadline — is unchanged.
            expect(next).toEqual(session);
            expect(commands).toEqual([]);

            state = next;
          }

          // After the entire interleaving the deadline is exactly the
          // original anchor — no failed poll reset or extended it
          // (Req 2.5, 2.7).
          expect(state).toEqual(session);
          if (state.kind === 'polling') {
            expect(state.deadline).toBe(
              session.startedAt + POLL_TIMEOUT_MS
            );
          }
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'after any absorbed interleaving, the first event at or past the ' +
      'original deadline — POLL_FAILED or non-completing POLL_RESULT, ' +
      'including exactly at the deadline — times out and stops ' +
      'polling, regardless of how many polls failed before it',
    () => {
      const scenarioWithFinalTickArb = fc
        .tuple(baselineArb, nowArb)
        .chain(([baseline, startedAt]) =>
          fc
            .tuple(
              fc.array(absorptionStepArb(baseline, beforeDeadlineOffsetArb), {
                maxLength: 30,
              }),
              absorptionStepArb(baseline, atOrPastDeadlineOffsetArb)
            )
            .map(([steps, finalStep]) => ({
              session: makePollingSession(baseline, startedAt),
              steps,
              finalStep,
            }))
        );

      fc.assert(
        fc.property(
          scenarioWithFinalTickArb,
          ({ session, steps, finalStep }) => {
            // Drive the absorbed prefix — arbitrary counts and orders
            // of failed and non-completing polls (Req 2.7).
            let state: PublishSession = session;
            for (const step of steps) {
              state = publishReducer(
                state,
                absorptionEvent(step, session.startedAt + step.offset)
              ).state;
            }

            // The timeout point is determined solely by the original
            // anchored deadline: the first at-or-past-deadline tick
            // times out and stops polling (Req 2.5), independent of
            // the preceding failure count.
            const { state: finalState, commands } = publishReducer(
              state,
              absorptionEvent(
                finalStep,
                session.startedAt + finalStep.offset
              )
            );

            expect(finalState).toEqual({ kind: 'timed-out' });
            expect(commands).toEqual([{ type: 'STOP_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

// -------------------------------------------- generators (Property 10)

/** Sessions in the `failed` state carrying an arbitrary prior error —
 *  the exact states Property 10 quantifies over (Req 3.8). */
const failedSessionArb: fc.Arbitrary<
  Extract<PublishSession, { kind: 'failed' }>
> = sessionErrorArb.map((error) => ({ kind: 'failed' as const, error }));

/** True when a session value carries failure information from a prior
 *  attempt anywhere in its shape. */
function carriesError(state: PublishSession): boolean {
  return state.kind === 'failed' || 'error' in state;
}

/**
 * **Feature: vllm-package-publish-gui, Property 10: Retry activation clears prior failure state**
 *
 * For any session in a `failed` state and any record, dispatching
 * `ACTIVATE` or `ACTIVATE_PUBLISH_RETRY` SHALL produce a state carrying
 * no error, so no failure information from the previous attempt remains
 * displayed when new progress or result feedback begins.
 *
 * **Validates: Requirements 3.8**
 */

describe('Property 10: Retry activation clears prior failure state', () => {
  it(
    'for any failed session and any record, ACTIVATE produces a state ' +
      'carrying no error: requesting for unpublished records, confirming ' +
      'for published ones',
    () => {
      fc.assert(
        fc.property(
          failedSessionArb,
          vllmRecordArb,
          nowArb,
          (failedSession, record, now) => {
            const { state } = publishReducer(failedSession, {
              type: 'ACTIVATE',
              record,
              now,
            });

            // The prior failure is discarded: the next state is a fresh
            // activation state with no error anywhere in it (Req 3.8).
            expect(carriesError(state)).toBe(false);
            expect(
              record.published_component !== undefined
                ? state.kind === 'confirming'
                : state.kind === 'requesting'
            ).toBe(true);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'for any failed session and any record, ACTIVATE_PUBLISH_RETRY ' +
      'produces a requesting state carrying no error',
    () => {
      fc.assert(
        fc.property(
          failedSessionArb,
          vllmRecordArb,
          nowArb,
          (failedSession, record, now) => {
            const { state } = publishReducer(failedSession, {
              type: 'ACTIVATE_PUBLISH_RETRY',
              record,
              now,
            });

            // The publish-only retry equally discards the prior failure
            // before any new progress or result feedback (Req 3.8).
            expect(carriesError(state)).toBe(false);
            expect(state).toEqual({
              kind: 'requesting',
              action: 'publish-retry',
              baseline: record.published_component?.component_version ?? null,
            });
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'end to end: a failure from a real attempt is gone from every state ' +
      'reached after the retry activation, through confirmation and ' +
      'request success alike',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          sessionErrorArb,
          nowArb,
          nowArb,
          nowArb,
          fc.boolean(),
          (record, error, failNow, retryNow, successNow, viaRetry) => {
            // Drive a genuine failed attempt: activate an unpublished
            // record, then have the request fail (Req 3.1 path).
            const activated = publishReducer(
              { kind: 'idle' },
              { type: 'ACTIVATE', record: withoutPublished(record), now: failNow }
            );
            const failed = publishReducer(activated.state, {
              type: 'REQUEST_FAILED',
              error,
            });
            expect(failed.state).toEqual({ kind: 'failed', error });

            // Retry via either action (Req 3.8 names both).
            const retried = publishReducer(
              failed.state,
              viaRetry
                ? { type: 'ACTIVATE_PUBLISH_RETRY', record, now: retryNow }
                : { type: 'ACTIVATE', record, now: retryNow }
            );
            expect(carriesError(retried.state)).toBe(false);

            // The prior error never resurfaces as the new session
            // proceeds: confirming → requesting, and requesting →
            // polling on success (new progress feedback, Req 3.8).
            let session = retried.state;
            if (session.kind === 'confirming') {
              session = publishReducer(session, {
                type: 'CONFIRM',
                now: retryNow,
              }).state;
              expect(carriesError(session)).toBe(false);
            }
            if (session.kind === 'requesting') {
              session = publishReducer(session, {
                type: 'REQUEST_SUCCEEDED',
                now: successNow,
              }).state;
              expect(carriesError(session)).toBe(false);
              expect(session.kind).toBe('polling');
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

// -------------------------------------------- generators (Property 11)

/** Records eligible for the publish-only retry: at least one
 *  successfully packaged entry and no `published_component` (Req 3.3). */
const retryEligibleRecordArb: fc.Arbitrary<VllmPublishRecord> = fc
  .tuple(successfulEntryArb, fc.array(successfulEntryArb, { maxLength: 3 }))
  .map(([first, rest]) => ({
    model_type: 'vllm',
    packaged_components: [first, ...rest],
  }));

/** Non-in-flight sessions: idle plus every terminal state — the states
 *  from which a publish-only retry activation is accepted. */
const nonInFlightSessionArb: fc.Arbitrary<PublishSession> = fc.oneof(
  fc.constant({ kind: 'idle' } as PublishSession),
  failedSessionArb,
  fc.constant({ kind: 'timed-out' } as PublishSession),
  publishedComponentArb.map((component) => ({
    kind: 'published' as const,
    component,
  }))
);

/**
 * **Feature: vllm-package-publish-gui, Property 11: Publish-only retry resumes the standard publish session**
 *
 * For any record with a successfully packaged entry and no
 * `published_component`, `ACTIVATE_PUBLISH_RETRY` SHALL emit exactly one
 * `INVOKE_PUBLISH` command and enter `requesting('publish-retry')`, and
 * a subsequent `REQUEST_SUCCEEDED` SHALL enter `polling` with a fresh
 * `POLL_TIMEOUT_MS` deadline — identical in structure to the packaging
 * path's session.
 *
 * **Validates: Requirements 3.4**
 */

describe('Property 11: Publish-only retry resumes the standard publish session', () => {
  it(
    'from any non-in-flight session, ACTIVATE_PUBLISH_RETRY on a ' +
      'retry-eligible record emits exactly one INVOKE_PUBLISH — and no ' +
      'packaging invocation — entering requesting(publish-retry) with ' +
      'the absent baseline',
    () => {
      fc.assert(
        fc.property(
          nonInFlightSessionArb,
          retryEligibleRecordArb,
          nowArb,
          (session, record, now) => {
            const { state, commands } = publishReducer(session, {
              type: 'ACTIVATE_PUBLISH_RETRY',
              record,
              now,
            });

            // Exactly one INVOKE_PUBLISH, no INVOKE_PACKAGING: the retry
            // re-drives publish without re-running packaging (Req 3.4).
            expect(commands).toEqual([{ type: 'INVOKE_PUBLISH' }]);

            // The session enters the same requesting shape the packaging
            // path uses, tagged with the retry action; the baseline is
            // null because retry-eligible records carry no
            // published_component (Req 3.4).
            expect(state).toEqual({
              kind: 'requesting',
              action: 'publish-retry',
              baseline: null,
            });
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'a REQUEST_SUCCEEDED following the retry activation enters polling ' +
      'anchored at the success time with a fresh POLL_TIMEOUT_MS ' +
      'deadline, emitting START_POLLING',
    () => {
      fc.assert(
        fc.property(
          nonInFlightSessionArb,
          retryEligibleRecordArb,
          nowArb,
          nowArb,
          (session, record, activateNow, successNow) => {
            const activated = publishReducer(session, {
              type: 'ACTIVATE_PUBLISH_RETRY',
              record,
              now: activateNow,
            });

            const { state, commands } = publishReducer(activated.state, {
              type: 'REQUEST_SUCCEEDED',
              now: successNow,
            });

            // The retry resumes the standard publish progress behavior:
            // an anchored polling session with the 5-minute deadline
            // fixed at the success time (Req 3.4 → Req 2 behavior).
            expect(state).toEqual({
              kind: 'polling',
              baseline: null,
              startedAt: successNow,
              deadline: successNow + POLL_TIMEOUT_MS,
            });
            expect(commands).toEqual([{ type: 'START_POLLING' }]);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'the polling session the retry path reaches is structurally ' +
      'identical to the one the packaging path reaches for the same ' +
      'record and times',
    () => {
      fc.assert(
        fc.property(
          retryEligibleRecordArb,
          nowArb,
          nowArb,
          (record, activateNow, successNow) => {
            // Publish-only retry path.
            const retryRequesting = publishReducer(
              { kind: 'idle' },
              { type: 'ACTIVATE_PUBLISH_RETRY', record, now: activateNow }
            );
            const retryPolling = publishReducer(retryRequesting.state, {
              type: 'REQUEST_SUCCEEDED',
              now: successNow,
            });

            // Packaging path over the same unpublished record: direct
            // invoke, no confirmation gate.
            const packageRequesting = publishReducer(
              { kind: 'idle' },
              { type: 'ACTIVATE', record, now: activateNow }
            );
            const packagePolling = publishReducer(packageRequesting.state, {
              type: 'REQUEST_SUCCEEDED',
              now: successNow,
            });

            // Identical session structure: same baseline, same anchor,
            // same deadline, same START_POLLING effect (Req 3.4 resumes
            // the standard Requirement 2 session).
            expect(retryPolling.state).toEqual(packagePolling.state);
            expect(retryPolling.commands).toEqual(packagePolling.commands);
            expect(retryPolling.state).toEqual({
              kind: 'polling',
              baseline: null,
              startedAt: successNow,
              deadline: successNow + POLL_TIMEOUT_MS,
            });
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});
