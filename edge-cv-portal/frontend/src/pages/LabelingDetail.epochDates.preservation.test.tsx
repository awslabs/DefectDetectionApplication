/**
 * Preservation property tests for the labeling job detail page
 * (labeling-jobs-epoch-1970-dates task 2).
 *
 * **Feature: labeling-jobs-epoch-1970-dates, Property 2: Preservation**
 *
 * Written BEFORE the fix, against the UNFIXED code, following the
 * observation-first methodology: the behaviors asserted here were
 * observed on the unfixed page and MUST keep holding after the
 * seconds → milliseconds fix lands at the defective render sites.
 *
 * Observed on UNFIXED code:
 * - A DDA job payload with `completed_at` / `stopped_at` ABSENT renders
 *   the `'-'` placeholder for the Completed and Stopped rows of the
 *   Details key-value pairs (the existing falsy guards, Requirement
 *   3.4); a present `created_at` renders a value (not the placeholder).
 * - A Ground Truth job payload with `completed_at` ABSENT renders `'-'`
 *   for the Overview tab's Completed row (Requirement 3.4).
 * - Non-timestamp detail content renders straight from the payload
 *   (Requirement 3.5): job name header, status indicator, task type,
 *   label set (joined, or `'-'` when empty), created by, job id,
 *   output manifest, Ground Truth ARN, worker-portal / console links
 *   (or their pinned fallbacks), and the progress values.
 * - The Created values and the Ground Truth Duration are the DEFECTIVE
 *   sites and are deliberately NOT asserted here (task 1's
 *   bug-condition suite owns them).
 *
 * **Validates: Requirements 3.4, 3.5**
 *
 * Mock scaffolding follows `LabelingDetail.deletepodium.test.tsx`
 * (apiService Proxy, router mocks, ManifestTransformer stub) with the
 * render-per-run walk of
 * `CreateLabelingJob.groundedsam.property.test.tsx`.
 */

import { describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
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
// exercise only the detail rendering.
vi.mock('../components/ManifestTransformer', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Generators and helpers
// ---------------------------------------------------------------------------

/** Epoch SECONDS in the 2020–2040 range (the spec's realistic domain). */
const epochSecondsArb = fc.integer({ min: 1577836800, max: 2208988800 });

/** Lowercase alphanumeric tokens: unambiguous in text assertions. */
const alnum = fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789');
const token = (prefix: string) =>
  fc
    .string({ unit: alnum, minLength: 3, maxLength: 10 })
    .map((s) => `${prefix}-${s}`);

const taskTypeArb = fc.constantFrom(
  'Classification',
  'ObjectDetection',
  'Segmentation'
);

/** Render per run against the given raw job payload. */
async function renderDetail(job: Record<string, unknown>) {
  cleanup();
  vi.clearAllMocks();
  getLabelingJob.mockResolvedValue({ job });
  const view = render(<LabelingDetail />);
  await screen.findByText(job.job_name as string);
  return view;
}

/** The value text of the key-value-pairs row wearing the given label. */
function kvpValue(label: string): string {
  const kvp = createWrapper().findKeyValuePairs();
  expect(kvp).not.toBeNull();
  const item = kvp!
    .findItems()
    .find((i) => i.findLabel()?.getElement().textContent === label);
  expect(item, `key-value row labeled "${label}"`).toBeDefined();
  return item!.findValue()!.getElement().textContent ?? '';
}

/** Assert a status indicator carrying the label and indicator type. */
function expectStatusIndicator(label: string, type: string) {
  const el = screen
    .getAllByText(label)
    .find((candidate) => candidate.closest(`[class*="status-${type}"]`));
  expect(el, `status indicator "${label}" of type ${type}`).toBeDefined();
}

// ---------------------------------------------------------------------------
// DDA detail — '-' placeholders and non-timestamp content (Req 3.4, 3.5)
// ---------------------------------------------------------------------------

/** The DDA status indicator map restated for the generated statuses
 *  (statuses a job with absent completed_at/stopped_at can rest in). */
const DDA_STATUS_ORACLE: Record<string, { type: string; label: string }> = {
  InProgress: { type: 'in-progress', label: 'In Progress' },
  Failed: { type: 'error', label: 'Failed' },
  Deleting: { type: 'in-progress', label: 'Deleting' },
  DeleteFailed: { type: 'error', label: 'Delete Failed' },
};

const ddaScenarioArb = fc.record({
  jobId: token('jid'),
  jobName: token('ddajob'),
  status: fc.constantFrom(...Object.keys(DDA_STATUS_ORACLE)),
  taskType: taskTypeArb,
  createdAtSeconds: epochSecondsArb,
  createdBy: token('user'),
  labelSet: fc.uniqueArray(
    fc.string({ unit: alnum, minLength: 1, maxLength: 6 }),
    { maxLength: 4 }
  ),
  imageCount: fc.integer({ min: 1, max: 500 }),
  submitted: fc.integer({ min: 0, max: 500 }),
  percent: fc.integer({ min: 0, max: 100 }),
  outputManifest: fc.option(
    fc
      .string({ unit: alnum, minLength: 3, maxLength: 8 })
      .map((s) => `s3://bucket/${s}.manifest`),
    { nil: undefined }
  ),
});

describe('Feature: labeling-jobs-epoch-1970-dates, Property 2: Preservation — DDA detail (Req 3.4, 3.5)', () => {
  it("renders '-' for absent completed_at/stopped_at and the non-timestamp details unchanged (Created value deliberately unasserted)", async () => {
    await fc.assert(
      fc.asyncProperty(ddaScenarioArb, async (scenario) => {
        const submitted = Math.min(scenario.submitted, scenario.imageCount);
        // completed_at and stopped_at are ABSENT from the payload.
        await renderDetail({
          job_id: scenario.jobId,
          usecase_id: 'usecase-1',
          job_name: scenario.jobName,
          sagemaker_job_name: '',
          status: scenario.status,
          task_type: scenario.taskType,
          dataset_prefix: 'datasets/d1',
          image_count: scenario.imageCount,
          label_categories: [],
          manifest_s3_uri: '',
          output_s3_uri: '',
          workforce_arn: '',
          created_at: scenario.createdAtSeconds,
          created_by: scenario.createdBy,
          updated_at: scenario.createdAtSeconds,
          labeling_backend: 'DDA',
          label_set: scenario.labelSet,
          submitted_count: submitted,
          progress_percent: scenario.percent,
          ...(scenario.outputManifest !== undefined
            ? { output_manifest_s3_uri: scenario.outputManifest }
            : {}),
        });

        // Absent timestamps render the '-' placeholder rows (Req 3.4).
        expect(kvpValue('Completed')).toBe('-');
        expect(kvpValue('Stopped')).toBe('-');
        // The present created_at renders a value, not the placeholder
        // (its text is the defective site — unasserted here).
        expect(kvpValue('Created')).not.toBe('-');

        // Non-timestamp details render unchanged (Req 3.5).
        expect(kvpValue('Job ID')).toBe(scenario.jobId);
        expect(kvpValue('Label Set')).toBe(
          scenario.labelSet.length > 0 ? scenario.labelSet.join(', ') : '-'
        );
        expect(kvpValue('Output Manifest')).toBe(
          scenario.outputManifest ?? '-'
        );

        const oracle = DDA_STATUS_ORACLE[scenario.status];
        expectStatusIndicator(oracle.label, oracle.type);
        expect(screen.getByText('DDA (portal-native)')).toBeInTheDocument();
        expect(screen.getByText(scenario.taskType)).toBeInTheDocument();
        expect(screen.getByText(scenario.createdBy)).toBeInTheDocument();
        expect(
          screen.getByText(
            `${submitted} of ${scenario.imageCount} tasks submitted`
          )
        ).toBeInTheDocument();
        expect(screen.getByText(`${scenario.percent}% complete`))
          .toBeInTheDocument();
      }),
      { numRuns: 25 }
    );
  }, 300_000);
});

// ---------------------------------------------------------------------------
// Ground Truth detail — '-' placeholder and non-timestamp content
// (Req 3.4, 3.5)
// ---------------------------------------------------------------------------

/** The Ground Truth status rendering restated for the generated
 *  statuses (raw status → mapped indicator; Stopped maps to failed). */
const GT_STATUS_ORACLE: Record<string, { type: string; label: string }> = {
  InProgress: { type: 'in-progress', label: 'In Progress' },
  Failed: { type: 'error', label: 'Failed' },
  Stopped: { type: 'error', label: 'Failed' },
};

const gtScenarioArb = fc.record({
  jobId: token('gtjid'),
  jobName: token('gtjob'),
  status: fc.constantFrom(...Object.keys(GT_STATUS_ORACLE)),
  taskType: taskTypeArb,
  createdAtSeconds: epochSecondsArb,
  createdBy: token('user'),
  arn: token('sm-job'),
  counts: fc
    .tuple(fc.integer({ min: 0, max: 500 }), fc.integer({ min: 1, max: 500 }))
    .map(([a, b]) => ({
      labeled: Math.min(a, b),
      total: Math.max(a, b),
    })),
  percent: fc.integer({ min: 0, max: 100 }),
  workerPortalUrl: fc.option(
    fc
      .string({ unit: alnum, minLength: 3, maxLength: 8 })
      .map((s) => `https://worker.example.com/${s}`),
    { nil: undefined }
  ),
  consoleUrl: fc.option(
    fc
      .string({ unit: alnum, minLength: 3, maxLength: 8 })
      .map((s) => `https://console.example.com/${s}`),
    { nil: undefined }
  ),
});

describe('Feature: labeling-jobs-epoch-1970-dates, Property 2: Preservation — Ground Truth detail (Req 3.4, 3.5)', () => {
  it("renders '-' for an absent completed_at and the non-timestamp overview unchanged (Created and Duration deliberately unasserted)", async () => {
    await fc.assert(
      fc.asyncProperty(gtScenarioArb, async (scenario) => {
        // completed_at is ABSENT from the payload.
        await renderDetail({
          job_id: scenario.jobId,
          usecase_id: 'usecase-1',
          job_name: scenario.jobName,
          sagemaker_job_name: scenario.arn,
          status: scenario.status,
          task_type: scenario.taskType,
          dataset_prefix: 'datasets/d1',
          image_count: scenario.counts.total,
          human_labeled: scenario.counts.labeled,
          label_categories: [],
          manifest_s3_uri: 's3://bucket/manifest.json',
          output_s3_uri: 's3://bucket/output/',
          workforce_arn: '',
          created_at: scenario.createdAtSeconds,
          created_by: scenario.createdBy,
          updated_at: scenario.createdAtSeconds,
          progress_percent: scenario.percent,
          labeling_backend: 'GroundTruth',
          ...(scenario.workerPortalUrl !== undefined
            ? { worker_portal_url: scenario.workerPortalUrl }
            : {}),
          ...(scenario.consoleUrl !== undefined
            ? { console_url: scenario.consoleUrl }
            : {}),
        });

        // The absent completed_at renders the '-' placeholder (Req 3.4).
        expect(kvpValue('Completed')).toBe('-');

        // Non-timestamp overview content renders unchanged (Req 3.5).
        // Observed on unfixed code: rows rendering an external-icon
        // Link carry a trailing space in their text content (the icon
        // markup), hence the trim on the two link-capable rows.
        expect(kvpValue('Job ID')).toBe(scenario.jobId);
        expect(kvpValue('Ground Truth Job ARN')).toBe(scenario.arn);
        expect(kvpValue('Worker Portal').trim()).toBe(
          scenario.workerPortalUrl ??
            'Not available yet (private workforce sign-in URL)'
        );
        expect(kvpValue('AWS Console').trim()).toBe(
          scenario.consoleUrl !== undefined
            ? 'View labeling job in SageMaker Ground Truth'
            : '-'
        );

        const oracle = GT_STATUS_ORACLE[scenario.status];
        expectStatusIndicator(oracle.label, oracle.type);
        expect(screen.getByText(scenario.taskType)).toBeInTheDocument();
        expect(screen.getByText('private')).toBeInTheDocument();
        expect(screen.getByText(scenario.createdBy)).toBeInTheDocument();
        expect(
          screen.getByText(
            `${scenario.counts.labeled} of ${scenario.counts.total} images labeled`
          )
        ).toBeInTheDocument();
        expect(screen.getByText(`${scenario.percent}% complete`))
          .toBeInTheDocument();
      }),
      { numRuns: 25 }
    );
  }, 300_000);
});
