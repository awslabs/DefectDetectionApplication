/**
 * Bug condition exploration property tests for the labeling-jobs
 * 1970-date defect (labeling-jobs-epoch-1970-dates task 1), job-detail
 * surfaces.
 *
 * **Feature: labeling-jobs-epoch-1970-dates, Property 1: Bug Condition —
 * epoch-seconds labeling job timestamps rendered as milliseconds
 * (ddaDetailKeyValuePairs, groundTruthDetailKeyValuePairs, and
 * groundTruthDurationMath sites in LabelingDetail.tsx)**
 *
 * *For any* job-detail record whose `created_at` / `completed_at` /
 * `stopped_at` are realistic epoch-SECONDS timestamps (2020-2040, i.e.
 * `fc.integer({ min: 1577836800, max: 2208988800 })`):
 * - a DDA job's Details key-value pairs SHALL render each present
 *   timestamp as `new Date(seconds * 1000).toLocaleString()` — never a
 *   1970 date (Requirement 2.2);
 * - a Ground Truth job's Created / Completed values SHALL render the
 *   same `* 1000` oracle (Requirement 2.3);
 * - a Ground Truth job's Duration SHALL be computed from consistent
 *   units — completed: `Math.round((completed_at - created_at) / 3600)`
 *   hours (a 2-hour job reports 2 hours, not 0); ongoing:
 *   `Math.round((Date.now() / 1000 - created_at) / 3600)` hours (a
 *   realistic count, not ~496,000) (Requirement 2.4).
 *
 * **Validates: Requirements 2.2, 2.3, 2.4** (bug condition evidenced:
 * 1.2, 1.3, 1.4, 1.5)
 *
 * EXPECTED TO FAIL ON UNFIXED CODE: `LabelingDetail.tsx` passes the
 * epoch-seconds values straight to `new Date(...)` (1970 dates) and
 * divides second-differences by 3,600,000 (completed durations collapse
 * to 0 hours) while mixing `Date.now()` milliseconds with epoch-seconds
 * `created_at` (ongoing durations ~496,000 hours). The failures are the
 * exploration outcome that proves the bug exists; the same assertions
 * validate the fix once the seconds → milliseconds conversion is applied.
 *
 * Mock scaffolding follows LabelingDetail.rerun.property.test.tsx: an
 * apiService Proxy over a vi.hoisted `getLabelingJob`, router stubs, the
 * heavy ManifestTransformer stubbed out, and a render-per-run walk —
 * every fast-check run mounts `LabelingDetail` fresh against one mocked
 * `GET /labeling/{id}` record. Rendered values are read through the
 * Cloudscape KeyValuePairs test-utils by their exact labels.
 */
import { describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import * as fc from 'fast-check';

import LabelingDetail from './LabelingDetail';

const { getLabelingJob } = vi.hoisted(() => ({
  getLabelingJob: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getLabelingJob },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        // Any other API call the page happens to make resolves to an
        // empty object so effects settle without error.
        return (..._args: unknown[]) => Promise.resolve({});
      },
    }
  );
  return { apiService };
});

vi.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => vi.fn(),
}));

// The manifest transform modal pulls heavy dependencies; these tests
// exercise only the detail key-value pairs (the sibling suites' mock).
vi.mock('../components/ManifestTransformer', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------

/**
 * Realistic epoch-SECONDS timestamps, 2020-01-01T00:00:00Z through
 * 2040-01-01T00:00:00Z (the bugfix.md scoped-PBT domain).
 */
const epochSecondsArb = fc.integer({ min: 1577836800, max: 2208988800 });

/**
 * Job lifetimes of at least 2 hours (up to 30 days): long enough that
 * the correct hour count (>= 2) is distinguishable from a
 * milliseconds-misread rounding collapse, matching the bugfix.md
 * "a 2-hour job reports 2 hours, not 0" expectation.
 */
const durationSecondsArb = fc.integer({ min: 7200, max: 2592000 });

/** Fields shared by every mocked `GET /labeling/{id}` record. */
const BASE_JOB = {
  job_id: 'job-1',
  usecase_id: 'uc-1',
  job_name: 'epoch-dates-detail-job',
  sagemaker_job_name: 'sm-job-1',
  task_type: 'Classification',
  dataset_prefix: 'datasets/d1',
  image_count: 10,
  label_categories: [] as string[],
  labeled_objects: 10,
  progress_percent: 100,
  manifest_s3_uri: 's3://bucket/manifest.json',
  output_s3_uri: 's3://out-bucket/output/',
  workforce_arn: '',
  created_by: 'admin',
};

/** Mount the page against the record and let the loadJob effect resolve. */
async function renderDetail(record: Record<string, unknown>) {
  getLabelingJob.mockResolvedValue({ job: record });
  const view = render(<LabelingDetail />);
  await act(async () => {});
  expect(screen.queryByText('Loading labeling job details...')).toBeNull();
  return view;
}

/**
 * The rendered value of the key-value pair with the given exact label,
 * searched across every KeyValuePairs on the page.
 */
function kvValue(container: HTMLElement, label: string): string {
  for (const kvp of createWrapper(container).findAllKeyValuePairs()) {
    for (const item of kvp.findItems()) {
      if (item.findLabel()?.getElement().textContent === label) {
        return item.findValue()?.getElement().textContent ?? '';
      }
    }
  }
  throw new Error(`No key-value pair labeled ${JSON.stringify(label)}`);
}

const secondsOracle = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString();

// ---------------------------------------------------------------------------
// Site ddaDetailKeyValuePairs: DDA job Created / Completed / Stopped
// ---------------------------------------------------------------------------

/** A DDA job record carrying all three timestamps, epoch seconds. */
function ddaJob(createdAt: number, completedAt: number, stoppedAt: number) {
  return {
    ...BASE_JOB,
    status: 'Completed',
    labeling_backend: 'DDA' as const,
    label_set: ['scratch', 'dent'],
    submitted_count: 10,
    created_at: createdAt,
    updated_at: createdAt,
    completed_at: completedAt,
    stopped_at: stoppedAt,
  };
}

describe('Feature: labeling-jobs-epoch-1970-dates, Property 1: Bug Condition — DDA detail Created / Completed / Stopped (Validates: Requirements 2.2)', () => {
  it('renders each present epoch-seconds timestamp as new Date(seconds * 1000).toLocaleString(), never a 1970 date', async () => {
    await fc.assert(
      fc.asyncProperty(
        epochSecondsArb,
        durationSecondsArb,
        durationSecondsArb,
        async (createdAt, completedOffset, stoppedOffset) => {
          cleanup();
          const completedAt = createdAt + completedOffset;
          const stoppedAt = createdAt + stoppedOffset;
          const { container } = await renderDetail(
            ddaJob(createdAt, completedAt, stoppedAt)
          );

          // Fix Checking oracle (Requirement 2.2): each present value is
          // treated as epoch seconds.
          expect(kvValue(container, 'Created')).toBe(secondsOracle(createdAt));
          expect(kvValue(container, 'Completed')).toBe(
            secondsOracle(completedAt)
          );
          expect(kvValue(container, 'Stopped')).toBe(secondsOracle(stoppedAt));
          for (const label of ['Created', 'Completed', 'Stopped']) {
            expect(kvValue(container, label)).not.toContain('1970');
          }
        }
      ),
      { numRuns: 25 }
    );
  }, 300_000);

  it('renders the reported counterexample created_at = 1789000000 (September 2026) as its actual date, not 1/21/1970', async () => {
    const { container } = await renderDetail(
      ddaJob(1789000000, 1789007200, 1789007260)
    );
    expect(kvValue(container, 'Created')).toBe(secondsOracle(1789000000));
    expect(kvValue(container, 'Created')).not.toContain('1970');
  });
});

// ---------------------------------------------------------------------------
// Sites groundTruthDetailKeyValuePairs + groundTruthDurationMath:
// Ground Truth job Created / Completed / Duration
// ---------------------------------------------------------------------------

/** A completed Ground Truth job record, timestamps in epoch seconds. */
function groundTruthCompletedJob(createdAt: number, completedAt: number) {
  return {
    ...BASE_JOB,
    status: 'Completed',
    labeling_backend: 'GroundTruth' as const,
    human_labeled: 10,
    created_at: createdAt,
    updated_at: completedAt,
    completed_at: completedAt,
  };
}

/** An ongoing Ground Truth job record (no completed_at), epoch seconds. */
function groundTruthOngoingJob(createdAt: number) {
  return {
    ...BASE_JOB,
    status: 'InProgress',
    labeling_backend: 'GroundTruth' as const,
    human_labeled: 4,
    labeled_objects: 4,
    progress_percent: 40,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

describe('Feature: labeling-jobs-epoch-1970-dates, Property 1: Bug Condition — Ground Truth detail Created / Completed and Duration (Validates: Requirements 2.3, 2.4)', () => {
  it('renders completed jobs with * 1000 date oracles and a Duration of round((completed_at - created_at) / 3600) hours', async () => {
    await fc.assert(
      fc.asyncProperty(
        epochSecondsArb,
        durationSecondsArb,
        async (createdAt, durationSeconds) => {
          cleanup();
          const completedAt = createdAt + durationSeconds;
          const { container } = await renderDetail(
            groundTruthCompletedJob(createdAt, completedAt)
          );

          // Duration from consistent seconds units (Requirement 2.4): a
          // >= 2-hour job never collapses to the milliseconds-misread 0.
          // Asserted first so the duration collapse surfaces as its own
          // counterexample beside the sibling tests' 1970 dates.
          expect(kvValue(container, 'Duration')).toBe(
            `${Math.round(durationSeconds / 3600)} hours`
          );

          // Fix Checking oracles (Requirement 2.3): dates from seconds.
          expect(kvValue(container, 'Created')).toBe(secondsOracle(createdAt));
          expect(kvValue(container, 'Completed')).toBe(
            secondsOracle(completedAt)
          );
          expect(kvValue(container, 'Created')).not.toContain('1970');
          expect(kvValue(container, 'Completed')).not.toContain('1970');
        }
      ),
      { numRuns: 25 }
    );
  }, 300_000);

  it('renders the concrete 2-hour job (created 1789000000, completed 1789007200) as "2 hours", not "0 hours"', async () => {
    const { container } = await renderDetail(
      groundTruthCompletedJob(1789000000, 1789007200)
    );
    // Duration first so the milliseconds-misread "0 hours" collapse is
    // surfaced as its own counterexample, then the 1970-date rendering.
    expect(kvValue(container, 'Duration')).toBe('2 hours');
    expect(kvValue(container, 'Created')).toBe(secondsOracle(1789000000));
    expect(kvValue(container, 'Created')).not.toContain('1970');
  });

  it('renders an ongoing job Duration as a realistic hour count from (Date.now() / 1000 - created_at) / 3600, not ~496,000 hours', async () => {
    await fc.assert(
      fc.asyncProperty(
        // Jobs created 1 hour to ~4.5 years ago (still within the
        // 2020+ domain), with sub-hour jitter so the expected count is
        // not pinned to exact hour boundaries.
        fc.integer({ min: 1, max: 40000 }),
        fc.integer({ min: 0, max: 3599 }),
        async (hoursAgo, jitterSeconds) => {
          cleanup();
          const createdAt =
            Math.floor(Date.now() / 1000) - hoursAgo * 3600 - jitterSeconds;
          const { container } = await renderDetail(
            groundTruthOngoingJob(createdAt)
          );

          // Duration first so the mixed-units ~496,000-hour count is
          // surfaced as its own counterexample.
          const duration = kvValue(container, 'Duration');
          const match = /^(-?\d+) hours \(ongoing\)$/.exec(duration);
          expect(match).not.toBeNull();
          const renderedHours = Number(match![1]);

          // Fix Checking oracle (Requirement 2.4): both operands in
          // seconds. Recomputed at assertion time, so a +/- 1 hour
          // tolerance absorbs the render-to-assert clock drift across
          // rounding boundaries — while the milliseconds-misread
          // ~496,000-hour rendering stays hundreds of thousands away.
          const oracleHours = Math.round(
            (Date.now() / 1000 - createdAt) / 3600
          );
          expect(renderedHours).toBeGreaterThanOrEqual(oracleHours - 1);
          expect(renderedHours).toBeLessThanOrEqual(oracleHours + 1);

          // And the Created date itself is the actual calendar date.
          expect(kvValue(container, 'Created')).toBe(secondsOracle(createdAt));
        }
      ),
      { numRuns: 25 }
    );
  }, 300_000);
});
