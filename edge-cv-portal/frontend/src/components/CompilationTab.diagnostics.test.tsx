/**
 * Integration tests for CompilationTab's diagnostic rendering
 * (onnx-compile-error-diagnostics, task 5.3).
 *
 * CompilationTab had no test suite before this spec. These tests render the
 * real component with only `apiService` mocked and verify that a preserved
 * reason is always surfaced instead of a bare status token:
 *
 * - a job with status `ERROR` and a preserved `error` renders the reason and
 *   never a bare `ERROR` token (Requirements 2.14, 2.17);
 * - a Neo job with the uppercase `FAILED` the backend actually writes renders
 *   its `failure_reason` — previously hidden by the exact-match
 *   `status === 'Failed'` filter (Requirements 2.14, 2.16);
 * - a job with no `compilation_job_name` (an ONNX export that never started)
 *   renders "not started" instead of an empty cell (Requirement 2.17).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';

import CompilationTab from './CompilationTab';
import { TrainingJob } from '../types';

// ------------------------------------------------------------------ mocks

const { getCompilationStatus, otherApiCalls } = vi.hoisted(() => ({
  getCompilationStatus: vi.fn(),
  otherApiCalls: [] as string[],
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  // Any apiService method beyond getCompilationStatus is recorded so the
  // tests can assert no other endpoint was hit by rendering alone.
  const apiService = new Proxy(
    { getCompilationStatus },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => {
          otherApiCalls.push(String(prop));
          return Promise.resolve({});
        };
      },
    }
  );
  return { ApiError, apiService };
});

// -------------------------------------------------------------- fixtures

const baseTrainingJob = {
  training_id: 'training-456',
  usecase_id: 'usecase-1',
  model_name: 'defect-model',
  model_version: '1',
  dataset_manifest_s3: 's3://bucket/manifest.json',
  algorithm_uri: 'algo',
  hyperparameters: {},
  instance_type: 'ml.p3.2xlarge',
  training_job_arn:
    'arn:aws:sagemaker:us-east-1:123456789012:training-job/training-456',
  status: 'Completed',
  metrics: {},
  artifact_s3: 's3://bucket/artifact.tar.gz',
  created_by: 'tester',
  created_at: 1700000000000,
  compilation_jobs: [],
} as unknown as TrainingJob;

/** A poll fault against a live Neo job: the originating `error` is preserved
 *  and the lookup failure is recorded separately in `poll_error`. */
const errorStatusJob = {
  target: 'jetson-xavier-jp6',
  compilation_job_name: 'defect-model-jetson-xavier-jp6',
  status: 'ERROR',
  error:
    'AccessDeniedException: User is not authorized to perform sagemaker:CreateTrainingJob',
  poll_error: 'ThrottlingException: Rate exceeded',
};

/** A Neo job stored with SageMaker's verbatim uppercase FAILED and the
 *  describe response's FailureReason. */
const neoFailedJob = {
  target: 'jetson-xavier',
  compilation_job_name: 'defect-model-jetson-xavier',
  status: 'FAILED',
  failure_reason:
    'ClientError: InputConfiguration: Unable to load provided PyTorch model',
};

/** An ONNX export whose start call raised: no live SageMaker job exists, so
 *  the entry carries no compilation_job_name at all. */
const onnxNotStartedJob = {
  target: 'onnx',
  export_format: 'onnx',
  status: 'Failed',
  job_started: false,
  error:
    'ValidationException: Role arn:aws:iam::123456789012:role/DDASageMakerExecutionRole not found',
  failed_step: 'start_onnx_export_job',
};

// --------------------------------------------------------------- helpers

/** Render the tab with the given jobs (served by the mocked status poll)
 *  and flush the mount-time refresh effect. */
async function renderTab(jobs: object[]) {
  getCompilationStatus.mockResolvedValue({ compilation_jobs: jobs });
  const trainingJob = {
    ...baseTrainingJob,
    compilation_jobs: jobs,
  } as TrainingJob;
  const rendered = render(
    <CompilationTab trainingId="training-456" trainingJob={trainingJob} />
  );
  await act(async () => {});
  return rendered;
}

beforeEach(() => {
  // The tab polls every 15 s while a job is non-terminal ('ERROR' is
  // non-terminal by design); fake timers keep the interval inert.
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

// ------------------------------------------------------------------ tests

describe('CompilationTab — diagnostic rendering (Req 2.14, 2.16, 2.17)', () => {
  it('renders the preserved error for a status ERROR job, never a bare token (Req 2.14, 2.17)', async () => {
    await renderTab([errorStatusJob]);

    // The preserved originating reason is surfaced in the errors panel.
    expect(screen.getByText('Compilation Errors')).not.toBeNull();
    expect(
      screen.getByText(new RegExp(errorStatusJob.error))
    ).not.toBeNull();

    // The poll fault is rendered under its own distinct label so it is never
    // conflated with the originating reason.
    expect(screen.getByText(/Status lookup error:/)).not.toBeNull();
    expect(
      screen.getAllByText(new RegExp(errorStatusJob.poll_error)).length
    ).toBeGreaterThan(0);

    // The status cell explains the transient fault instead of the raw token.
    expect(
      screen.getByText(/Status unavailable — retrying/)
    ).not.toBeNull();

    // No element anywhere renders the bare token 'ERROR' alone.
    expect(screen.queryByText('ERROR')).toBeNull();

    // Rendering alone hits only the status poll.
    expect(getCompilationStatus).toHaveBeenCalledWith('training-456');
    expect(otherApiCalls).toEqual([]);
  });

  it('renders the failure_reason for an uppercase Neo FAILED job (Req 2.14, 2.16)', async () => {
    await renderTab([neoFailedJob]);

    // Case-insensitive classification: the uppercase FAILED the Neo path
    // actually writes reaches the Failed arm.
    expect(screen.getByText('Failed')).not.toBeNull();

    // The errors panel includes the job — the old exact-match
    // `status === 'Failed'` filter always excluded uppercase FAILED.
    expect(screen.getByText('Compilation Errors')).not.toBeNull();
    expect(
      screen.getByText('jetson-xavier Compilation Failed')
    ).not.toBeNull();
    expect(
      screen.getByText(new RegExp(neoFailedJob.failure_reason))
    ).not.toBeNull();

    // No bare uppercase token renders.
    expect(screen.queryByText('FAILED')).toBeNull();
    expect(otherApiCalls).toEqual([]);
  });

  it('renders "not started" for a job with no compilation_job_name (Req 2.17)', async () => {
    await renderTab([onnxNotStartedJob]);

    // Both the job-name column and the errors panel say "not started"
    // instead of an empty cell for the no-live-job entry.
    expect(screen.getAllByText(/not started/).length).toBeGreaterThan(0);

    // The preserved originating reason and the failed step are surfaced.
    expect(
      screen.getByText(new RegExp(onnxNotStartedJob.error))
    ).not.toBeNull();
    expect(screen.getByText(/Failed step:/)).not.toBeNull();
    expect(
      screen.getByText(new RegExp(onnxNotStartedJob.failed_step))
    ).not.toBeNull();
    expect(otherApiCalls).toEqual([]);
  });
});
