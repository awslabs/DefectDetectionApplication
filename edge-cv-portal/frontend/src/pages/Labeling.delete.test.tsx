/**
 * Example tests for the Labeling list page's delete control
 * (labeling-job-cleanup-work-stealing-and-podium task 2.12,
 * Requirements 4.1, 4.2, 4.4, 4.6, 4.7 — plus 4.3's shared dialog
 * wording en route through the confirmation flow).
 *
 * Covers, by example:
 * - the delete control (testid `delete-job-button`) renders in the
 *   header actions exactly for a selected resting DDA row: present for
 *   a Completed DDA selection, absent for InProgress and Deleting DDA
 *   selections, absent for a Completed Ground Truth selection, and
 *   absent with no selection at all (Req 4.1, 4.2);
 * - a selected DeleteFailed DDA row's control reads "Retry Delete"
 *   (Req 4.1, 4.6);
 * - activating the control opens the confirmation modal carrying the
 *   shared wording — the named job, the removed set (task assignments,
 *   pre-label/annotation artifacts), the retained set (dataset images,
 *   generated training manifest) — and confirming issues
 *   `deleteLabelingJob(job_id)` then reloads the list, rendering the
 *   job in the Deleting status (Req 4.3, 4.4);
 * - list rows render Deleting as an in-progress "Deleting" indicator
 *   and DeleteFailed as an error "Delete Failed" indicator (Req 4.6);
 * - a post-delete reload payload omitting the job drops its row
 *   (Req 4.7).
 *
 * Mock scaffolding follows the page suites' conventions
 * (CreateLabelingJob.test.tsx / LabelingDetail.deletepodium.test.tsx):
 * an apiService Proxy over vi.hoisted mocks, a `useAuth` stub, and a
 * `useNavigate` stub. The page auto-selects the first use case from
 * `listUseCases`, which triggers `listLabelingJobs` — both mocked per
 * test. The delete confirmation modal is mounted unconditionally with
 * a `visible` flag (beside the always-mounted pre-labeled-dataset
 * modal), so open/closed assertions go through the Cloudscape
 * test-utils ModalWrapper found by its `delete-job-confirm` button.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import Labeling from './Labeling';

const { listUseCases, listLabelingJobs, deleteLabelingJob } = vi.hoisted(
  () => ({
    listUseCases: vi.fn(),
    listLabelingJobs: vi.fn(),
    deleteLabelingJob: vi.fn(),
  })
);

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { listUseCases, listLabelingJobs, deleteLabelingJob },
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
// keeps the header actions to exactly the controls under test.
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'user', role: 'DataScientist' },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const USE_CASE = { usecase_id: 'uc-1', name: 'UC One' };

/** One list-payload job row as `listLabelingJobs` carries it. */
function listJob(overrides: Record<string, unknown> = {}) {
  return {
    job_id: 'job-completed',
    job_name: 'completed-dda',
    status: 'Completed',
    task_type: 'Classification',
    image_count: 8,
    labeled_objects: 8,
    progress_percent: 100,
    created_at: 1700000000000,
    updated_at: 1700000000000,
    labeling_backend: 'DDA',
    ...overrides,
  };
}

/**
 * The gating payload, one row per scenario. `useTableSort` starts with
 * no sorting column, so table rows keep payload order: row 1 Completed
 * DDA, row 2 InProgress DDA, row 3 Deleting DDA, row 4 Completed
 * Ground Truth, row 5 DeleteFailed DDA.
 */
const GATING_JOBS = [
  listJob(),
  listJob({
    job_id: 'job-inprogress',
    job_name: 'inprogress-dda',
    status: 'InProgress',
    labeled_objects: 4,
    progress_percent: 50,
  }),
  listJob({
    job_id: 'job-deleting',
    job_name: 'deleting-dda',
    status: 'Deleting',
  }),
  listJob({
    job_id: 'job-gt',
    job_name: 'completed-gt',
    labeling_backend: 'GroundTruth',
  }),
  listJob({
    job_id: 'job-deletefailed',
    job_name: 'deletefailed-dda',
    status: 'DeleteFailed',
  }),
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Render the page and wait until the jobs table shows the first row. */
async function renderPage(firstRowName = 'completed-dda') {
  const rendered = render(<Labeling />);
  await screen.findByText(firstRowName);
  return rendered;
}

/** Select the n-th jobs-table row (1-based) and wait for the selection. */
async function selectRow(n: number) {
  await act(async () => {
    createWrapper().findTable()!.findRowSelectionArea(n)!.click();
  });
  await waitFor(() =>
    expect(createWrapper().findTable()!.findSelectedRows()).toHaveLength(1)
  );
}

/** The delete confirmation modal: the mounted modal carrying the
 *  confirm button (the pre-labeled-dataset modal sits beside it). */
const findDeleteModal = () => {
  const modal = createWrapper()
    .findAllModals()
    .find((m) => m.find('[data-testid="delete-job-confirm"]') !== null);
  expect(modal).toBeDefined();
  return modal!;
};

beforeEach(() => {
  listUseCases.mockResolvedValue({ usecases: [USE_CASE], count: 1 });
  listLabelingJobs.mockResolvedValue({
    jobs: GATING_JOBS,
    count: GATING_JOBS.length,
  });
  deleteLabelingJob.mockResolvedValue({
    job_id: 'job-completed',
    status: 'Deleting',
  });
});

afterEach(() => {
  vi.resetAllMocks();
});

// ---------------------------------------------------------------------------
// Delete control gating (Req 4.1, 4.2)
// ---------------------------------------------------------------------------

describe('Labeling list delete control gating (Req 4.1, 4.2)', () => {
  it('offers no delete control while nothing is selected', async () => {
    await renderPage();
    // The jobs table rendered (absence below is not vacuous).
    expect(screen.getByText('completed-dda')).toBeInTheDocument();
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
  });

  it('offers the delete control for a selected Completed DDA job', async () => {
    await renderPage();
    await selectRow(1);
    const button = await screen.findByTestId('delete-job-button');
    expect(button).toHaveTextContent('Delete Job');
  });

  it('offers no delete control for a selected InProgress DDA job', async () => {
    await renderPage();
    await selectRow(2);
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
  });

  it('offers no delete control for a selected Deleting DDA job', async () => {
    await renderPage();
    await selectRow(3);
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
  });

  it('offers no delete control for a selected Completed Ground Truth job', async () => {
    await renderPage();
    await selectRow(4);
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
  });

  it('reads "Retry Delete" for a selected DeleteFailed DDA job (Req 4.6)', async () => {
    await renderPage();
    await selectRow(5);
    const button = await screen.findByTestId('delete-job-button');
    expect(button).toHaveTextContent('Retry Delete');
  });
});

// ---------------------------------------------------------------------------
// Confirmation flow (Req 4.3, 4.4)
// ---------------------------------------------------------------------------

describe('Labeling list delete confirmation flow (Req 4.3, 4.4)', () => {
  it('confirms with the shared wording, issues the deletion, and reloads the list', async () => {
    // First load: the resting Completed job. Reload after the 202: the
    // same job in the Deleting status.
    listLabelingJobs
      .mockResolvedValueOnce({ jobs: [listJob()], count: 1 })
      .mockResolvedValue({
        jobs: [listJob({ status: 'Deleting' })],
        count: 1,
      });
    await renderPage();
    await selectRow(1);

    // Closed until the control is activated.
    expect(findDeleteModal().isVisible()).toBe(false);

    fireEvent.click(screen.getByTestId('delete-job-button'));

    // The confirmation dialog carries the shared wording (Req 4.3):
    // names the job, states the removed set and the retained set.
    const modal = findDeleteModal();
    expect(modal.isVisible()).toBe(true);
    expect(modal.findHeader()!.getElement()).toHaveTextContent(
      'Delete labeling job'
    );
    const content = modal.findContent()!.getElement().textContent ?? '';
    expect(content).toContain('delete "completed-dda"');
    expect(content).toContain(
      'task assignments and pre-label/annotation artifacts are removed'
    );
    expect(content).toContain(
      'dataset images and any generated training manifest are retained'
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId('delete-job-confirm'));
    });

    // The Deletion_Route request carried the job id, and the 202 answer
    // reloaded the list (Req 4.4).
    expect(deleteLabelingJob).toHaveBeenCalledTimes(1);
    expect(deleteLabelingJob).toHaveBeenCalledWith('job-completed');
    await waitFor(() => expect(listLabelingJobs).toHaveBeenCalledTimes(2));

    // The reloaded row renders in the Deleting status with the dialog
    // closed and the selection (and its control) cleared.
    const label = await screen.findByText('Deleting');
    expect(label.closest('[class*="status-in-progress"]')).not.toBeNull();
    expect(findDeleteModal().isVisible()).toBe(false);
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Status indicators (Req 4.6)
// ---------------------------------------------------------------------------

describe('Labeling list deletion status indicators (Req 4.6)', () => {
  it('renders Deleting as in-progress and DeleteFailed as error', async () => {
    // GATING_JOBS carries one Deleting row and one DeleteFailed row.
    await renderPage();

    const deleting = screen.getByText('Deleting');
    expect(deleting.closest('[class*="status-in-progress"]')).not.toBeNull();

    const deleteFailed = screen.getByText('Delete Failed');
    expect(deleteFailed.closest('[class*="status-error"]')).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Deleted job absent after reload (Req 4.7)
// ---------------------------------------------------------------------------

describe('Labeling list drops the deleted job (Req 4.7)', () => {
  it('no longer lists a job the post-delete reload payload omits', async () => {
    const survivor = listJob({
      job_id: 'job-survivor',
      job_name: 'survivor-dda',
      status: 'Stopped',
    });
    // First load: the doomed job plus a survivor. Reload after the
    // delete: the survivor alone (the deletion completed server-side).
    listLabelingJobs
      .mockResolvedValueOnce({ jobs: [listJob(), survivor], count: 2 })
      .mockResolvedValue({ jobs: [survivor], count: 1 });
    await renderPage();
    await selectRow(1);

    fireEvent.click(screen.getByTestId('delete-job-button'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('delete-job-confirm'));
    });

    await waitFor(() => expect(listLabelingJobs).toHaveBeenCalledTimes(2));
    // The deleted job's row is gone; the survivor still renders (the
    // absence is not an empty table).
    await waitFor(() =>
      expect(screen.queryByText('completed-dda')).toBeNull()
    );
    expect(screen.getByText('survivor-dda')).toBeInTheDocument();
  });
});
