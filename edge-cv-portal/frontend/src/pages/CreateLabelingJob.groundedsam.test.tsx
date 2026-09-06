/**
 * Vitest example tests for the job creation wizard's Grounded-SAM
 * auto-labeling additions (grounded-sam-autolabel task 4.6, Requirements
 * 1.3, 1.4, 2.1, 2.2, 2.6, 6.2, 7.3).
 *
 * Covers, by example:
 * - the `isAutoLabelModelCompatible('grounded-sam', ...)` matrix: true
 *   for Segmentation and ObjectDetection, false for Classification
 *   (Req 1.3);
 * - a recorded grounded-sam selection is cleared through the existing
 *   incompatible-selection clearing effect when the modality switches to
 *   Classification (Req 1.4);
 * - under a grounded-sam selection exactly one Prompt_Override entry per
 *   effective Label_Set label renders, each with the label name as its
 *   placeholder; no entry renders for `sam` or `llm:` selections (or
 *   none) (Req 2.1, 2.2);
 * - a >256-character override blocks the setup step with an error naming
 *   the label, and exactly 256 characters is accepted (Req 2.6);
 * - none of the `llm:`-only controls (detection prompt, few-shot,
 *   sizing, prompt tuning preview) render for a grounded-sam selection
 *   (Req 7.3);
 * - a seeded Setup_Draft carrying `groundedSamPromptOverrides` restores
 *   the entries into the controls exactly as saved and the subsequent
 *   submit payload carries the surviving overrides (Req 6.2).
 *
 * Mocking and wizard navigation follow the `CreateLabelingJob.test.tsx`
 * scaffolding; the draft seeding follows
 * `CreateLabelingJob.recovery.test.tsx` (`makeDraft`/`seedDraft` against
 * the `draft-restore-offer`/`draft-restore-button` test ids), with
 * `window.localStorage.clear()` in `beforeEach` so no draft persists
 * across tests.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob, {
  GROUNDED_SAM_MODALITIES,
  MAX_PROMPT_OVERRIDE_LENGTH,
  isAutoLabelModelCompatible,
} from './CreateLabelingJob';
import type { LabelingJobDraft } from './labelingJobDraft';
import { labelingJobDraftKey } from './labelingJobDraft';

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

/** Settled empty listing for the real PromptTuningPreview (llm: only). */
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
// isAutoLabelModelCompatible — the grounded-sam matrix (Req 1.3)
// ---------------------------------------------------------------------------

describe('isAutoLabelModelCompatible — grounded-sam (Req 1.3)', () => {
  it('allows grounded-sam only for Segmentation and ObjectDetection', () => {
    expect(isAutoLabelModelCompatible('grounded-sam', 'Segmentation')).toBe(
      true
    );
    expect(isAutoLabelModelCompatible('grounded-sam', 'ObjectDetection')).toBe(
      true
    );
    expect(isAutoLabelModelCompatible('grounded-sam', 'Classification')).toBe(
      false
    );
  });

  it('matches the exported modality list and override length limit', () => {
    expect(GROUNDED_SAM_MODALITIES).toEqual([
      'Segmentation',
      'ObjectDetection',
    ]);
    expect(MAX_PROMPT_OVERRIDE_LENGTH).toBe(256);
  });
});

// ---------------------------------------------------------------------------
// Wizard navigation helpers (the CreateLabelingJob.test.tsx scaffolding)
// ---------------------------------------------------------------------------

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
const clickPrevious = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Previous' }));

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
    { target: { value: 'gsam-job' } }
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

  fireEvent.click(wrapper.findToggle()!.findNativeInput().getElement());
}

/** Select an auto-label model in the (second) model select. */
function selectAutoLabelModel(container: HTMLElement, value: string) {
  const modelSelect = createWrapper(container).findAllSelects()[1];
  modelSelect.openDropdown();
  modelSelect.selectOptionByValue(value);
}

/** The auto-label model select's trigger text (selection or placeholder). */
const modelTriggerText = (container: HTMLElement) =>
  createWrapper(container)
    .findAllSelects()[1]
    .findTrigger()
    .getElement().textContent || '';

/** The labeling-team select's trigger text. */
const teamTriggerText = (container: HTMLElement) =>
  createWrapper(container)
    .findAllSelects()[0]
    .findTrigger()
    .getElement().textContent || '';

/** The `llm:`-only Detection prompt field, by its aria-label. */
const promptField = () => screen.queryByLabelText('Detection prompt');

/**
 * The Prompt_Override entry inputs, in render order. Each entry's Input
 * carries `ariaLabel={'Text prompt for ' + label}`, so the attribute
 * query counts exactly the override entries and nothing else.
 */
const overrideInputs = (container: HTMLElement) =>
  Array.from(
    container.querySelectorAll<HTMLInputElement>(
      'input[aria-label^="Text prompt for "]'
    )
  );

// ---------------------------------------------------------------------------
// Setup_Draft helpers (the CreateLabelingJob.recovery.test.tsx scaffolding)
// ---------------------------------------------------------------------------

/**
 * A conforming Setup_Draft a previous session would have written: DDA
 * branch on the labeling setup step, team t-1, Segmentation. Tests
 * override the fields their scenario varies.
 */
function makeDraft(
  overrides: Partial<LabelingJobDraft> = {}
): LabelingJobDraft {
  return {
    version: 1,
    savedAtMs: Date.now(),
    usecaseId: 'uc-1',
    activeStepIndex: 4,
    labelingBackend: 'DDA',
    jobName: 'draft-job',
    description: 'saved mid-setup',
    datasetS3Uri: 's3://bucket/images/',
    maskPrefix: '',
    taskTypeValue: 'Segmentation',
    workforceTypeValue: 'private',
    labelCategories: '',
    gtInstructions: '',
    enableAutomatedLabeling: false,
    ddaLabels: ['scratch'],
    ddaInstructions: '',
    selectedTeam: { teamId: 't-1', teamName: 'Team One' },
    autoLabelEnabled: false,
    autoLabelModel: '',
    detectionPrompt: '',
    fewShotEnabled: false,
    downscaleMaxEdge: null,
    tokenBudget: '',
    skipVerification: false,
    skipVerificationModelId: '',
    perLabelPrompts: {},
    exampleRefs: { good: [], bad: [] },
    previewSelectedKeys: [],
    previewRun: null,
    ...overrides,
  };
}

function seedDraft(draft: LabelingJobDraft): void {
  window.localStorage.setItem(
    labelingJobDraftKey(draft.usecaseId),
    JSON.stringify(draft)
  );
}

// ---------------------------------------------------------------------------
// Incompatible-selection clearing on a modality switch (Req 1.4)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — grounded-sam cleared on switching to Classification (Req 1.4)', () => {
  it('clears the recorded selection through the existing clearing effect', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    selectAutoLabelModel(container, 'grounded-sam');

    // The selection is recorded and its override block is on screen.
    expect(modelTriggerText(container)).toContain(
      'Grounded-SAM (text-prompted)'
    );
    expect(overrideInputs(container)).toHaveLength(1);

    // Back to the task-type step and over to Classification.
    clickPrevious();
    const taskSelect = createWrapper(container).findSelect()!;
    taskSelect.openDropdown();
    taskSelect.selectOptionByValue('Classification');
    clickNext();
    await screen.findByText('Model-assisted pre-labeling');

    // The incompatible selection was dropped: the model select is back to
    // its placeholder and no override entry survives the clearing.
    expect(modelTriggerText(container)).toContain(
      'Select an auto-label model'
    );
    expect(overrideInputs(container)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Prompt_Override entry rendering (Req 2.1, 2.2)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — override entries render exactly under grounded-sam (Req 2.1, 2.2)', () => {
  it('renders one entry per label with label-name placeholders, and none for sam or llm:', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container); // Label 1: scratch
    fireEvent.click(screen.getByRole('button', { name: 'Add label' }));
    fireEvent.change(screen.getByPlaceholderText('Label 2'), {
      target: { value: 'dent' },
    });

    // No model selected: no override entries (Req 2.2).
    expect(overrideInputs(container)).toHaveLength(0);

    // The sam family renders none (Req 2.2).
    selectAutoLabelModel(container, 'sam');
    expect(overrideInputs(container)).toHaveLength(0);

    // grounded-sam: exactly one optional entry per effective Label_Set
    // label, in order, with the label name as the placeholder (Req 2.1).
    selectAutoLabelModel(container, 'grounded-sam');
    const inputs = overrideInputs(container);
    expect(inputs).toHaveLength(2);
    expect(inputs.map((input) => input.getAttribute('aria-label'))).toEqual([
      'Text prompt for scratch',
      'Text prompt for dent',
    ]);
    expect(inputs.map((input) => input.getAttribute('placeholder'))).toEqual([
      'scratch',
      'dent',
    ]);

    // The llm: family renders none either (Req 2.2).
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(overrideInputs(container)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Over-length override rejected at validation, naming the label (Req 2.6)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — over-length override blocks the setup step (Req 2.6)', () => {
  it('rejects a 257-character override with an error naming the label and accepts 256', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    selectAutoLabelModel(container, 'grounded-sam');

    const input = screen.getByLabelText('Text prompt for scratch');
    fireEvent.change(input, {
      target: { value: 'x'.repeat(MAX_PROMPT_OVERRIDE_LENGTH + 1) },
    });
    clickNext();
    // The rejection names the label; the message surfaces both as the
    // entry's field error and as the step-level error.
    expect(
      (
        await screen.findAllByText(
          `The text prompt for label "scratch" exceeds ${MAX_PROMPT_OVERRIDE_LENGTH} characters`
        )
      ).length
    ).toBeGreaterThan(0);
    // Still on the setup step.
    expect(
      screen.getByText('Model-assisted pre-labeling')
    ).toBeInTheDocument();

    // Exactly 256 characters is accepted: the wizard advances to the
    // review step, whose Create Job action replaces the Next button.
    fireEvent.change(input, {
      target: { value: 'y'.repeat(MAX_PROMPT_OVERRIDE_LENGTH) },
    });
    clickNext();
    expect(
      await screen.findByRole('button', { name: 'Create Job' })
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// No llm:-only controls for grounded-sam (Req 7.3)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — no llm-only controls render for grounded-sam (Req 7.3)', () => {
  it('shows detection prompt, few-shot, sizing and preview for llm: and none of them for grounded-sam', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);

    // Positive control: the llm-only surface is present under llm:, so
    // the absence assertions below cannot pass vacuously.
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(promptField()).toBeInTheDocument();
    expect(
      screen.getByText('Attach example images as few-shot examples')
    ).toBeInTheDocument();
    expect(
      await screen.findByTestId('prompt-tuning-preview')
    ).toBeInTheDocument();
    expect(screen.getByTestId('preview-sizing-controls')).toBeInTheDocument();
    // Let the preview's listing settle before unmounting it.
    await screen.findByTestId('preview-prefix-empty');

    // grounded-sam: none of the llm-only controls render...
    selectAutoLabelModel(container, 'grounded-sam');
    expect(promptField()).toBeNull();
    expect(
      screen.queryByText('Attach example images as few-shot examples')
    ).toBeNull();
    expect(screen.queryByTestId('prompt-tuning-preview')).toBeNull();
    expect(screen.queryByTestId('preview-sizing-controls')).toBeNull();
    expect(screen.queryByLabelText('Image downscaling')).toBeNull();
    expect(screen.queryByLabelText('Output token budget')).toBeNull();
    // ...while the family's own override entries do.
    expect(overrideInputs(container)).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Setup_Draft restore of the overrides (Req 6.2)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — a seeded draft restores the overrides (Req 6.2)', () => {
  it('returns groundedSamPromptOverrides to the controls exactly as saved and submits the surviving entries', async () => {
    const OVERRIDE = 'a shallow scratch mark, hairline';
    seedDraft(
      makeDraft({
        ddaLabels: ['scratch', 'dent'],
        autoLabelEnabled: true,
        autoLabelModel: 'grounded-sam',
        // The dent entry is whitespace-only: restored verbatim into its
        // control, pruned from the submit payload.
        groundedSamPromptOverrides: { scratch: OVERRIDE, dent: '   ' },
      })
    );
    const { container } = render(<CreateLabelingJob />);
    await screen.findByTestId('draft-restore-offer');
    fireEvent.click(screen.getByTestId('draft-restore-button'));
    await waitFor(() =>
      expect(
        screen.queryByTestId('draft-restore-offer')
      ).not.toBeInTheDocument()
    );

    // The setup step is on screen with the grounded-sam selection and the
    // override entries restored exactly as saved (Req 6.2).
    await screen.findByText('Model-assisted pre-labeling');
    expect(modelTriggerText(container)).toContain(
      'Grounded-SAM (text-prompted)'
    );
    expect(screen.getByLabelText('Text prompt for scratch')).toHaveValue(
      OVERRIDE
    );
    expect(screen.getByLabelText('Text prompt for dent')).toHaveValue('   ');

    // The restored team re-selects once the team list loads, unblocking
    // the step validation.
    await waitFor(() =>
      expect(teamTriggerText(container)).toContain('Team One')
    );

    // Advance to review and submit: the payload's auto_label carries
    // exactly the surviving override, character-for-character.
    clickNext();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Create Job' })
    );
    await waitFor(() =>
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
    );
    const payload = apiMocks.createLabelingJob.mock.calls[0][0];
    expect(payload.task_type).toBe('Segmentation');
    expect(payload.team_id).toBe('t-1');
    expect(payload.auto_label).toEqual({
      enabled: true,
      model: 'grounded-sam',
      prompt_overrides: { scratch: OVERRIDE },
    });
    expect(navigateMock).toHaveBeenCalledWith('/labeling');
  });
});
