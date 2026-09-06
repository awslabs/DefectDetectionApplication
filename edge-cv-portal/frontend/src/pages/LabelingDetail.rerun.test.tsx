/**
 * Example tests for LabelingDetail's pre-label failure visibility and
 * re-run pre-labels dialog
 * (grounded-sam-prompt-guardrails-and-prelabel-retry task 4.8,
 * Requirements 3.5, 4.2, 4.3, 7.3, 7.4, 7.6, 7.7).
 *
 * Covers, by example:
 * - the "Pre-labeling failures" warning alert renders the failed count
 *   and the Failure_Reason_Summary entries with their counts when
 *   `prelabel_failed_count` >= 1, and neither the alert nor the re-run
 *   action renders for a zero-failed job (Req 4.2, 4.3);
 * - the grounded-sam re-run dialog pre-fills one entry per Label_Set
 *   label from the persisted `auto_label.prompt_overrides`, renders the
 *   shared Prompt_Guidance (constraint text plus info content), and
 *   blocks a period-bearing entry in-dialog without calling the API
 *   (Req 7.3, 3.5);
 * - an llm-family job's dialog shows the failed count with zero
 *   override entries (Req 7.4);
 * - a 202 closes the dialog and refetches the job detail (Req 7.6);
 * - a rejected request shows the response's error content (the message
 *   plus its `details.validation_errors` messages) inline, leaving the
 *   dialog open with the entered values retained (Req 7.7).
 *
 * Mock scaffolding follows LabelingDetail.test.tsx. The detail page does
 * not touch window.localStorage (verified: no reference in
 * LabelingDetail.tsx), so no storage reset is needed between tests.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';

import LabelingDetail from './LabelingDetail';
import { PROMPT_GUIDANCE_CONSTRAINT } from './promptOverrideGuardrails';

const { getLabelingJob, rerunPrelabels } = vi.hoisted(() => ({
  getLabelingJob: vi.fn(),
  rerunPrelabels: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getLabelingJob, rerunPrelabels },
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

/** The motivating incident's per-image `prelabel_error` string: the
 *  Grounded-SAM worker's caption-alignment guard tripping on an
 *  instruction-style prompt with inner periods. */
const CAPTION_ALIGNMENT_ERROR =
  'Grounded-SAM worker failed: {"errorMessage": "caption token spans (2) ' +
  'do not align with the 1 prompts; a prompt likely contains inner ' +
  'sentence punctuation", "errorType": "ValueError"}';

/** The incident's instruction-style Prompt_Override with an inner
 *  period — the persisted value the dialog must pre-fill verbatim. */
const BROKEN_PERIOD_PROMPT =
  'draw and fill in the gaps between the broken cookie pieces within ' +
  'the bounds of the image. If there is a large crack, fill it in';

const baseDdaJob = {
  job_id: 'job-1',
  usecase_id: 'usecase-1',
  job_name: 'rerun-detail-job',
  sagemaker_job_name: '',
  status: 'InProgress',
  task_type: 'Segmentation',
  dataset_prefix: 'datasets/d1',
  image_count: 72,
  label_categories: [],
  manifest_s3_uri: '',
  output_s3_uri: '',
  workforce_arn: '',
  created_at: 1700000000000,
  created_by: 'admin',
  updated_at: 1700000000000,
  labeling_backend: 'DDA' as const,
  label_set: ['cookie_gap'],
  submitted_count: 0,
};

/** The incident job's shape: a retry-eligible InProgress grounded-sam
 *  job whose every image failed on the alignment guard, the broken
 *  override persisted on the record. */
const groundedSamIncidentJob = {
  ...baseDdaJob,
  auto_label: {
    enabled: true,
    model: 'grounded-sam',
    prompt_overrides: { cookie_gap: BROKEN_PERIOD_PROMPT },
  },
  prelabel_available_count: 0,
  prelabel_failed_count: 72,
  prelabel_failure_reasons: [{ reason: CAPTION_ALIGNMENT_ERROR, count: 72 }],
};

/** A retry-eligible llm-family job with a few failed pre-labels. */
const llmFailedJob = {
  ...baseDdaJob,
  job_name: 'llm-detail-job',
  task_type: 'ObjectDetection',
  label_set: ['scratch', 'dent'],
  image_count: 10,
  auto_label: {
    enabled: true,
    model: 'llm:us.amazon.nova-pro-v1:0',
    detection_prompt: 'Find every visible scratch',
  },
  prelabel_available_count: 1,
  prelabel_failed_count: 3,
  prelabel_failure_reasons: [
    { reason: 'Model invocation timed out', count: 3 },
  ],
};

async function renderPage(job: Record<string, unknown>) {
  getLabelingJob.mockResolvedValue({ job });
  const rendered = render(<LabelingDetail />);
  // Let the loadJob effect resolve.
  await act(async () => {});
  return rendered;
}

const openRerunDialog = () => {
  fireEvent.click(screen.getByTestId('rerun-prelabels-button'));
  return screen.getByTestId('rerun-prelabels-modal');
};

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Failure alert (Req 4.2, 4.3)
// ---------------------------------------------------------------------------

describe('LabelingDetail pre-labeling failures alert (Req 4.2, 4.3)', () => {
  it('states the failed count and lists each reason with its count', async () => {
    await renderPage(groundedSamIncidentJob);

    const alert = screen.getByTestId('prelabel-failures-alert');
    expect(alert).toHaveTextContent('Pre-labeling failures');
    expect(alert).toHaveTextContent('72 pre-label tasks failed.');
    // The Failure_Reason_Summary entry: the incident's caption-alignment
    // reason with its occurrence count.
    expect(alert).toHaveTextContent(
      `${CAPTION_ALIGNMENT_ERROR} (72 images)`
    );
  });

  it('renders no alert and no re-run action for a zero-failed job', async () => {
    await renderPage({
      ...llmFailedJob,
      prelabel_available_count: 4,
      prelabel_failed_count: 0,
      prelabel_failure_reasons: undefined,
    });

    // The Auto-Labeling container is on screen (llm configuration), so
    // the absence assertions below cannot pass vacuously.
    expect(screen.getByText('Auto-Labeling')).toBeInTheDocument();
    expect(screen.queryByTestId('prelabel-failures-alert')).toBeNull();
    expect(screen.queryByTestId('rerun-prelabels-button')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Grounded-sam dialog: pre-fill, shared guidance, in-dialog guardrail
// (Req 7.3, 3.5)
// ---------------------------------------------------------------------------

describe('LabelingDetail grounded-sam re-run dialog (Req 7.3, 3.5)', () => {
  it('pre-fills the persisted override and renders the shared guidance', async () => {
    await renderPage(groundedSamIncidentJob);
    openRerunDialog();

    // One entry per Label_Set label, pre-filled verbatim from the job
    // record's persisted auto_label.prompt_overrides.
    expect(
      screen.getByLabelText('Text prompt for cookie_gap')
    ).toHaveValue(BROKEN_PERIOD_PROMPT);

    // The shared module's constraint text renders on the entry
    // (Req 3.5: the same Prompt_Guidance as the wizard).
    expect(screen.getByText(PROMPT_GUIDANCE_CONSTRAINT)).toBeInTheDocument();
    // The guidance content: noun-phrase teaching with both examples,
    // and the instructions-don't-work rule.
    expect(
      screen.getByText(/short noun phrase naming the visual thing/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/gap between broken cookie pieces/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/scratch\s+on metal surface/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/does not work: the model grounds noun\s+phrases/)
    ).toBeInTheDocument();
  });

  it('blocks a period-bearing entry in-dialog without calling the API', async () => {
    await renderPage(groundedSamIncidentJob);
    openRerunDialog();

    const TYPED_PERIOD_VALUE =
      'gap between cookie pieces. large crack in the surface';
    fireEvent.change(screen.getByLabelText('Text prompt for cookie_gap'), {
      target: { value: TYPED_PERIOD_VALUE },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('rerun-prelabels-submit'));
    });

    // The period error names the label; the submission never reached
    // the API.
    const errorAlert = await screen.findByTestId('rerun-prelabels-error');
    expect(errorAlert).toHaveTextContent(
      'The text prompt for label "cookie_gap" contains a period'
    );
    expect(rerunPrelabels).not.toHaveBeenCalled();
    // The dialog stays open with the entered value retained.
    expect(screen.getByTestId('rerun-prelabels-modal')).toBeInTheDocument();
    expect(
      screen.getByLabelText('Text prompt for cookie_gap')
    ).toHaveValue(TYPED_PERIOD_VALUE);
  });
});

// ---------------------------------------------------------------------------
// Other-family dialog (Req 7.4)
// ---------------------------------------------------------------------------

describe('LabelingDetail llm-family re-run dialog (Req 7.4)', () => {
  it('shows the failed count with zero override entries', async () => {
    await renderPage(llmFailedJob);
    const modal = openRerunDialog();

    expect(modal).toHaveTextContent('3 failed pre-label tasks');
    // No Prompt_Override entry renders for a non-grounded-sam family.
    expect(screen.queryAllByLabelText(/^Text prompt for /)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 202 path (Req 7.6)
// ---------------------------------------------------------------------------

describe('LabelingDetail re-run 202 path (Req 7.6)', () => {
  it('closes the dialog and refetches the job detail', async () => {
    rerunPrelabels.mockResolvedValue({
      job_id: 'job-1',
      retried_count: 3,
      message: 'Re-run started for 3 failed pre-label task(s)',
    });
    await renderPage(llmFailedJob);
    expect(getLabelingJob).toHaveBeenCalledTimes(1);
    openRerunDialog();

    await act(async () => {
      fireEvent.click(screen.getByTestId('rerun-prelabels-submit'));
    });

    // A non-grounded-sam job submits a pure retry: no body.
    expect(rerunPrelabels).toHaveBeenCalledTimes(1);
    expect(rerunPrelabels).toHaveBeenCalledWith('job-1', undefined);
    // The dialog closed and the detail was refetched.
    await waitFor(() =>
      expect(screen.queryByTestId('rerun-prelabels-modal')).toBeNull()
    );
    expect(getLabelingJob).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// Error path (Req 7.7)
// ---------------------------------------------------------------------------

describe('LabelingDetail re-run error path (Req 7.7)', () => {
  it('shows the response error inline and retains the entered values', async () => {
    // The backend's corrective guardrail rejection: a creation-shaped
    // 400 whose validation_errors ride the thrown error's `details`.
    const CORRECTIVE_MESSAGE =
      "The text prompt for label 'cookie_gap' contains a period; " +
      'periods separate labels in the detection caption';
    rerunPrelabels.mockRejectedValue(
      Object.assign(new Error('Validation failed'), {
        details: {
          validation_errors: [
            {
              parameter: 'auto_label',
              message: CORRECTIVE_MESSAGE,
              label: 'cookie_gap',
            },
          ],
        },
      })
    );
    await renderPage(groundedSamIncidentJob);
    openRerunDialog();

    const FIXED_VALUE = 'gap between broken cookie pieces';
    fireEvent.change(screen.getByLabelText('Text prompt for cookie_gap'), {
      target: { value: FIXED_VALUE },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('rerun-prelabels-submit'));
    });

    // The request carried the pruned override (differs from the
    // persisted map, so the body field is present).
    expect(rerunPrelabels).toHaveBeenCalledWith('job-1', {
      prompt_overrides: { cookie_gap: FIXED_VALUE },
    });

    // The rejection's error content renders inline: the message and the
    // validation_errors messages.
    const errorAlert = await screen.findByTestId('rerun-prelabels-error');
    expect(errorAlert).toHaveTextContent('Validation failed');
    expect(errorAlert).toHaveTextContent(CORRECTIVE_MESSAGE);
    // The dialog stays open with the entered value retained, and the
    // detail was not refetched.
    expect(screen.getByTestId('rerun-prelabels-modal')).toBeInTheDocument();
    expect(
      screen.getByLabelText('Text prompt for cookie_gap')
    ).toHaveValue(FIXED_VALUE);
    expect(getLabelingJob).toHaveBeenCalledTimes(1);
  });
});
