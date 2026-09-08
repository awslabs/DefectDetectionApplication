/**
 * Bug condition exploration property test for the labeling-jobs 1970-date
 * defect (labeling-jobs-epoch-1970-dates task 1), jobs-table surface.
 *
 * **Feature: labeling-jobs-epoch-1970-dates, Property 1: Bug Condition —
 * epoch-seconds labeling job timestamps rendered as milliseconds
 * (jobsTableCreatedColumn site, Labeling.tsx jobs table Created column)**
 *
 * *For any* labeling job whose list-payload `created_at` is a realistic
 * epoch-SECONDS timestamp (2020-2040, i.e.
 * `fc.integer({ min: 1577836800, max: 2208988800 })`), the Data Labeling
 * page's jobs-table Created cell SHALL render
 * `new Date(created_at * 1000).toLocaleString()` — the job's actual
 * creation date — and never a year-1970 date.
 *
 * **Validates: Requirements 2.1** (bug condition evidenced: 1.1, 1.5)
 *
 * EXPECTED TO FAIL ON UNFIXED CODE: `Labeling.tsx` passes the
 * epoch-seconds `created_at` straight to `new Date(...)`, which reads it
 * as epoch milliseconds, so every realistic timestamp collapses to
 * January 1970 (the reported screenshot: every row `1/21/1970, ...`).
 * The failure is the exploration outcome that proves the bug exists; the
 * same assertions validate the fix once the seconds → milliseconds
 * conversion is applied.
 *
 * Mock scaffolding follows the page's suite conventions
 * (Labeling.delete.test.tsx / CreateLabelingJob.groundedsam.property.test.tsx):
 * an apiService Proxy over vi.hoisted mocks, a `useAuth` stub, a
 * `useNavigate` stub, and a render-per-run walk — every fast-check run
 * mounts `Labeling` fresh against a one-job list payload. The page
 * auto-selects the first use case from `listUseCases`, which triggers
 * `listLabelingJobs`. The Created cell is located positionally through
 * the Cloudscape table test-utils by its column header, so the assertion
 * reads exactly the defective cell (never another column's text).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import * as fc from 'fast-check';

import Labeling from './Labeling';

const { listUseCases, listLabelingJobs } = vi.hoisted(() => ({
  listUseCases: vi.fn(),
  listLabelingJobs: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { listUseCases, listLabelingJobs },
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
// Fixtures and helpers
// ---------------------------------------------------------------------------

/**
 * Realistic epoch-SECONDS creation timestamps, 2020-01-01T00:00:00Z
 * through 2040-01-01T00:00:00Z (the bugfix.md scoped-PBT domain).
 */
const epochSecondsArb = fc.integer({ min: 1577836800, max: 2208988800 });

const JOB_NAME = 'epoch-dates-property-job';

/** Prime the mocks around one list-payload job with the given created_at. */
function primeMocks(createdAtSeconds: number) {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC One' }],
    count: 1,
  });
  listLabelingJobs.mockResolvedValue({
    jobs: [
      {
        job_id: 'job-1',
        job_name: JOB_NAME,
        status: 'InProgress',
        task_type: 'Classification',
        image_count: 10,
        labeled_objects: 4,
        progress_percent: 40,
        created_at: createdAtSeconds,
        updated_at: createdAtSeconds,
        labeling_backend: 'DDA',
      },
    ],
    count: 1,
  });
}

beforeEach(() => {
  primeMocks(1789000000);
});

/**
 * Mount the page against a one-job payload and read the jobs-table
 * Created cell's rendered text. The Created column is found by its
 * header text: `findColumnHeaders()` and `findBodyCell()` index the same
 * cell list (selection column included), so the header position maps
 * 1:1 onto the body cell.
 */
async function renderedCreatedCell(createdAtSeconds: number): Promise<string> {
  primeMocks(createdAtSeconds);
  const { container } = render(<Labeling />);
  await screen.findByText(JOB_NAME);

  const table = createWrapper(container).findTable()!;
  const headerTexts = table
    .findColumnHeaders()
    .map((header) => header.getElement().textContent ?? '');
  const createdIndex = headerTexts.findIndex((text) =>
    text.includes('Created')
  );
  expect(createdIndex).toBeGreaterThanOrEqual(0);

  const cell = table.findBodyCell(1, createdIndex + 1);
  expect(cell).not.toBeNull();
  return cell!.getElement().textContent ?? '';
}

// ---------------------------------------------------------------------------
// Feature: labeling-jobs-epoch-1970-dates, Property 1: Bug Condition —
// jobs-table Created column (site jobsTableCreatedColumn)
// ---------------------------------------------------------------------------

describe('Feature: labeling-jobs-epoch-1970-dates, Property 1: Bug Condition — jobs table Created column', () => {
  it('renders any epoch-seconds created_at as new Date(seconds * 1000).toLocaleString(), never a 1970 date', async () => {
    await fc.assert(
      fc.asyncProperty(epochSecondsArb, async (createdAt) => {
        cleanup();
        const cellText = await renderedCreatedCell(createdAt);
        // Fix Checking oracle (Requirement 2.1): the Created cell treats
        // the stored value as epoch seconds.
        expect(cellText).toBe(new Date(createdAt * 1000).toLocaleString());
        // And for any realistic epoch-seconds value the rendered year is
        // never 1970.
        expect(cellText).not.toContain('1970');
      }),
      { numRuns: 25 }
    );
  }, 300_000);

  it('renders the reported counterexample created_at = 1789000000 (September 2026) as its actual date, not 1/21/1970', async () => {
    // The bugfix.md concrete counterexample: ~1.789e9 seconds read as
    // milliseconds is ~20.7 days after the epoch — the screenshot's
    // `1/21/1970, ...` rows.
    const cellText = await renderedCreatedCell(1789000000);
    expect(cellText).toBe(new Date(1789000000 * 1000).toLocaleString());
    expect(cellText).not.toContain('1970');
  });
});
