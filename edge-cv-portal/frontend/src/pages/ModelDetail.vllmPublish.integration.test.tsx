/**
 * Integration and backward-compatibility regression tests for the vLLM
 * Package & Publish mount on the Model Detail page
 * (vllm-package-publish-gui, task 7.2).
 *
 * Rendered against the real `ModelDetail` page with the real
 * `VllmPackagePublishSection` / controller / state module, with only
 * `apiService`, the router hooks, the auth context, and `CompilationTab`
 * mocked:
 *
 * - live record refresh: a completion poll result flows through
 *   `onModelUpdate` into the page's `model` state, so the page's
 *   architecture derivation renders one badge per supported
 *   architecture in place of the placeholder, and the placeholder is
 *   retained when the published component's list is empty
 *   (Requirements 2.3, 2.4, 2.8, 4.4);
 * - vision-record regression: `trained` / `imported` records load the
 *   CompilationTab through the existing path, render no vLLM section,
 *   and start no vLLM polling (Requirements 1.3, 5.1, 5.4, 5.5);
 * - vLLM records render the section and never mount CompilationTab
 *   (Requirements 1.3, 5.1).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import ModelDetail from './ModelDetail';
import { POLL_INTERVAL_MS } from '../components/vllm-publish/publishState';

// ------------------------------------------------------------------ mocks

const {
  startPackaging,
  publishGreengrassComponent,
  getModel,
  getTrainingJob,
  otherApiCalls,
} = vi.hoisted(() => ({
  startPackaging: vi.fn(),
  publishGreengrassComponent: vi.fn(),
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
  // Any apiService method beyond the four the page and the vLLM
  // controller may call is recorded so tests can assert no other
  // endpoint was hit (Requirement 5.2).
  const apiService = new Proxy(
    { startPackaging, publishGreengrassComponent, getModel, getTrainingJob },
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

// The page reads only `user?.role` from the auth context (Req 1.6).
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

// Stub CompilationTab so its mount is directly observable and its own
// API calls / polling cannot interfere with the vLLM assertions.
vi.mock('../components/CompilationTab', () => ({
  default: () => <div data-testid="compilation-tab" />,
}));

// -------------------------------------------------------------- fixtures

/** Registered vLLM record with no packaged/published state: the
 *  Supported Architectures section renders the placeholder. */
const vllmRecord = {
  model_id: 'model-123',
  usecase_id: 'usecase-1',
  name: 'My LLM',
  version: '1.0',
  stage: 'candidate',
  source: 'vllm',
  training_job_id: 'training-456',
  model_type: 'vllm',
  metrics: {},
  component_arns: {},
  deployed_devices: [],
  created_by: 'tester',
  created_at: 1700000000000,
  updated_at: 1700000000000,
};

/** Polled record after a completed first publish: the write-back map
 *  carries the supported architectures the badges render from. */
const publishedRecord = {
  ...vllmRecord,
  packaged_components: [
    {
      target: 'jetson-xavier-jp6',
      status: 'packaged',
      supported_architectures: ['arm64_jp6'],
    },
  ],
  published_component: {
    component_name: 'model-vllm-my-llm',
    component_version: '1.0.0',
    supported_architectures: ['arm64_jp6'],
    runtime: 'vllm',
    component_arns: {
      'jetson-xavier-jp6': 'arn:aws:greengrass:us-east-1:123:component',
    },
    published_at: 1700000100000,
  },
};

/** Completion write-back whose `supported_architectures` list is empty:
 *  the placeholder must be retained (Requirement 2.8). */
const publishedRecordEmptyArchitectures = {
  ...vllmRecord,
  packaged_components: [{ target: 'jetson-xavier-jp6', status: 'packaged' }],
  published_component: {
    ...publishedRecord.published_component,
    supported_architectures: [] as string[],
  },
};

const visionRecord = (source: 'trained' | 'imported') => ({
  ...vllmRecord,
  source,
  model_type: 'object-detection',
});

const PACKAGING_ACCEPTED_RESPONSE = {
  training_id: 'training-456',
  packaged_components: publishedRecord.packaged_components,
  message: 'ok',
  component_creation_triggered: true,
};

const PLACEHOLDER_TEXT =
  'Supported architectures are recorded when the model component is packaged and published.';

// --------------------------------------------------------------- helpers

const wrapper = () => createWrapper(document.body);
const actionButton = () =>
  wrapper().findButton('[data-testid="vllm-publish-action"]')!;

/** Render the page and flush the initial getModel (and any
 *  getTrainingJob) load effects. */
async function renderPage() {
  const rendered = render(<ModelDetail />);
  await act(async () => {});
  return rendered;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

// ------------------------------------------------------------------ tests

describe('ModelDetail — live record refresh on publish completion (Req 2.3, 2.4, 4.4)', () => {
  it('a completion poll result updates the page record: badges replace the placeholder and the published state renders', async () => {
    // Initial page load: bare vLLM record → placeholder, no badges.
    getModel.mockResolvedValueOnce({ model: vllmRecord });
    await renderPage();

    expect(screen.getByText('Supported Architectures')).not.toBeNull();
    expect(screen.getByText(PLACEHOLDER_TEXT)).not.toBeNull();
    expect(screen.queryByText('arm64_jp6')).toBeNull();
    expect(screen.queryByTestId('vllm-published-section')).toBeNull();

    // Activation succeeds; every subsequent poll observes the record
    // carrying the fresh published_component.
    startPackaging.mockResolvedValueOnce(PACKAGING_ACCEPTED_RESPONSE);
    getModel.mockResolvedValue({ model: publishedRecord });

    await act(async () => {
      actionButton().click();
    });
    // First poll tick delivers the completed record to onModelUpdate.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    // The page's architecture derivation now renders the badge and the
    // placeholder is gone (Req 2.4, 4.4).
    expect(screen.getByText('arm64_jp6')).not.toBeNull();
    expect(screen.queryByText(PLACEHOLDER_TEXT)).toBeNull();

    // The published state renders from the refreshed record (Req 2.3).
    const published = screen.getByTestId('vllm-published-section');
    expect(published.textContent).toContain('model-vllm-my-llm');
    expect(published.textContent).toContain('1.0.0');

    // Only the expected endpoints were hit: the initial load plus the
    // completing poll, packaging once, and no training-job load.
    expect(startPackaging).toHaveBeenCalledTimes(1);
    expect(getModel).toHaveBeenCalledTimes(2);
    expect(getModel).toHaveBeenCalledWith('model-123');
    expect(getTrainingJob).not.toHaveBeenCalled();
    expect(publishGreengrassComponent).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });

  it('retains the placeholder when the completed published_component has an empty supported_architectures list (Req 2.8)', async () => {
    getModel.mockResolvedValueOnce({ model: vllmRecord });
    await renderPage();

    startPackaging.mockResolvedValueOnce(PACKAGING_ACCEPTED_RESPONSE);
    getModel.mockResolvedValue({ model: publishedRecordEmptyArchitectures });

    await act(async () => {
      actionButton().click();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    // Placeholder retained: no badge values exist to render.
    expect(screen.getByText(PLACEHOLDER_TEXT)).not.toBeNull();
    expect(screen.queryByText('arm64_jp6')).toBeNull();

    // The component name and version still display (Req 2.8).
    const published = screen.getByTestId('vllm-published-section');
    expect(published.textContent).toContain('model-vllm-my-llm');
    expect(published.textContent).toContain('1.0.0');
    expect(otherApiCalls).toEqual([]);
  });
});

describe.each(['trained', 'imported'] as const)(
  'ModelDetail — %s vision-record regression (Req 1.3, 5.1, 5.4, 5.5)',
  (source) => {
    it('shows CompilationTab through the existing path, no vLLM section, and starts no vLLM polling', async () => {
      getModel.mockResolvedValue({ model: visionRecord(source) });
      getTrainingJob.mockResolvedValue({
        training_id: 'training-456',
        status: 'COMPLETED',
      });
      await renderPage();

      // The existing trained/imported path loads the training job and
      // mounts CompilationTab unchanged (Req 1.3, 5.1, 5.5).
      expect(getTrainingJob).toHaveBeenCalledTimes(1);
      expect(getTrainingJob).toHaveBeenCalledWith('training-456');
      expect(screen.getByTestId('compilation-tab')).not.toBeNull();

      // No vLLM action, section, or architecture section renders.
      expect(screen.queryByTestId('vllm-publish-section')).toBeNull();
      expect(screen.queryByTestId('vllm-publish-action')).toBeNull();
      expect(screen.queryByTestId('vllm-publish-retry')).toBeNull();
      expect(screen.queryByText('Supported Architectures')).toBeNull();
      expect(screen.queryByText('Component Packaging & Publish')).toBeNull();

      // No vLLM polling starts: well past several would-be poll
      // intervals, the model was read only by the initial page load
      // (Req 5.4) and no packaging/publish endpoint was hit.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 6);
      });
      expect(getModel).toHaveBeenCalledTimes(1);
      expect(startPackaging).not.toHaveBeenCalled();
      expect(publishGreengrassComponent).not.toHaveBeenCalled();
      expect(otherApiCalls).toEqual([]);
    });
  }
);

describe('ModelDetail — vLLM record mounts the section and never CompilationTab (Req 1.3, 5.1)', () => {
  it('renders the vLLM section with the Package & Publish action and loads no training job', async () => {
    getModel.mockResolvedValue({ model: vllmRecord });
    await renderPage();

    // The vLLM section renders below the Supported Architectures
    // section with the first-publish action label.
    expect(screen.getByTestId('vllm-publish-section')).not.toBeNull();
    expect(screen.getByText('Supported Architectures')).not.toBeNull();
    expect(actionButton().getElement().textContent).toContain(
      'Package & Publish'
    );

    // CompilationTab never mounts for vLLM records: the trained/
    // imported training-job path is not entered.
    expect(getTrainingJob).not.toHaveBeenCalled();
    expect(screen.queryByTestId('compilation-tab')).toBeNull();

    // Page load alone starts no polling and hits no other endpoint.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 6);
    });
    expect(getModel).toHaveBeenCalledTimes(1);
    expect(startPackaging).not.toHaveBeenCalled();
    expect(publishGreengrassComponent).not.toHaveBeenCalled();
    expect(otherApiCalls).toEqual([]);
  });
});
