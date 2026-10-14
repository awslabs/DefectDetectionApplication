/**
 * Property-based tests for the pure helpers of the labeling-job
 * Setup_Draft module (`labelingJobDraft.ts`) — the example-ref merge
 * (Property 3) and, in a separate describe, the preview-run
 * Resume_Window (Property 4). Pure: no rendering, no storage, no mocks.
 *
 * **Feature: labeling-setup-session-recovery, Property 3: Example-ref
 * merging is restored-first, complete, and count-additive**
 *
 * For any restored ref lists and any uploaded ref lists (per
 * designation, arbitrary lengths and contents),
 * `mergedExampleRefs(restored, uploaded)` returns, per designation,
 * exactly the restored refs in order followed by the uploaded refs in
 * order; the few-shot example set built from the merge by the unchanged
 * `fewShotExamplesFromRefs` (the helper the job submission consumes,
 * `CreateLabelingJob.tsx`) carries every good ref before every bad ref
 * with per-designation positions numbered 0..n−1 in merge order; and
 * the merged per-designation counts equal the sum of the restored and
 * uploaded counts.
 *
 * **Validates: Requirements 2.3, 4.2, 4.3**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';

import {
  canResumePreviewRun,
  mergedExampleRefs,
  previewRunResumeWindowMs,
  type PreviewRunReference,
} from './labelingJobDraft';
import { fewShotExamplesFromRefs } from './CreateLabelingJob';

// ---------------------------------------------------------------------------
// Generators: example-ref lists per designation. Real refs are
// `s3://bucket/key` URIs staged under `labeling-examples/`, but the merge
// is generic over strings — so realistic refs are mixed with fully
// arbitrary contents (empty, unicode, whitespace, slashes).
// ---------------------------------------------------------------------------

/** Realistic staged-example ref: `s3://<bucket>/labeling-examples/<name>`. */
const s3RefArb: fc.Arbitrary<string> = fc
  .record({
    bucket: fc.stringMatching(/^[a-z0-9][a-z0-9.-]{2,20}$/),
    name: fc.stringMatching(/^[A-Za-z0-9._ -]{1,24}$/),
    ext: fc.constantFrom('.jpg', '.jpeg', '.png', ''),
  })
  .map(({ bucket, name, ext }) => `s3://${bucket}/labeling-examples/${name}${ext}`);

/** One ref: realistic S3 URI or fully arbitrary string content. */
const refArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 2, arbitrary: s3RefArb },
  { weight: 1, arbitrary: fc.string() },
  { weight: 1, arbitrary: fc.string({ unit: 'grapheme' }) }
);

/** Per-designation ref lists, arbitrary lengths (0 included). */
const refListsArb: fc.Arbitrary<{ good: string[]; bad: string[] }> = fc.record({
  good: fc.array(refArb, { minLength: 0, maxLength: 12 }),
  bad: fc.array(refArb, { minLength: 0, maxLength: 12 }),
});

/** Deep copy used to pin the inputs before exercising the helpers. */
function copyRefLists(lists: { good: string[]; bad: string[] }): {
  good: string[];
  bad: string[];
} {
  return { good: [...lists.good], bad: [...lists.bad] };
}

// ---------------------------------------------------------------------------
// Property 3
// ---------------------------------------------------------------------------

describe('Feature: labeling-setup-session-recovery, Property 3: Example-ref merging is restored-first, complete, and count-additive', () => {
  it('merges restored-first per designation, feeds fewShotExamplesFromRefs good-before-bad with per-designation positions 0..n−1 in merge order, adds counts, and mutates nothing', () => {
    fc.assert(
      fc.property(refListsArb, refListsArb, (restored, uploaded) => {
        const restoredSnapshot = copyRefLists(restored);
        const uploadedSnapshot = copyRefLists(uploaded);

        const merged = mergedExampleRefs(restored, uploaded);

        // (a) Per designation: exactly the restored refs in order
        // followed by the uploaded refs in order (Req 2.3, 4.3).
        expect(merged.good).toEqual([...restoredSnapshot.good, ...uploadedSnapshot.good]);
        expect(merged.bad).toEqual([...restoredSnapshot.bad, ...uploadedSnapshot.bad]);

        // (c) Merged per-designation counts equal the sums (Req 4.2).
        expect(merged.good).toHaveLength(
          restoredSnapshot.good.length + uploadedSnapshot.good.length
        );
        expect(merged.bad).toHaveLength(restoredSnapshot.bad.length + uploadedSnapshot.bad.length);

        // (b) The few-shot example set built from the merge by the
        // unchanged fewShotExamplesFromRefs — the shape the preview run
        // and the job submission consume (Req 4.3).
        const examples = fewShotExamplesFromRefs(merged);

        // Complete: one example per merged ref, nothing else.
        expect(examples).toHaveLength(merged.good.length + merged.bad.length);

        // Every good ref before every bad ref.
        const designations = examples.map((example) => example.designation);
        const lastGoodIndex = designations.lastIndexOf('good');
        const firstBadIndex = designations.indexOf('bad');
        if (lastGoodIndex !== -1 && firstBadIndex !== -1) {
          expect(lastGoodIndex).toBeLessThan(firstBadIndex);
        }

        // Per designation: refs in merge order, positions 0..n−1.
        const goodExamples = examples.filter((example) => example.designation === 'good');
        const badExamples = examples.filter((example) => example.designation === 'bad');
        expect(goodExamples.map((example) => example.ref)).toEqual(merged.good);
        expect(badExamples.map((example) => example.ref)).toEqual(merged.bad);
        expect(goodExamples.map((example) => example.position)).toEqual(
          merged.good.map((_, index) => index)
        );
        expect(badExamples.map((example) => example.position)).toEqual(
          merged.bad.map((_, index) => index)
        );

        // Pure: neither input was mutated by the merge or the few-shot
        // construction.
        expect(restored).toEqual(restoredSnapshot);
        expect(uploaded).toEqual(uploadedSnapshot);
      }),
      { numRuns: 100 }
    );
  });
});

// ---------------------------------------------------------------------------
// Property 4 generators: preview-run references and a `now` constructed to
// straddle the Resume_Window boundary.
// ---------------------------------------------------------------------------

/** Run ids: uuid-shaped or fully arbitrary strings (empty included). */
const runIdArb: fc.Arbitrary<string> = fc.oneof(fc.uuid(), fc.string());

/**
 * Sample counts ≥ 1. The uncapped branch of `min(n×120+60, 900)` covers
 * n ≤ 6, the min switches branches exactly at n = 7 (7×120+60 = 900),
 * and large counts sit deep inside the 900 s cap — all three regions are
 * drawn, with the cap point pinned explicitly.
 */
const sampleCountArb: fc.Arbitrary<number> = fc.oneof(
  { weight: 2, arbitrary: fc.integer({ min: 1, max: 8 }) },
  { weight: 1, arbitrary: fc.constantFrom(7, 8) },
  { weight: 2, arbitrary: fc.integer({ min: 1, max: 10_000_000 }) }
);

/** Run start times: epoch milliseconds up to year 2100. */
const startedAtMsArb: fc.Arbitrary<number> = fc.integer({
  min: 0,
  max: 4_102_444_800_000,
});

/**
 * Offset of `now` from the window's end (`now = start + window +
 * offset`): ±1 and 0 pin the boundary (just below, exactly at, just
 * above), the mid range straddles it by up to the largest possible
 * window (4 500 000 ms), and the far range covers comfortably-inside
 * references — including a `now` before the start — and long-expired
 * ones. All sums stay within safe-integer precision.
 */
const offsetMsArb: fc.Arbitrary<number> = fc.oneof(
  { weight: 3, arbitrary: fc.constantFrom(-1, 0, 1) },
  { weight: 2, arbitrary: fc.integer({ min: -4_500_000, max: 4_500_000 }) },
  { weight: 1, arbitrary: fc.integer({ min: -4_102_444_800_000, max: 4_102_444_800_000 }) }
);

// ---------------------------------------------------------------------------
// Property 4
// ---------------------------------------------------------------------------

/**
 * **Feature: labeling-setup-session-recovery, Property 4: The resume
 * window equals the backend-derived readability bound**
 *
 * For any sample count n ≥ 1 and any start/now millisecond pair,
 * `canResumePreviewRun({runId, sampleCount: n, startedAtMs}, nowMs)`
 * SHALL hold exactly when
 * `nowMs − startedAtMs ≤ (min(n×120+60, 900) + 3600) × 1000` — the
 * backend's `expires_at` derivation plus TTL grace
 * (`dda_labeling.py`), restated here as the oracle rather than imported
 * — and SHALL be false for a null or undefined reference.
 *
 * **Validates: Requirements 5.1, 5.5**
 */
describe('Feature: labeling-setup-session-recovery, Property 4: The resume window equals the backend-derived readability bound', () => {
  it('resumes exactly when now − startedAt ≤ (min(n×120+60, 900) + 3600) × 1000 ms, and never for null/undefined references', () => {
    fc.assert(
      fc.property(
        runIdArb,
        sampleCountArb,
        startedAtMsArb,
        offsetMsArb,
        (runId, sampleCount, startedAtMs, offsetMs) => {
          // The oracle, restated from the design's backend derivation
          // (dda_labeling.py: expires_at = start + min(n×120+60, 900) s;
          // ttl = expires_at + 3600 s) — deliberately NOT computed via
          // previewRunResumeWindowMs.
          const restatedWindowMs = (Math.min(sampleCount * 120 + 60, 900) + 3600) * 1000;

          // `now` straddles the window's end by construction: below
          // (offset < 0), exactly at (0), or above (> 0).
          const nowMs = startedAtMs + restatedWindowMs + offsetMs;
          const ref: PreviewRunReference = { runId, sampleCount, startedAtMs };

          // ⇔ the backend-derived readability bound (Req 5.1, 5.5).
          const expectedResumable = nowMs - startedAtMs <= restatedWindowMs;
          expect(canResumePreviewRun(ref, nowMs)).toBe(expectedResumable);
          // By construction the bound trips exactly when offset > 0.
          expect(expectedResumable).toBe(offsetMs <= 0);

          // The exported window helper agrees with the restated formula.
          expect(previewRunResumeWindowMs(sampleCount)).toBe(restatedWindowMs);

          // Null and undefined references are never resumable (Req 5.5).
          expect(canResumePreviewRun(null, nowMs)).toBe(false);
          expect(canResumePreviewRun(undefined, nowMs)).toBe(false);
        }
      ),
      { numRuns: 100 }
    );
  });
});
