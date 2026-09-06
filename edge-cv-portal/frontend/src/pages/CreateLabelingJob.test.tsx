/**
 * Vitest tests for the job creation wizard's prompt-guided LLM
 * auto-labeling additions (llm-auto-labeling task 13.2, Requirements
 * 1.1, 1.2, 1.4, 2.1, 2.2).
 *
 * Covers:
 * - `isAutoLabelModelCompatible` across the three model families (sam,
 *   bedrock:, llm:) and the three modalities (Req 1.3 matrix as surfaced
 *   through 1.1);
 * - Detection_Prompt gating through the wizard UI (validateDdaSetup is
 *   internal to the component): with an `llm:` model selected, the DDA
 *   setup step blocks progression on an empty, whitespace-only, or
 *   2001-character prompt and accepts a 2000-character one (Req 2.1, 2.2);
 * - the Detection prompt field renders only for `llm:` selections
 *   (Req 2.1);
 * - the catalog-unavailable notice plus free-text identifier entry appear
 *   when the model catalog fails to load (Req 1.4);
 * - the submitted payload carries `detection_prompt` character-for-character
 *   (Req 2.2, and the wire shape of 1.2).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob, {
  BEDROCK_MODALITIES,
  LLM_MODALITIES,
  MAX_DETECTION_PROMPT_LENGTH,
  SAM_MODALITIES,
  isAutoLabelModelCompatible,
} from './CreateLabelingJob';

const { apiMocks, navigateMock } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    createLabelingJob: vi.fn(),
    listWorkteams: vi.fn(),
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
      // Any other API call the page happens to make resolves to an empty
      // object so effects settle without error.
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

// A non-admin user: the skip-verification section stays hidden, keeping
// the DDA setup step to exactly the controls under test.
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

// The S3 browser modal is irrelevant here and drags in its own effects.
vi.mock('../components/S3Browser', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const NOVA = { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' };

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
});

// ---------------------------------------------------------------------------
// isAutoLabelModelCompatible — the model/modality matrix (Req 1.1, 1.3)
// ---------------------------------------------------------------------------

describe('isAutoLabelModelCompatible', () => {
  const MODALITIES = ['Classification', 'Segmentation', 'ObjectDetection'];

  it('allows SAM only for Segmentation and ObjectDetection', () => {
    expect(isAutoLabelModelCompatible('sam', 'Segmentation')).toBe(true);
    expect(isAutoLabelModelCompatible('sam', 'ObjectDetection')).toBe(true);
    expect(isAutoLabelModelCompatible('sam', 'Classification')).toBe(false);
  });

  it('allows bedrock: models only for Classification and ObjectDetection', () => {
    const model = `bedrock:${NOVA.id}`;
    expect(isAutoLabelModelCompatible(model, 'Classification')).toBe(true);
    expect(isAutoLabelModelCompatible(model, 'ObjectDetection')).toBe(true);
    expect(isAutoLabelModelCompatible(model, 'Segmentation')).toBe(false);
  });

  it('allows llm: models for all three modalities', () => {
    const model = `llm:${NOVA.id}`;
    for (const modality of MODALITIES) {
      expect(isAutoLabelModelCompatible(model, modality)).toBe(true);
    }
  });

  it('rejects unknown model families and empty values for every modality', () => {
    for (const modality of MODALITIES) {
      expect(isAutoLabelModelCompatible('', modality)).toBe(false);
      expect(isAutoLabelModelCompatible('yolo', modality)).toBe(false);
    }
  });

  it('matches the exported modality lists', () => {
    // The matrix constants the wizard builds its option groups from.
    expect(SAM_MODALITIES).toEqual(['Segmentation', 'ObjectDetection']);
    expect(BEDROCK_MODALITIES).toEqual(['Classification', 'ObjectDetection']);
    expect(LLM_MODALITIES).toEqual([
      'Classification',
      'Segmentation',
      'ObjectDetection',
    ]);
    expect(MAX_DETECTION_PROMPT_LENGTH).toBe(2000);
  });
});

// ---------------------------------------------------------------------------
// Wizard navigation helpers
// ---------------------------------------------------------------------------

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));

/**
 * Render the wizard and walk it to the DDA Labeling Setup step with the
 * given task type: DDA backend -> job name (use case auto-selected) ->
 * dataset S3 URI -> task type.
 */
async function renderToDdaSetup(taskTypeValue: string) {
  const view = render(<CreateLabelingJob />);
  const wrapper = createWrapper(view.container);

  // Step 0: choose the DDA backend.
  fireEvent.click(
    wrapper.findRadioGroup()!.findInputByValue('DDA')!.getElement()
  );
  clickNext();

  // Step 1: job name; the single use case auto-selects from the API.
  fireEvent.change(
    await screen.findByPlaceholderText('e.g., Defect Detection - Batch 1'),
    { target: { value: 'llm-job' } }
  );
  await waitFor(() => {
    expect(
      wrapper.findSelect()!.findTrigger().getElement().textContent
    ).toContain('UC1');
  });
  clickNext();

  // Step 2: dataset S3 URI.
  fireEvent.change(
    await screen.findByPlaceholderText(
      'e.g., s3://my-bucket/raw-images/production-line-1/'
    ),
    { target: { value: 's3://bucket/images/' } }
  );
  clickNext();

  // Step 3: task type.
  const taskSelect = wrapper.findSelect()!;
  taskSelect.openDropdown();
  taskSelect.selectOptionByValue(taskTypeValue);
  clickNext();

  // Step 4: the DDA labeling setup step is on screen.
  await screen.findByText('Model-assisted pre-labeling');
  return view;
}

/**
 * On the DDA setup step: pick the labeling team, fill one label when the
 * modality needs a label set, and enable the auto-label toggle.
 */
async function fillSetupAndEnableAutoLabel(
  container: HTMLElement,
  { needsLabels = true }: { needsLabels?: boolean } = {}
) {
  const wrapper = createWrapper(container);
  const teamSelect = wrapper.findAllSelects()[0];
  await waitFor(() => {
    expect(teamSelect.findTrigger().getElement()).not.toBeDisabled();
  });
  teamSelect.openDropdown();
  teamSelect.selectOptionByValue('t-1');

  if (needsLabels) {
    fireEvent.change(screen.getByPlaceholderText('Label 1'), {
      target: { value: 'scratch' },
    });
  }

  fireEvent.click(
    wrapper.findToggle()!.findNativeInput().getElement()
  );
}

/** Select an auto-label model in the (second) model select. */
function selectAutoLabelModel(container: HTMLElement, value: string) {
  const modelSelect = createWrapper(container).findAllSelects()[1];
  modelSelect.openDropdown();
  modelSelect.selectOptionByValue(value);
}

const promptField = () => screen.queryByLabelText('Detection prompt');

// ---------------------------------------------------------------------------
// Detection_Prompt gating through the wizard (Req 2.1, 2.2)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — Detection_Prompt gating (Req 2.1, 2.2)', () => {
  it('blocks progression on empty, whitespace-only, and over-length prompts and accepts a 2000-character one', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    const prompt = promptField();
    expect(prompt).toBeInTheDocument();

    // Empty prompt: submission past the step is prevented with the
    // required-prompt indication (Req 2.1).
    clickNext();
    expect(
      await screen.findByText(
        'A detection prompt is required for prompt-guided auto-labeling'
      )
    ).toBeInTheDocument();
    // Still on the setup step.
    expect(promptField()).toBeInTheDocument();

    // Whitespace-only prompt is treated as empty (Req 2.2).
    fireEvent.change(prompt!, { target: { value: '  \n\t  ' } });
    clickNext();
    expect(
      await screen.findByText(
        'A detection prompt is required for prompt-guided auto-labeling'
      )
    ).toBeInTheDocument();
    expect(promptField()).toBeInTheDocument();

    // 2001 characters: rejected with the length violation (Req 2.2).
    fireEvent.change(prompt!, {
      target: { value: 'x'.repeat(MAX_DETECTION_PROMPT_LENGTH + 1) },
    });
    clickNext();
    expect(
      (
        await screen.findAllByText(
          `The detection prompt exceeds ${MAX_DETECTION_PROMPT_LENGTH.toLocaleString()} characters`
        )
      ).length
    ).toBeGreaterThan(0);
    expect(promptField()).toBeInTheDocument();

    // Exactly 2000 characters is accepted: the wizard advances to the
    // review step (Req 2.2).
    fireEvent.change(prompt!, {
      target: { value: 'x'.repeat(MAX_DETECTION_PROMPT_LENGTH) },
    });
    clickNext();
    expect(await screen.findByText('Detection Prompt')).toBeInTheDocument();
    expect(promptField()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Prompt field visibility per model family (Req 2.1)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — prompt field renders only for llm: selections', () => {
  it('hides the field for sam and bedrock: and shows it for llm:', async () => {
    // ObjectDetection is the one modality where all three families are
    // offered side by side.
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    // No model selected yet: no prompt field.
    expect(promptField()).toBeNull();

    selectAutoLabelModel(container, 'sam');
    expect(promptField()).toBeNull();

    selectAutoLabelModel(container, `bedrock:${NOVA.id}`);
    expect(promptField()).toBeNull();

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(promptField()).toBeInTheDocument();

    // Switching back away from the llm: family removes the field again.
    selectAutoLabelModel(container, 'sam');
    expect(promptField()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Catalog-unavailable degradation (Req 1.4)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — model catalog unavailable (Req 1.4)', () => {
  it('shows the unavailable notice with free-text identifier entry, which drives the prompt field', async () => {
    apiMocks.getBedrockModels.mockRejectedValue(new Error('boom'));
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);

    // The inline unavailable indication and the free-text entry (Req 1.4).
    expect(
      await screen.findByText(/The model catalog is unavailable/)
    ).toBeInTheDocument();
    const freeText = screen.getByLabelText('Prompt-guided model identifier');
    expect(freeText).toBeInTheDocument();

    // Typing an identifier selects the llm: model, so the Detection
    // prompt field appears.
    expect(promptField()).toBeNull();
    fireEvent.change(freeText, { target: { value: NOVA.id } });
    expect(promptField()).toBeInTheDocument();

    // Clearing the identifier clears the selection again.
    fireEvent.change(freeText, { target: { value: '' } });
    expect(promptField()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Submitted payload (Req 1.2 wire shape, 2.2 character-for-character)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — submitted payload carries detection_prompt', () => {
  it('sends auto_label { enabled, model, detection_prompt } with the prompt preserved verbatim', async () => {
    const PROMPT = '  Find surface scratches.\nAlso "dents" {major}.  ';
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    fireEvent.change(promptField()!, { target: { value: PROMPT } });

    // To the review step, then submit.
    clickNext();
    expect(await screen.findByText('Detection Prompt')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Create Job' }));

    await waitFor(() => {
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1);
    });
    expect(apiMocks.createLabelingJob).toHaveBeenCalledWith(
      expect.objectContaining({
        usecase_id: 'uc-1',
        labeling_backend: 'DDA',
        task_type: 'Segmentation',
        team_id: 't-1',
        auto_label: {
          enabled: true,
          model: `llm:${NOVA.id}`,
          detection_prompt: PROMPT,
          // The untouched sizing controls submit their defaults for the
          // `llm:` family: a blank downscale select is `null`
          // (Downscale_Off) and the budget pre-fill — the catalog carries
          // no token_limit here, so the 10000 fallback — travels as the
          // Token_Budget_Selection (llm-model-token-and-image-sizing
          // Req 3.1, 3.6, 5.7).
          downscale_max_edge: null,
          token_budget: 10000,
        },
      })
    );
    expect(navigateMock).toHaveBeenCalledWith('/labeling');
  });
});
