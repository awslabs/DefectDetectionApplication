/**
 * Preservation property tests for the Data Labeling page
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
 * - The pre-labeled DATASETS table on this same page (`Labeling.tsx`
 *   datasets Created column) already treats `created_at` as epoch
 *   SECONDS: it renders `new Date(created_at * 1000).toLocaleDateString()`
 *   — correct current-era dates (e.g. `created_at = 1789000000` renders
 *   the September 2026 date, never 1970). This already-correct call
 *   site must not be touched by the fix (Requirement 3.1).
 * - The JOBS table renders its non-timestamp content straight from the
 *   list payload: job name link, task type, progress bar
 *   (`{labeled} / {total} images`, `{percent}% complete`), and the
 *   status indicator label per the page's status map (Requirement 3.5).
 *   The jobs-table Created cell is the DEFECTIVE site and is
 *   deliberately NOT asserted here (task 1's bug-condition suite owns
 *   it).
 *
 * **Validates: Requirements 3.1, 3.5**
 *
 * Mock scaffolding follows `Labeling.delete.test.tsx` (apiService
 * Proxy over hoisted mocks, `useAuth` stub, `useNavigate` stub) with
 * the render-per-run walk of
 * `CreateLabelingJob.groundedsam.property.test.tsx`.
 */

import { describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import * as fc from 'fast-check';

import Labeling from './Labeling';

const { listUseCases, listLabelingJobs, listPreLabeledDatasets } = vi.hoisted(
  () => ({
    listUseCases: vi.fn(),
    listLabelingJobs: vi.fn(),
    listPreLabeledDatasets: vi.fn(),
  })
);

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { listUseCases, listLabelingJobs, listPreLabeledDatasets },
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

// The page reads only `user?.role` (the Manage Teams gate); a non-admin
// keeps the header actions minimal.
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'user', role: 'DataScientist' },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

// ---------------------------------------------------------------------------
// Fixtures and generators
// ---------------------------------------------------------------------------

const USE_CASE = { usecase_id: 'uc-1', name: 'UC One' };

/** Epoch SECONDS in the 2020–2040 range (the spec's realistic domain). */
const epochSecondsArb = fc.integer({ min: 1577836800, max: 2208988800 });

/** Lowercase alphanumeric tokens: unambiguous in text assertions. */
const alnum = fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789');
const token = (prefix: string) =>
  fc
    .string({ unit: alnum, minLength: 3, maxLength: 10 })
    .map((s) => `${prefix}-${s}`);

/** Reset every mock to the run's payloads. */
function primeMocks({
  jobs = [] as Record<string, unknown>[],
  datasets = [] as Record<string, unknown>[],
} = {}) {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({ usecases: [USE_CASE], count: 1 });
  listLabelingJobs.mockResolvedValue({ jobs, count: jobs.length });
  listPreLabeledDatasets.mockResolvedValue({
    datasets,
    count: datasets.length,
  });
}

// ---------------------------------------------------------------------------
// Requirement 3.1 — the datasets table's already-correct Created cell
// ---------------------------------------------------------------------------

describe('Feature: labeling-jobs-epoch-1970-dates, Property 2: Preservation — datasets table Created cell (Req 3.1)', () => {
  it('renders the pre-labeled datasets Created cell as new Date(created_at * 1000).toLocaleDateString() — a current-era date, never 1970', async () => {
    await fc.assert(
      fc.asyncProperty(
        epochSecondsArb,
        token('ds'),
        async (createdAtSeconds, datasetName) => {
          cleanup();
          primeMocks({
            datasets: [
              {
                dataset_id: 'ds-1',
                usecase_id: USE_CASE.usecase_id,
                name: datasetName,
                manifest_s3_uri: 's3://bucket/m.manifest',
                image_count: 12,
                label_attribute: 'label',
                label_stats: { good: 9, bad: 3 },
                task_type: 'classification',
                created_at: createdAtSeconds,
                created_by: 'u-1',
                updated_at: createdAtSeconds,
              },
            ],
          });

          render(<Labeling />);

          // Switch the page to the pre-labeled datasets source.
          fireEvent.click(
            createWrapper()
              .findRadioGroup()!
              .findInputByValue('pre-labeled')!
              .getElement()
          );
          await screen.findByText(datasetName);

          // Self-validating column position: the datasets table has no
          // selection column, so Created is the 5th column.
          const table = createWrapper().findTable()!;
          const headerTexts = table
            .findColumnHeaders()
            .map((h) => h.getElement().textContent ?? '');
          expect(headerTexts[4]).toContain('Created');

          // The already-correct oracle observed on unfixed code: the
          // stored epoch seconds are scaled to milliseconds before the
          // Date is constructed (Requirement 3.1).
          const expected = new Date(
            createdAtSeconds * 1000
          ).toLocaleDateString();
          const cellText =
            table.findBodyCell(1, 5)!.getElement().textContent ?? '';
          expect(cellText).toBe(expected);
          expect(cellText).not.toContain('1970');
        }
      ),
      // The bugfix.md counterexample seed (September 2026): the datasets
      // table renders it correctly even on unfixed code.
      { numRuns: 25, examples: [[1789000000, 'ds-counterexample']] }
    );
  }, 300_000);
});

// ---------------------------------------------------------------------------
// Requirement 3.5 — jobs-table non-timestamp content
// ---------------------------------------------------------------------------

/** The page's status map restated (raw backend status → indicator). */
const JOBS_STATUS_ORACLE: Record<string, { type: string; label: string }> = {
  InProgress: { type: 'in-progress', label: 'In Progress' },
  Completed: { type: 'success', label: 'Completed' },
  Failed: { type: 'error', label: 'Failed' },
  Stopped: { type: 'info', label: 'Stopped' },
  Deleting: { type: 'in-progress', label: 'Deleting' },
  DeleteFailed: { type: 'error', label: 'Delete Failed' },
};

/** One jobs-table scenario straight off the list payload. */
const jobScenarioArb = fc.record({
  name: token('job'),
  taskType: fc.constantFrom(
    'Classification',
    'ObjectDetection',
    'Segmentation'
  ),
  status: fc.constantFrom(...Object.keys(JOBS_STATUS_ORACLE)),
  imageCount: fc.integer({ min: 1, max: 5000 }),
  labeled: fc.integer({ min: 0, max: 5000 }),
  percent: fc.integer({ min: 0, max: 100 }),
  createdAtSeconds: epochSecondsArb,
  backend: fc.constantFrom('DDA', 'GroundTruth'),
});

describe('Feature: labeling-jobs-epoch-1970-dates, Property 2: Preservation — jobs table non-timestamp content (Req 3.5)', () => {
  it('renders job name, task type, progress values, and status indicator unchanged from the payload (Created cell deliberately unasserted)', async () => {
    await fc.assert(
      fc.asyncProperty(jobScenarioArb, async (scenario) => {
        cleanup();
        const labeled = Math.min(scenario.labeled, scenario.imageCount);
        primeMocks({
          jobs: [
            {
              job_id: 'job-1',
              job_name: scenario.name,
              status: scenario.status,
              task_type: scenario.taskType,
              image_count: scenario.imageCount,
              labeled_objects: labeled,
              progress_percent: scenario.percent,
              created_at: scenario.createdAtSeconds,
              updated_at: scenario.createdAtSeconds,
              labeling_backend: scenario.backend,
            },
          ],
        });

        render(<Labeling />);
        await screen.findByText(scenario.name);

        const table = createWrapper().findTable()!;
        // The Created column still exists (its cell value is the
        // defective site owned by the task-1 suite — unasserted here).
        expect(
          table
            .findColumnHeaders()
            .some((h) => (h.getElement().textContent ?? '').includes('Created'))
        ).toBe(true);

        const rowEl = table.findRows()[0].getElement();
        const rowText = rowEl.textContent ?? '';

        // Non-timestamp cells render straight from the payload
        // (Requirement 3.5): name link, task type, progress bar label
        // and percentage, status indicator.
        expect(rowText).toContain(scenario.name);
        expect(rowText).toContain(scenario.taskType);
        expect(rowText).toContain(`${labeled} / ${scenario.imageCount} images`);
        expect(rowText).toContain(`${scenario.percent}% complete`);

        const oracle = JOBS_STATUS_ORACLE[scenario.status];
        const statusEl = within(rowEl).getByText(oracle.label);
        expect(
          statusEl.closest(`[class*="status-${oracle.type}"]`)
        ).not.toBeNull();
      }),
      { numRuns: 25 }
    );
  }, 300_000);
});
