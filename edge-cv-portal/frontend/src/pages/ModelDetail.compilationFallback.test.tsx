/**
 * Integration tests for the ModelDetail fallback compilation-jobs table
 * (onnx-compile-error-diagnostics, task 5.3).
 *
 * This is the surface the reported URL renders when the underlying training
 * job did NOT load: `trainingJob` stays null, CompilationTab never mounts,
 * and the page falls back to the inline compilation-jobs table built from
 * the model record itself. These tests verify the table's new Reason column
 * surfaces the preserved diagnostics instead of a bare status token:
 *
 * - the Reason column renders `failure_reason || error`, with `poll_error`
 *   as secondary text (Requirements 2.14, 2.15);
 * - status comparison is case-insensitive and `ERROR` gets an explanatory
 *   badge instead of the raw token (Requirements 2.16, 2.17).
 *
 * Mock structure follows ModelDetail.vllmPublish.integration.test.tsx.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';

import ModelDetail from './ModelDetail';

// ------------------------------------------------------------------ mocks

const { getModel, getTrainingJob, otherApiCalls } = vi.hoisted(() => ({
  getModel: vi.fn(),
  getTrainingJob: vi.fn(),
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
  const apiService = new Proxy(
    { getModel, getTrainingJob },
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

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      user_id: 'user-1',
      email: 'user@example.com',
      username: 'tester',
      role: 'DataScientist',
      is_super_user: false,
    },
  }),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ modelId: 'model-123' }),
  useNavigate: () => vi.fn(),
}));

// Stub CompilationTab so its (non-)mount is directly observable: on this
// surface the training job fails to load, so it must never appear.
vi.mock('../components/CompilationTab', () => ({
  default: () => <div data-testid="compilation-tab" />,
}));

// -------------------------------------------------------------- fixtures

/** ONNX export that never started: the preserved originating reason. */
const ONNX_ERROR =
  'ValidationException: Role arn:aws:iam::123456789012:role/DDASageMakerExecutionRole not found';

/** Neo entry latched by a legacy poll fault, reason preserved separately. */
const NEO_FAILURE_REASON =
  'ClientError: InputConfiguration: Unable to load provided PyTorch model';
const NEO_POLL_ERROR = 'ThrottlingException: Rate exceeded';

/** Vision record whose compilation_jobs carry the preserved diagnostics.
 *  Its training job will fail to load, forcing the fallback table. */
const visionRecord = {
  model_id: 'model-123',
  usecase_id: 'usecase-1',
  name: 'Defect Detector',
  version: '1.0',
  stage: 'candidate',
  source: 'trained',
  training_job_id: 'training-456',
  model_type: 'object-detection',
  metrics: {},
  component_arns: {},
  deployed_devices: [],
  created_by: 'tester',
  created_at: 1700000000000,
  updated_at: 1700000000000,
  compilation_jobs: [
    {
      target: 'onnx',
      status: 'Failed',
      job_started: false,
      error: ONNX_ERROR,
    },
    {
      target: 'jetson-xavier-jp6',
      status: 'ERROR',
      failure_reason: NEO_FAILURE_REASON,
      poll_error: NEO_POLL_ERROR,
    },
  ],
};

// --------------------------------------------------------------- helpers

/** Render the page with the training-job load failing, and flush the
 *  initial load effects so the fallback table renders. */
async function renderFallbackPage() {
  getModel.mockResolvedValue({ model: visionRecord });
  getTrainingJob.mockRejectedValue(new Error('training job not found'));
  const rendered = render(<ModelDetail />);
  await act(async () => {});
  return rendered;
}

afterEach(() => {
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

// ------------------------------------------------------------------ tests

describe('ModelDetail — fallback compilation table Reason column (Req 2.14, 2.15, 2.16, 2.17)', () => {
  it('renders the fallback table with the preserved reasons when the training job did not load', async () => {
    await renderFallbackPage();

    // The training-job load was attempted and failed, so the fallback
    // table renders instead of CompilationTab.
    expect(getTrainingJob).toHaveBeenCalledWith('training-456');
    expect(screen.queryByTestId('compilation-tab')).toBeNull();
    expect(screen.getByText('Compilation Jobs')).not.toBeNull();
    expect(screen.getByText('Reason')).not.toBeNull();

    // The ONNX no-live-job entry's originating error is rendered in the
    // Reason column (2.14, 2.15).
    expect(screen.getByText(ONNX_ERROR)).not.toBeNull();

    // The Neo entry's preserved failure_reason is rendered even though its
    // status is the transient 'ERROR' (2.14, 2.15).
    expect(screen.getByText(NEO_FAILURE_REASON)).not.toBeNull();

    // The poll fault renders as secondary text under its own label, never
    // conflated with the originating reason.
    expect(
      screen.getByText(`Status lookup error: ${NEO_POLL_ERROR}`)
    ).not.toBeNull();
  });

  it('classifies statuses case-insensitively and never renders a bare ERROR token (Req 2.16, 2.17)', async () => {
    await renderFallbackPage();

    // Mixed-case 'Failed' reaches the Failed arm via uppercase
    // normalization (2.16).
    expect(screen.getByText('Failed')).not.toBeNull();

    // The 'ERROR' status renders the explanatory badge, not the raw
    // token (2.17).
    expect(
      screen.getByText('Status unavailable — retrying')
    ).not.toBeNull();
    expect(screen.queryByText('ERROR')).toBeNull();
  });
});
