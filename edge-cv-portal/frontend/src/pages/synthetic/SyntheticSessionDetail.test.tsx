/**
 * Unit tests for the SyntheticSessionDetail review workspace
 * (synthetic-defect-data-generation, task 7.6).
 *
 * - A thumbnail appears from a poll response without a page reload (Req 5.2)
 * - Pass-tagged comparison of regenerated results (Req 5.4)
 * - Full-size view with the retained prompt text (Req 5.5, 5.6)
 * - Per-item and bulk approval controls (Req 6.1, 6.2)
 * - Zero-approved confirmation rejection (Req 6.5)
 * - Approval summary dialog with count / target dataset / Defect_Type (Req 6.4)
 * - Integration banner with manifest URI + count and retrain pre-population
 *   (Req 7.6, 8.1); retrain failure keeps the manifest URI (Req 8.4)
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type {
  SyntheticPreview,
  SyntheticSession,
  SyntheticSessionDetailResponse,
} from './types';

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useParams: () => ({ sessionId: 's-1' }) };
});

vi.mock('../../services/api', () => ({
  apiService: {
    getSyntheticSession: vi.fn(),
    generateSyntheticPreviews: vi.fn(),
    setSyntheticPreviewApproval: vi.fn(),
    integrateSyntheticSession: vi.fn(),
    retrainSyntheticSession: vi.fn(),
  },
}));

import { apiService } from '../../services/api';
import SyntheticSessionDetail, {
  POLL_INTERVAL_MS,
  ZERO_APPROVED_MESSAGE,
} from './SyntheticSessionDetail';

const mocked = vi.mocked(apiService);

function makeSession(overrides: Partial<SyntheticSession> = {}): SyntheticSession {
  return {
    session_id: 's-1',
    usecase_id: 'uc-1',
    status: 'awaiting_review',
    generation_model_id: 'amazon.nova-canvas-v1:0',
    object_type: 'metal casting',
    defect_type: 'scratch',
    prompt_template_text: 'A {object_type} with a {defect_type}',
    generation_pass: 1,
    generation_plan: [{}, {}],
    target_dataset_prefix: 'datasets/castings/',
    target_manifest_key: 'datasets/castings/manifests/train.manifest',
    created_at: 1730000000000,
    ...overrides,
  };
}

function makePreview(
  id: string,
  overrides: Partial<SyntheticPreview> = {}
): SyntheticPreview {
  return {
    preview_id: id,
    source_image_key: 'datasets/castings/img1.png',
    variation_index: 0,
    generation_pass: 1,
    status: 'completed',
    approval_state: 'pending',
    resolved_prompt: 'A metal casting with a scratch',
    seed: 42,
    thumbnail_url: `https://example.com/${id}.png`,
    ...overrides,
  };
}

function respond(response: SyntheticSessionDetailResponse) {
  mocked.getSyntheticSession.mockResolvedValue(response);
}

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  if (!window.matchMedia) {
    (window as any).matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('SyntheticSessionDetail review workspace', () => {
  it('shows new thumbnails from poll responses without a reload (Req 5.2)', async () => {
    vi.useFakeTimers();
    try {
      const generatingSession = makeSession({ status: 'generating' });
      mocked.getSyntheticSession
        .mockResolvedValueOnce({
          session: generatingSession,
          previews: [makePreview('p1')],
        })
        .mockResolvedValue({
          session: generatingSession,
          previews: [makePreview('p1'), makePreview('p2')],
        });

      render(<SyntheticSessionDetail />);
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByTestId('preview-p1')).toBeInTheDocument();
      expect(screen.queryByTestId('preview-p2')).not.toBeInTheDocument();

      // The next poll delivers a second completed variation; it appears
      // without any navigation or reload (Req 5.2).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
        await Promise.resolve();
      });
      expect(screen.getByTestId('preview-p2')).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('tags previews with their generation pass for comparison (Req 5.4)', async () => {
    respond({
      session: makeSession({ generation_pass: 2 }),
      previews: [
        makePreview('p1', { generation_pass: 1 }),
        makePreview('p2', { generation_pass: 2 }),
      ],
    });
    render(<SyntheticSessionDetail />);
    expect(await screen.findByText('Pass 1')).toBeInTheDocument();
    expect(screen.getByText('Pass 2')).toBeInTheDocument();
  });

  it('opens a full-size view showing the prompt used for that preview (Req 5.5, 5.6)', async () => {
    respond({
      session: makeSession(),
      previews: [
        makePreview('p1', { resolved_prompt: 'exact prompt used for p1' }),
      ],
    });
    render(<SyntheticSessionDetail />);
    await userEvent.click(
      await screen.findByRole('button', { name: /view preview p1/i })
    );
    expect(await screen.findByTestId('lightbox-prompt')).toHaveTextContent(
      'exact prompt used for p1'
    );
    expect(
      screen.getByAltText('Full-size preview p1')
    ).toHaveAttribute('src', 'https://example.com/p1.png');
  });

  it('supports per-item approve/reject (Req 6.1)', async () => {
    respond({ session: makeSession(), previews: [makePreview('p1')] });
    mocked.setSyntheticPreviewApproval.mockResolvedValue({
      updated: 1,
      approval_state: 'approved',
    });
    render(<SyntheticSessionDetail />);
    await userEvent.click(
      await screen.findByRole('button', { name: /approve preview p1/i })
    );
    expect(mocked.setSyntheticPreviewApproval).toHaveBeenCalledWith('s-1', {
      approval_state: 'approved',
      preview_ids: ['p1'],
    });

    await userEvent.click(
      await screen.findByRole('button', { name: /reject preview p1/i })
    );
    expect(mocked.setSyntheticPreviewApproval).toHaveBeenCalledWith('s-1', {
      approval_state: 'rejected',
      preview_ids: ['p1'],
    });
  });

  it('supports bulk approve/reject of all previews (Req 6.2)', async () => {
    respond({
      session: makeSession(),
      previews: [makePreview('p1'), makePreview('p2')],
    });
    mocked.setSyntheticPreviewApproval.mockResolvedValue({
      updated: 2,
      approval_state: 'approved',
    });
    render(<SyntheticSessionDetail />);
    await userEvent.click(
      await screen.findByRole('button', { name: /^approve all$/i })
    );
    expect(mocked.setSyntheticPreviewApproval).toHaveBeenCalledWith('s-1', {
      approval_state: 'approved',
      all: true,
    });
  });

  it('rejects confirmation with zero approved previews (Req 6.5)', async () => {
    respond({
      session: makeSession(),
      previews: [makePreview('p1', { approval_state: 'pending' })],
    });
    render(<SyntheticSessionDetail />);
    await userEvent.click(
      await screen.findByRole('button', { name: /integrate approved images/i })
    );
    expect(await screen.findByText(ZERO_APPROVED_MESSAGE)).toBeInTheDocument();
    expect(mocked.integrateSyntheticSession).not.toHaveBeenCalled();
    // No confirmation dialog opens.
    expect(screen.queryByTestId('confirm-approved-count')).not.toBeInTheDocument();
  });

  it('shows the approval summary with count, target dataset, and Defect_Type (Req 6.4)', async () => {
    respond({
      session: makeSession(),
      previews: [
        makePreview('p1', { approval_state: 'approved' }),
        makePreview('p2', { approval_state: 'approved' }),
        makePreview('p3', { approval_state: 'rejected' }),
      ],
    });
    render(<SyntheticSessionDetail />);
    await userEvent.click(
      await screen.findByRole('button', { name: /integrate approved images/i })
    );
    expect(await screen.findByTestId('confirm-approved-count')).toHaveTextContent('2');
    expect(screen.getByTestId('confirm-target-dataset')).toHaveTextContent(
      'datasets/castings/'
    );
    expect(screen.getByTestId('confirm-defect-type')).toHaveTextContent('scratch');
  });

  it('shows the integration banner with manifest URI + count and pre-populates retraining (Req 7.6, 8.1)', async () => {
    respond({
      session: makeSession({
        status: 'integrated',
        integration_result: {
          manifest_uri: 's3://bucket/datasets/castings/manifests/train.manifest',
          appended_count: 3,
          at: 1730000001000,
        },
      }),
      previews: [makePreview('p1', { approval_state: 'approved' })],
    });
    render(<SyntheticSessionDetail />);

    // Banner: manifest URI + appended record count (Req 7.6).
    expect(await screen.findByText(/integration complete/i)).toBeInTheDocument();
    expect(
      screen.getByText('s3://bucket/datasets/castings/manifests/train.manifest')
    ).toBeInTheDocument();
    expect(screen.getByText(/appended 3 records to/i)).toBeInTheDocument();

    // Retraining action pre-populated with the manifest URI (Req 8.1).
    await userEvent.click(screen.getByRole('button', { name: /start retraining/i }));
    expect(await screen.findByLabelText('Dataset manifest')).toHaveValue(
      's3://bucket/datasets/castings/manifests/train.manifest'
    );
  });

  it('surfaces retrain failure while keeping the manifest URI available (Req 8.4)', async () => {
    respond({
      session: makeSession({
        status: 'integrated',
        integration_result: {
          manifest_uri: 's3://bucket/m.manifest',
          appended_count: 1,
          at: 1730000001000,
        },
      }),
      previews: [],
    });
    mocked.retrainSyntheticSession.mockRejectedValue(
      new Error('ResourceLimitExceeded: training quota reached')
    );
    render(<SyntheticSessionDetail />);
    await userEvent.click(
      await screen.findByRole('button', { name: /start retraining/i })
    );
    await userEvent.type(
      await screen.findByLabelText('Model name'),
      'synthetic-model'
    );
    await userEvent.click(
      screen.getByRole('button', { name: /create training job/i })
    );

    // Failure reason displayed; manifest URI retained for retry (Req 8.4).
    expect(
      await screen.findByText(/training job creation failed/i)
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(/ResourceLimitExceeded: training quota reached/i).length
    ).toBeGreaterThan(0);
    await waitFor(() =>
      expect(screen.getAllByText(/s3:\/\/bucket\/m\.manifest/).length).toBeGreaterThan(0)
    );
  });

  it('displays session and per-variation failure reasons (Req 1.4, 4.5)', async () => {
    respond({
      session: makeSession({
        last_failure: { reason: 'Bedrock throttled the request', at: 1 },
      }),
      previews: [
        makePreview('p1'),
        makePreview('p2', {
          status: 'failed',
          failure_reason: 'Model returned no image',
          thumbnail_url: undefined,
        }),
      ],
    });
    render(<SyntheticSessionDetail />);
    expect(
      await screen.findByText('Bedrock throttled the request')
    ).toBeInTheDocument();
    expect(screen.getByText(/model returned no image/i)).toBeInTheDocument();
  });
});
