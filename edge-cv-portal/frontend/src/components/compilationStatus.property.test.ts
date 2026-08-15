/**
 * **Feature: onnx-compile-error-diagnostics, Property 6: Fix Checking - The UI Surfaces the Preserved Reason**
 *
 * _For any_ status the poller can emit, the surface's classification SHALL be
 * identical for the value, its uppercase form, and its lowercase form; and the
 * diagnostic predicate that drives the "Compilation Errors" panel SHALL be
 * true whenever any of `failure_reason` / `error` / `poll_error` is present,
 * and for every normalized `FAILED` / `STOPPED` / `ERROR` status.
 *
 * **Validates: Requirements 2.14, 2.16, 2.17**
 *
 * The helpers under test are the pure, UI-free extractions from
 * `CompilationTab.tsx` (`normalizeCompilationStatus`,
 * `isDiagnosticCompilationJob` in `./compilationStatus`), following the
 * pattern of `vllm-publish/publishState.gating.property.test.ts`.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  normalizeCompilationStatus,
  isDiagnosticCompilationJob,
  type CompilationJobDiagnostics,
} from './compilationStatus';

// ------------------------------------------------------------- generators

/**
 * Every per-job status value the backend can write: the SageMaker describe
 * responses stored verbatim in uppercase, the portal-synthesized mixed-case
 * values, and the transient poll-fault marker.
 */
const EMITTABLE_STATUSES = [
  'STARTING',
  'INPROGRESS',
  'IN_PROGRESS',
  'COMPLETED',
  'FAILED',
  'STOPPING',
  'STOPPED',
  'ERROR',
  'InProgress',
  'Completed',
  'Failed',
] as const;

/** Re-case a string per character according to a boolean mask. */
const mixCase = (value: string, mask: boolean[]): string =>
  value
    .split('')
    .map((ch, i) => (mask[i % Math.max(mask.length, 1)] ? ch.toUpperCase() : ch.toLowerCase()))
    .join('');

/** An emittable status in an arbitrary per-character casing. */
const mixedCaseStatusArb: fc.Arbitrary<string> = fc
  .tuple(
    fc.constantFrom(...EMITTABLE_STATUSES),
    fc.array(fc.boolean(), { minLength: 1, maxLength: 12 })
  )
  .map(([status, mask]) => mixCase(status, mask));

/** Emittable statuses plus adversarial unknown values and the absent case. */
const statusArb: fc.Arbitrary<string | undefined> = fc.oneof(
  mixedCaseStatusArb,
  fc.constantFrom(...EMITTABLE_STATUSES),
  fc.string({ maxLength: 20 }),
  fc.constant(undefined)
);

/** A non-empty recorded reason / poll-fault string. */
const reasonArb: fc.Arbitrary<string> = fc.string({
  minLength: 1,
  maxLength: 60,
});

/** Each reason field independently present or absent. */
const optionalReasonArb: fc.Arbitrary<string | undefined> = fc.option(
  reasonArb,
  { nil: undefined }
);

/** A job over any status with any combination of reason fields. */
const jobArb: fc.Arbitrary<CompilationJobDiagnostics> = fc
  .record({
    status: statusArb,
    failure_reason: optionalReasonArb,
    error: optionalReasonArb,
    poll_error: optionalReasonArb,
  })
  .map(
    (job) =>
      Object.fromEntries(
        Object.entries(job).filter(([, value]) => value !== undefined)
      ) as CompilationJobDiagnostics
  );

// ------------------------------------------------------------------ tests

describe('Property 6: the UI surfaces the preserved reason', () => {
  it(
    'classification is identical for a status value, its uppercase form, ' +
      'and its lowercase form',
    () => {
      fc.assert(
        fc.property(jobArb, mixedCaseStatusArb, (job, status) => {
          // The normalizer maps every casing of a value to one class
          // (Req 2.16).
          expect(normalizeCompilationStatus(status)).toBe(
            normalizeCompilationStatus(status.toUpperCase())
          );
          expect(normalizeCompilationStatus(status)).toBe(
            normalizeCompilationStatus(status.toLowerCase())
          );

          // The diagnostic predicate classifies every casing identically,
          // with the reason fields held fixed (Req 2.16, 2.17).
          const asIs = isDiagnosticCompilationJob({ ...job, status });
          expect(
            isDiagnosticCompilationJob({ ...job, status: status.toUpperCase() })
          ).toBe(asIs);
          expect(
            isDiagnosticCompilationJob({ ...job, status: status.toLowerCase() })
          ).toBe(asIs);
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'the diagnostic predicate is true whenever any of failure_reason / ' +
      'error / poll_error is present',
    () => {
      // Force at least one reason field to be present; the others and the
      // status stay arbitrary (Req 2.14: a carried reason is always
      // surfaced, never a bare status token alone).
      const jobWithReasonArb: fc.Arbitrary<CompilationJobDiagnostics> = fc
        .tuple(
          jobArb,
          fc.constantFrom<'failure_reason' | 'error' | 'poll_error'>(
            'failure_reason',
            'error',
            'poll_error'
          ),
          reasonArb
        )
        .map(([job, field, reason]) => ({ ...job, [field]: reason }));

      fc.assert(
        fc.property(jobWithReasonArb, (job) => {
          expect(isDiagnosticCompilationJob(job)).toBe(true);
        }),
        { numRuns: 100 }
      );
    }
  );

  it(
    'the diagnostic predicate is true for every normalized FAILED / ' +
      'STOPPED / ERROR status, in any casing and with or without a reason',
    () => {
      // FAILED and STOPPED are genuine terminal failures; ERROR is the
      // transient poll-fault marker — all three must reach the errors
      // panel even when no reason field is present (Req 2.16, 2.17).
      const diagnosticStatusArb: fc.Arbitrary<string> = fc
        .tuple(
          fc.constantFrom('FAILED', 'STOPPED', 'ERROR'),
          fc.array(fc.boolean(), { minLength: 1, maxLength: 12 })
        )
        .map(([status, mask]) => mixCase(status, mask));

      fc.assert(
        fc.property(jobArb, diagnosticStatusArb, (job, status) => {
          expect(isDiagnosticCompilationJob({ ...job, status })).toBe(true);
        }),
        { numRuns: 100 }
      );
    }
  );
});
