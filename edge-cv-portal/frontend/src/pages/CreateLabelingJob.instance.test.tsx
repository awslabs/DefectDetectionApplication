/**
 * The job creation wizard's `InstanceSegmentation` modality
 * (rfdetr-seg-support task 2.4; Requirements 2.1, 2.4, 2.6).
 *
 * Covers:
 * - the task-type option is offered on the DDA branch with the "available
 *   in DDA labeling only" description and is absent on the SageMaker Ground
 *   Truth branch (Req 2.6);
 * - `isAutoLabelModelCompatible` is true for `sam`, `grounded-sam` and
 *   `llm:<id>` with InstanceSegmentation and false for `bedrock:<id>`
 *   (Req 2.4 — the frontend mirror of AUTO_LABEL_MODEL_MODALITIES);
 * - the auto-label select for an InstanceSegmentation job lists the SAM,
 *   Grounded-SAM and prompt-guided entries and no Bedrock entry (Req 2.4);
 * - the Prompt_Tuning_Preview panel mounts for `grounded-sam` and for an
 *   `llm:` selection (Req 2.1 / C18).
 *
 * Scaffolding follows `CreateLabelingJob.groundedsam.test.tsx` (mocked API
 * service with a settled empty preview listing, DDA wizard walk).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob, {
  GROUNDED_SAM_MODALITIES,
  LLM_MODALITIES,
  SAM_MODALITIES,
  BEDROCK_MODALITIES,
  isAutoLabelModelCompatible,
} from './CreateLabelingJob';

const { apiMocks, navigateMock } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    createLabelingJob: vi.fn(),
    listWorkteams: vi.fn(),
    getImagePreview: vi.fn(),
  },
  navigateMock: vi.fn(),
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
    }
  }
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

vi.mock('../contexts/UsecaseContext', () => ({
  useUsecase: () => ({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  }),
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'user', role: 'DataScientist' },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useLocation: () => ({ state: undefined }),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

vi.mock('../components/S3Browser', () => ({ default: () => null }));

const NOVA = { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' };
const MODALITY = 'InstanceSegmentation';

/** Settled empty listing for the real PromptTuningPreview. */
const emptyListing = {
  prefix: 'images/',
  bucket: 'data-bucket',
  total_found: 0,
  offset: 0,
  limit: 50,
  has_more: false,
  images: [],
  expires_in_seconds: 900,
};

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC1', s3_bucket: 'out-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingTeams.mockResolvedValue({
    teams: [{ team_id: 't-1', team_name: 'Team One', members: ['a'] }],
    count: 1,
  });
  apiMocks.getBedrockModels.mockResolvedValue({
    models: [NOVA],
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
  apiMocks.getImagePreview.mockResolvedValue(emptyListing);
});

// ---------------------------------------------------------------------------
// The compatibility matrix (Req 2.4)
// ---------------------------------------------------------------------------

describe('isAutoLabelModelCompatible — InstanceSegmentation (Req 2.4)', () => {
  it('admits sam, grounded-sam and llm: and refuses bedrock:', () => {
    expect(isAutoLabelModelCompatible('sam', MODALITY)).toBe(true);
    expect(isAutoLabelModelCompatible('grounded-sam', MODALITY)).toBe(true);
    expect(isAutoLabelModelCompatible(`llm:${NOVA.id}`, MODALITY)).toBe(true);
    expect(isAutoLabelModelCompatible(`bedrock:${NOVA.id}`, MODALITY)).toBe(
      false
    );
    expect(isAutoLabelModelCompatible('', MODALITY)).toBe(false);
  });

  it('extends the three mask-producing lists and leaves bedrock alone', () => {
    expect(SAM_MODALITIES).toContain(MODALITY);
    expect(GROUNDED_SAM_MODALITIES).toContain(MODALITY);
    expect(LLM_MODALITIES).toContain(MODALITY);
    expect(BEDROCK_MODALITIES).not.toContain(MODALITY);
  });
});

// ---------------------------------------------------------------------------
// Wizard navigation helpers (the CreateLabelingJob.test.tsx scaffolding)
// ---------------------------------------------------------------------------

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));

/** Render the wizard and walk it to the task-type step for a backend. */
async function renderToTaskTypeStep(backend: 'DDA' | 'GroundTruth') {
  const view = render(<CreateLabelingJob />);
  const wrapper = createWrapper(view.container);

  fireEvent.click(
    wrapper.findRadioGroup()!.findInputByValue(backend)!.getElement()
  );
  clickNext();

  fireEvent.change(
    await screen.findByPlaceholderText('e.g., Defect Detection - Batch 1'),
    { target: { value: 'instance-job' } }
  );
  await waitFor(() => {
    expect(
      wrapper.findSelect()!.findTrigger().getElement().textContent
    ).toContain('UC1');
  });
  clickNext();

  fireEvent.change(
    await screen.findByPlaceholderText(
      'e.g., s3://my-bucket/raw-images/production-line-1/'
    ),
    { target: { value: 's3://bucket/images/' } }
  );
  clickNext();

  return { view, taskSelect: wrapper.findSelect()! };
}

/** Walk to the DDA labeling setup step with the InstanceSegmentation type. */
async function renderToDdaSetup() {
  const { view, taskSelect } = await renderToTaskTypeStep('DDA');
  taskSelect.openDropdown();
  taskSelect.selectOptionByValue(MODALITY);
  clickNext();
  await screen.findByText('Model-assisted pre-labeling');
  return view;
}

async function fillSetupAndEnableAutoLabel(container: HTMLElement) {
  const wrapper = createWrapper(container);
  const teamSelect = wrapper.findAllSelects()[0];
  await waitFor(() => {
    expect(teamSelect.findTrigger().getElement()).not.toBeDisabled();
  });
  teamSelect.openDropdown();
  teamSelect.selectOptionByValue('t-1');
  fireEvent.change(screen.getByPlaceholderText('Label 1'), {
    target: { value: 'scratch' },
  });
  fireEvent.click(wrapper.findToggle()!.findNativeInput().getElement());
}

const modelSelect = (container: HTMLElement) =>
  createWrapper(container).findAllSelects()[1];

function modelOptionOffered(container: HTMLElement, value: string): boolean {
  const select = modelSelect(container);
  select.openDropdown();
  const offered = !!select.findDropdown().findOptionByValue(value);
  select.closeDropdown();
  return offered;
}

function selectAutoLabelModel(container: HTMLElement, value: string) {
  const select = modelSelect(container);
  select.openDropdown();
  select.selectOptionByValue(value);
}

// ---------------------------------------------------------------------------
// Task-type option per backend (Req 2.6)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — the InstanceSegmentation task type (Req 2.6)', () => {
  it('is offered on the DDA branch and states that it is available in DDA labeling only', async () => {
    const { taskSelect } = await renderToTaskTypeStep('DDA');
    taskSelect.openDropdown();
    const option = taskSelect.findDropdown().findOptionByValue(MODALITY);
    expect(option).not.toBeNull();
    expect(option!.findLabel().getElement().textContent).toBe(
      'Instance Segmentation'
    );
    expect(option!.findDescription()!.getElement().textContent).toContain(
      'available in DDA labeling only'
    );
    // The three pre-feature options are still offered alongside it.
    for (const value of ['Classification', 'Segmentation', 'ObjectDetection']) {
      expect(taskSelect.findDropdown().findOptionByValue(value)).not.toBeNull();
    }
  });

  it('is absent on the SageMaker Ground Truth branch', async () => {
    const { taskSelect } = await renderToTaskTypeStep('GroundTruth');
    taskSelect.openDropdown();
    const dropdown = taskSelect.findDropdown();
    expect(dropdown.findOptionByValue(MODALITY)).toBeNull();
    expect(dropdown.findOptionByValue('ObjectDetection')).toBeNull();
    expect(dropdown.findOptionByValue('Classification')).not.toBeNull();
    expect(dropdown.findOptionByValue('Segmentation')).not.toBeNull();
    expect(screen.queryByText(/available in DDA labeling only/)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Auto-label families offered for the modality (Req 2.4) and the preview
// panel (Req 2.1)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — InstanceSegmentation pre-labelers and preview (Req 2.1, 2.4)', () => {
  it('offers SAM, Grounded-SAM and the prompt-guided entry, never a Bedrock entry', async () => {
    const { container } = await renderToDdaSetup();
    await fillSetupAndEnableAutoLabel(container);

    expect(modelOptionOffered(container, 'sam')).toBe(true);
    expect(modelOptionOffered(container, 'grounded-sam')).toBe(true);
    expect(modelOptionOffered(container, `llm:${NOVA.id}`)).toBe(true);
    expect(modelOptionOffered(container, `bedrock:${NOVA.id}`)).toBe(false);
    expect(screen.queryByText('Bedrock vision models')).toBeNull();
  });

  it('mounts the Prompt_Tuning_Preview for grounded-sam and for an llm: selection', async () => {
    const { container } = await renderToDdaSetup();
    await fillSetupAndEnableAutoLabel(container);

    expect(screen.queryByTestId('prompt-tuning-preview')).toBeNull();

    selectAutoLabelModel(container, 'grounded-sam');
    expect(
      await screen.findByTestId('prompt-tuning-preview')
    ).toBeInTheDocument();
    // The grounded-sam arm raises no modality violation for the new value.
    await screen.findByTestId('preview-prefix-empty');
    expect(
      screen.queryByText(/The grounded-sam family supports/)
    ).toBeNull();

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(screen.getByTestId('prompt-tuning-preview')).toBeInTheDocument();
    await screen.findByTestId('preview-prefix-empty');
    expect(screen.queryByText(/is not supported/)).toBeNull();

    // sam has no preview panel (unchanged).
    selectAutoLabelModel(container, 'sam');
    expect(screen.queryByTestId('prompt-tuning-preview')).toBeNull();
  });
});
