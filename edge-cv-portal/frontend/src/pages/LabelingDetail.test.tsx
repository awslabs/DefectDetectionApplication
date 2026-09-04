/**
 * Unit tests for the DDA-job display helpers of LabelingDetail
 * (dda-data-labeling task 16.3): progress description including the
 * skip-verification substitution note (Requirements 11.1, 11.10) and
 * Stop-button visibility (Requirements 11.4, 11.9).
 *
 * Rendering tests for the Auto-Labeling container (llm-auto-labeling
 * task 15.2): the model identifier and the full untruncated
 * Detection_Prompt render for `llm:` jobs (Requirement 10.1); the
 * Pre-Labels Available / Failed counts render once at least one task has
 * resolved and are omitted entirely before that (Requirement 10.3); a
 * non-LLM job renders neither the model nor the prompt block.
 */

import { afterEach, describe, it, expect, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';

import LabelingDetail, {
  getDdaProgress,
  canStopDdaJob,
} from './LabelingDetail';

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
// exercise only the DDA detail view.
vi.mock('../components/ManifestTransformer', () => ({ default: () => null }));

describe('getDdaProgress', () => {
  it('reports submitted/total and the backend percentage for team jobs', () => {
    const progress = getDdaProgress({
      submitted_count: 3,
      image_count: 10,
      progress_percent: 30,
    });
    expect(progress.percent).toBe(30);
    expect(progress.description).toBe('3 of 10 tasks submitted');
    expect(progress.note).toBeUndefined();
  });

  it('computes the percentage when the backend omits it', () => {
    const progress = getDdaProgress({ submitted_count: 1, image_count: 3 });
    expect(progress.percent).toBe(33);
  });

  it('handles a zero image count without dividing by zero', () => {
    const progress = getDdaProgress({ submitted_count: 0, image_count: 0 });
    expect(progress.percent).toBe(0);
    expect(progress.description).toBe('0 of 0 tasks submitted');
  });

  it('substitutes auto-label completion wording for skip-verification jobs (Req 11.10)', () => {
    const progress = getDdaProgress({
      submitted_count: 4,
      image_count: 8,
      progress_percent: 50,
      skip_verification: true,
    });
    expect(progress.description).toBe(
      '4 of 8 auto-label attempts completed'
    );
    expect(progress.note).toContain('auto-label completion');
  });
});

describe('canStopDdaJob', () => {
  it('allows stopping only InProgress DDA jobs', () => {
    expect(
      canStopDdaJob({ labeling_backend: 'DDA', status: 'InProgress' })
    ).toBe(true);
  });

  it.each(['Completed', 'Failed', 'Stopped'])(
    'disallows stopping a DDA job in %s status',
    (status) => {
      expect(canStopDdaJob({ labeling_backend: 'DDA', status })).toBe(false);
    }
  );

  it('disallows stopping Ground Truth jobs', () => {
    expect(
      canStopDdaJob({ labeling_backend: 'GroundTruth', status: 'InProgress' })
    ).toBe(false);
    expect(canStopDdaJob({ status: 'InProgress' })).toBe(false);
  });
});

// --- Auto-Labeling container rendering (llm-auto-labeling task 15.2) ---

/** Multi-line prompt with leading indentation and blank lines that must
 *  survive rendering byte-for-byte (Requirement 10.1). */
const MULTILINE_PROMPT =
  'Find every visible scratch on the metal surface.\n' +
  '\n' +
  'Rules:\n' +
  '  - draw one tight box per defect\n' +
  '  - ignore reflections and dust';

const baseDdaJob = {
  job_id: 'job-1',
  usecase_id: 'usecase-1',
  job_name: 'llm-detail-job',
  sagemaker_job_name: '',
  status: 'InProgress',
  task_type: 'ObjectDetection',
  dataset_prefix: 'datasets/d1',
  image_count: 10,
  label_categories: [],
  manifest_s3_uri: '',
  output_s3_uri: '',
  workforce_arn: '',
  created_at: 1700000000000,
  created_by: 'admin',
  updated_at: 1700000000000,
  labeling_backend: 'DDA' as const,
  label_set: ['scratch', 'dent'],
  submitted_count: 2,
};

async function renderPage(job: Record<string, unknown>) {
  getLabelingJob.mockResolvedValue({ job });
  const rendered = render(<LabelingDetail />);
  // Let the loadJob effect resolve.
  await act(async () => {});
  return rendered;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('LabelingDetail Auto-Labeling container (Req 10.1, 10.3)', () => {
  it('renders the model identifier and the full multi-line prompt for an llm job', async () => {
    await renderPage({
      ...baseDdaJob,
      auto_label: {
        enabled: true,
        model: 'llm:us.amazon.nova-pro-v1:0',
        detection_prompt: MULTILINE_PROMPT,
      },
    });

    expect(screen.getByText('Auto-Labeling')).toBeInTheDocument();
    // The identifier renders without the `llm:` prefix, colons intact.
    expect(screen.getByText('us.amazon.nova-pro-v1:0')).toBeInTheDocument();

    // The full prompt is rendered untruncated with newlines preserved in
    // the DOM text, inside a pre-wrap span so they display as line breaks.
    const matches = screen.getAllByText(
      (_content, element) => element?.textContent === MULTILINE_PROMPT
    );
    const span = matches.find((el) => el.tagName === 'SPAN');
    expect(span).toBeDefined();
    expect(span!.style.whiteSpace).toBe('pre-wrap');
  });

  it('renders the Available/Failed counts once tasks have resolved with mixed statuses', async () => {
    await renderPage({
      ...baseDdaJob,
      auto_label: {
        enabled: true,
        model: 'llm:us.amazon.nova-pro-v1:0',
        detection_prompt: MULTILINE_PROMPT,
      },
      prelabel_available_count: 3,
      prelabel_failed_count: 1,
    });

    expect(screen.getByText('Pre-Labels Available')).toBeInTheDocument();
    expect(screen.getByText('Pre-Labels Failed')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('hides the counts entirely when no task has resolved', async () => {
    await renderPage({
      ...baseDdaJob,
      auto_label: {
        enabled: true,
        model: 'llm:us.amazon.nova-pro-v1:0',
        detection_prompt: MULTILINE_PROMPT,
      },
      prelabel_available_count: 0,
      prelabel_failed_count: 0,
    });

    // The llm configuration still shows, but neither count renders.
    expect(screen.getByText('Auto-Labeling')).toBeInTheDocument();
    expect(screen.queryByText('Pre-Labels Available')).toBeNull();
    expect(screen.queryByText('Pre-Labels Failed')).toBeNull();
  });

  it('hides the counts when the count fields are absent', async () => {
    await renderPage({
      ...baseDdaJob,
      auto_label: {
        enabled: true,
        model: 'llm:us.amazon.nova-pro-v1:0',
        detection_prompt: MULTILINE_PROMPT,
      },
    });

    expect(screen.queryByText('Pre-Labels Available')).toBeNull();
    expect(screen.queryByText('Pre-Labels Failed')).toBeNull();
  });

  it('renders neither the model nor the prompt block for a non-LLM job', async () => {
    await renderPage({
      ...baseDdaJob,
      auto_label: { enabled: true, model: 'sam' },
    });

    expect(screen.queryByText('Auto-Labeling')).toBeNull();
    expect(screen.queryByText('Model')).toBeNull();
    expect(screen.queryByText('Detection Prompt')).toBeNull();
    expect(screen.queryByText('Pre-Labels Available')).toBeNull();
  });
});
