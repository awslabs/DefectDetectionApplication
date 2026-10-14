/**
 * Example tests for LabelingDetail's delete control and Winner Podium
 * (labeling-job-cleanup-work-stealing-and-podium task 2.11,
 * Requirements 4.1-4.6, 8.1, 8.5).
 *
 * Covers, by example:
 * - `canDeleteDdaJob` per status: true exactly for DDA jobs resting in
 *   Completed, Failed, Stopped, or DeleteFailed; false for InProgress,
 *   Deleting, and Ground Truth jobs (Req 4.1, 4.2);
 * - rendered gating: the delete control (testid `delete-job-button`)
 *   renders for a Completed DDA payload and not for InProgress or
 *   Deleting ones (Req 4.1, 4.2);
 * - the confirmation dialog names the job and states the removed set
 *   (task assignments, pre-label/annotation artifacts) versus the
 *   retained set (dataset images, generated training manifest)
 *   (Req 4.3);
 * - confirming issues the Deletion_Route request and the 202 refetch
 *   renders the job in the Deleting status (Req 4.4);
 * - a rejected request surfaces the error with the job rendered
 *   unchanged (Req 4.5);
 * - a DeleteFailed job's control reads "Retry Delete" and the
 *   Deleting / DeleteFailed statuses render distinctly (in-progress
 *   "Deleting" / error "Delete Failed") (Req 4.6);
 * - the Winner Podium container renders for a Completed team payload
 *   carrying podium entries and is absent when the payload carries
 *   none (Req 8.1, 8.5).
 *
 * Mock scaffolding follows LabelingDetail.test.tsx /
 * LabelingDetail.rerun.test.tsx (apiService Proxy, router mocks, DDA
 * job payload fixtures). The delete modal is mounted unconditionally
 * with a `visible` flag, so open/closed assertions go through the
 * Cloudscape test-utils ModalWrapper.isVisible().
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import LabelingDetail, { canDeleteDdaJob } from './LabelingDetail';

const { getLabelingJob, deleteLabelingJob } = vi.hoisted(() => ({
  getLabelingJob: vi.fn(),
  deleteLabelingJob: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getLabelingJob, deleteLabelingJob },
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
// exercise only the DDA detail view.
vi.mock('../components/ManifestTransformer', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** A resting (Completed) DDA job — the deletable baseline. */
const baseDdaJob = {
  job_id: 'job-1',
  usecase_id: 'usecase-1',
  job_name: 'deletepodium-job',
  sagemaker_job_name: '',
  status: 'Completed',
  task_type: 'Classification',
  dataset_prefix: 'datasets/d1',
  image_count: 8,
  label_categories: [],
  manifest_s3_uri: '',
  output_s3_uri: '',
  workforce_arn: '',
  created_at: 1700000000000,
  created_by: 'admin',
  updated_at: 1700000000000,
  labeling_backend: 'DDA' as const,
  label_set: ['ok', 'broken'],
  submitted_count: 8,
};

/** Podium_Ranking entries as the detail payload carries them for a
 *  Completed team job: emails for current members, the bare user id
 *  for a departed submitter. */
const PODIUM_ENTRIES = [
  {
    place: 1,
    user_id: 'user-alice',
    email: 'alice@example.com',
    submitted: 4,
    final_submitted_at: 1700000400,
  },
  {
    place: 2,
    user_id: 'user-bob',
    email: 'bob@example.com',
    submitted: 3,
    final_submitted_at: 1700000500,
  },
  {
    place: 3,
    user_id: 'user-carol',
    submitted: 1,
    final_submitted_at: 1700000600,
  },
];

async function renderPage(job: Record<string, unknown>) {
  getLabelingJob.mockResolvedValue({ job });
  const rendered = render(<LabelingDetail />);
  // Let the loadJob effect resolve.
  await act(async () => {});
  return rendered;
}

/** The delete confirmation modal: the one carrying the confirm button
 *  (the stop modal is mounted beside it on the same page). */
const findDeleteModal = () => {
  const modal = createWrapper()
    .findAllModals()
    .find((m) => m.find('[data-testid="delete-job-confirm"]') !== null);
  expect(modal).toBeDefined();
  return modal!;
};

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// canDeleteDdaJob (Req 4.1, 4.2)
// ---------------------------------------------------------------------------

describe('canDeleteDdaJob (Req 4.1, 4.2)', () => {
  it.each([
    ['InProgress', false],
    ['Completed', true],
    ['Failed', true],
    ['Stopped', true],
    ['Deleting', false],
    ['DeleteFailed', true],
  ])('a DDA job in %s status → %s', (status, expected) => {
    expect(canDeleteDdaJob({ labeling_backend: 'DDA', status })).toBe(
      expected
    );
  });

  it('disallows deleting Ground Truth jobs even in a resting status', () => {
    expect(
      canDeleteDdaJob({ labeling_backend: 'GroundTruth', status: 'Completed' })
    ).toBe(false);
    expect(canDeleteDdaJob({ status: 'Completed' })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Rendered gating (Req 4.1, 4.2)
// ---------------------------------------------------------------------------

describe('LabelingDetail delete control gating (Req 4.1, 4.2)', () => {
  it('offers the delete control for a Completed DDA job', async () => {
    await renderPage(baseDdaJob);
    const button = screen.getByTestId('delete-job-button');
    expect(button).toHaveTextContent('Delete Job');
  });

  it('offers no delete control for an InProgress DDA job', async () => {
    await renderPage({ ...baseDdaJob, status: 'InProgress' });
    // The DDA detail view rendered (absence below is not vacuous).
    expect(screen.getByText(baseDdaJob.job_name)).toBeInTheDocument();
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
  });

  it('offers no delete control for a Deleting DDA job', async () => {
    await renderPage({ ...baseDdaJob, status: 'Deleting' });
    expect(screen.getByText(baseDdaJob.job_name)).toBeInTheDocument();
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Confirmation dialog wording (Req 4.3)
// ---------------------------------------------------------------------------

describe('LabelingDetail delete confirmation dialog (Req 4.3)', () => {
  it('names the job and states the removed and retained sets', async () => {
    await renderPage(baseDdaJob);

    // Closed until the control is activated.
    expect(findDeleteModal().isVisible()).toBe(false);

    fireEvent.click(screen.getByTestId('delete-job-button'));

    const modal = findDeleteModal();
    expect(modal.isVisible()).toBe(true);
    expect(modal.findHeader()!.getElement()).toHaveTextContent(
      'Delete labeling job'
    );
    const content = modal.findContent()!.getElement().textContent ?? '';
    // Names the job.
    expect(content).toContain('delete "deletepodium-job"');
    // The removed set: task assignments and pre-label/annotation
    // artifacts.
    expect(content).toContain(
      'task assignments and pre-label/annotation artifacts are removed'
    );
    // The retained set: dataset images and any generated training
    // manifest.
    expect(content).toContain(
      'dataset images and any generated training manifest are retained'
    );
  });
});

// ---------------------------------------------------------------------------
// Confirm flow (Req 4.4)
// ---------------------------------------------------------------------------

describe('LabelingDetail delete confirm flow (Req 4.4)', () => {
  it('issues the deletion request and renders the refetched Deleting status', async () => {
    deleteLabelingJob.mockResolvedValue({
      job_id: 'job-1',
      status: 'Deleting',
    });
    // First load: the Completed job. Refetch after the 202: Deleting.
    getLabelingJob
      .mockResolvedValueOnce({ job: baseDdaJob })
      .mockResolvedValue({ job: { ...baseDdaJob, status: 'Deleting' } });
    render(<LabelingDetail />);
    await act(async () => {});

    fireEvent.click(screen.getByTestId('delete-job-button'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('delete-job-confirm'));
    });

    expect(deleteLabelingJob).toHaveBeenCalledTimes(1);
    expect(deleteLabelingJob).toHaveBeenCalledWith('job-1');
    // The 202 closed the dialog and refetched the detail, which now
    // renders the Deleting status indicator with no delete control.
    await waitFor(() =>
      expect(screen.getByText('Deleting')).toBeInTheDocument()
    );
    expect(getLabelingJob).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId('delete-job-button')).toBeNull();
    expect(findDeleteModal().isVisible()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Failure path (Req 4.5)
// ---------------------------------------------------------------------------

describe('LabelingDetail delete failure (Req 4.5)', () => {
  it('surfaces the error and leaves the job rendered unchanged', async () => {
    deleteLabelingJob.mockRejectedValue(new Error('access denied'));
    await renderPage(baseDdaJob);

    fireEvent.click(screen.getByTestId('delete-job-button'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('delete-job-confirm'));
    });

    // The error surfaces (page-level alert header plus the reason).
    expect(await screen.findByText('Delete failed')).toBeInTheDocument();
    expect(
      screen.getAllByText(/The job was not deleted: access denied/).length
    ).toBeGreaterThan(0);
    // The job was not refetched and still renders as the deletable
    // Completed job: the delete control is offered and the dialog stays
    // open with the error inline.
    expect(getLabelingJob).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('delete-job-button')).toBeInTheDocument();
    expect(findDeleteModal().isVisible()).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// DeleteFailed / Deleting rendering (Req 4.6)
// ---------------------------------------------------------------------------

describe('LabelingDetail deletion status rendering (Req 4.6)', () => {
  it('renders the DeleteFailed control as a retry with an error indicator', async () => {
    await renderPage({ ...baseDdaJob, status: 'DeleteFailed' });

    // The delete control reads as a retry.
    expect(screen.getByTestId('delete-job-button')).toHaveTextContent(
      'Retry Delete'
    );
    // The status renders distinctly as an error-type "Delete Failed".
    const label = screen.getByText('Delete Failed');
    expect(label.closest('[class*="status-error"]')).not.toBeNull();
  });

  it('renders Deleting as an in-progress indicator', async () => {
    await renderPage({ ...baseDdaJob, status: 'Deleting' });

    const label = screen.getByText('Deleting');
    expect(label.closest('[class*="status-in-progress"]')).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Winner Podium container (Req 8.1, 8.5)
// ---------------------------------------------------------------------------

describe('LabelingDetail Winner Podium container (Req 8.1, 8.5)', () => {
  it('renders the podium for a Completed team job payload with entries', async () => {
    await renderPage({
      ...baseDdaJob,
      team_id: 'team-1',
      member_progress: [],
      podium: PODIUM_ENTRIES,
    });

    // The "Winner Podium" container header and the shared component.
    expect(screen.getByText('Winner Podium')).toBeInTheDocument();
    const podium = screen.getByTestId('winner-podium');
    // Entries render with the display name (email when carried, user id
    // otherwise) and the submitted count.
    expect(within(podium).getByTestId('podium-place-1')).toHaveTextContent(
      'alice@example.com'
    );
    expect(within(podium).getByTestId('podium-place-1')).toHaveTextContent(
      '4 submitted'
    );
    expect(within(podium).getByTestId('podium-place-2')).toHaveTextContent(
      'bob@example.com'
    );
    expect(within(podium).getByTestId('podium-place-3')).toHaveTextContent(
      'user-carol'
    );
  });

  it('renders no podium for a Completed payload without podium entries', async () => {
    await renderPage({ ...baseDdaJob, team_id: 'team-1', member_progress: [] });

    // The detail view rendered (absence below is not vacuous).
    expect(screen.getByText(baseDdaJob.job_name)).toBeInTheDocument();
    expect(screen.queryByTestId('winner-podium')).toBeNull();
    expect(screen.queryByText('Winner Podium')).toBeNull();
  });
});
