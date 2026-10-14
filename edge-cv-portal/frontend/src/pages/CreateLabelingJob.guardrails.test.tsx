/**
 * Example tests for the job creation wizard's grounded-sam
 * Prompt_Guardrail and Prompt_Guidance surfaces
 * (grounded-sam-prompt-guardrails-and-prelabel-retry task 4.6,
 * Requirements 1.3, 3.1, 3.2, 3.3, 3.4).
 *
 * Covers, by example:
 * - a period-bearing Prompt_Override entry carries the Requirement 1.1
 *   period error as its field-level error text, clicking Next surfaces
 *   the same message as the step error, and removing the period clears
 *   the field-level error (Req 1.3);
 * - under a grounded-sam selection the override entries carry the shared
 *   PROMPT_GUIDANCE_CONSTRAINT constraint text and the
 *   PromptGuidanceContent info content, while under `sam` and `llm:`
 *   selections the entries are absent so neither renders (Req 3.1);
 * - the guidance content pins: the noun-phrase teaching with both
 *   examples and the localizes/masks explanation (Req 3.2), the
 *   instruction-style examples with does-not-work (Req 3.3), and the
 *   no-periods / commas-acceptable / empty-uses-the-label-name rules
 *   (Req 3.4).
 *
 * Mocking and wizard navigation follow the
 * `CreateLabelingJob.groundedsam.test.tsx` scaffolding (the
 * renderToDdaSetup walk, fillSetupAndEnableAutoLabel,
 * selectAutoLabelModel), with `window.localStorage.clear()` in
 * `beforeEach` so no draft persists across tests. The guidance
 * paragraphs wrap across source lines, so the content pins use
 * whitespace-tolerant regex matchers — the
 * `LabelingDetail.rerun.test.tsx` precedent for this Box-in-info
 * pattern, which jsdom renders inline.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob from './CreateLabelingJob';
import { PROMPT_GUIDANCE_CONSTRAINT } from './promptOverrideGuardrails';

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

/**
 * The Requirement 1.1 override-source error for the scaffolding's
 * "scratch" label, restated verbatim from the design (Components §1:
 * `promptGuardrailMessage`) rather than imported, so a drift in the
 * shared module fails this pin.
 */
const PERIOD_ERROR_FOR_SCRATCH =
  'The text prompt for label "scratch" contains a period. Periods separate labels in the detection caption — remove them or split the idea into a short noun phrase';

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
// Wizard navigation helpers (the CreateLabelingJob.groundedsam.test.tsx
// scaffolding)
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
    { target: { value: 'gsam-guardrail-job' } }
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
 * On the DDA setup step: pick the labeling team, fill one label
 * ("scratch"), and enable the auto-label toggle.
 */
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

/** Select an auto-label model in the (second) model select. */
function selectAutoLabelModel(container: HTMLElement, value: string) {
  const modelSelect = createWrapper(container).findAllSelects()[1];
  modelSelect.openDropdown();
  modelSelect.selectOptionByValue(value);
}

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

/**
 * The FormField wrapping one label's Prompt_Override entry, located by
 * the entry Input it contains — the FormField whose error, constraint,
 * and info slots Requirements 1.3 and 3.1 pin.
 */
function overrideFormField(container: HTMLElement, label: string) {
  const field = createWrapper(container)
    .findAllFormFields()
    .find(
      (ff) =>
        ff
          .getElement()
          .querySelector(`input[aria-label="Text prompt for ${label}"]`) !==
        null
    );
  if (!field) {
    throw new Error(
      `No Prompt_Override FormField for label ${JSON.stringify(label)}`
    );
  }
  return field;
}

// ---------------------------------------------------------------------------
// Field-level error mirrors the step error (Req 1.3)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — period error is the entry\'s field-level error and the step error (Req 1.3)', () => {
  it('shows the error on the offending entry, mirrors it on Next, and clears it when the period is removed', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    selectAutoLabelModel(container, 'grounded-sam');

    // Typing a period-bearing override displays the Requirement 1.1
    // error as the entry's field-level error text, before any Next.
    const input = screen.getByLabelText('Text prompt for scratch');
    fireEvent.change(input, {
      target: { value: 'a deep scratch. thin hairline crack' },
    });
    expect(
      overrideFormField(container, 'scratch')
        .findError()!
        .getElement().textContent
    ).toBe(PERIOD_ERROR_FOR_SCRATCH);
    // No step error yet: the error alert renders only when Next is tried.
    expect(screen.queryByText("Couldn't create the labeling job")).toBeNull();

    // Next: the step is blocked and the step error surfaces the same
    // message the entry already carries.
    clickNext();
    await waitFor(() => {
      const alert = createWrapper(container).findAlert();
      expect(alert?.findHeader()?.getElement().textContent).toBe(
        "Couldn't create the labeling job"
      );
      expect(alert?.findContent()?.getElement().textContent).toBe(
        PERIOD_ERROR_FOR_SCRATCH
      );
    });
    // Still on the setup step, the field-level error still in place.
    expect(
      screen.getByText('Model-assisted pre-labeling')
    ).toBeInTheDocument();
    expect(
      overrideFormField(container, 'scratch')
        .findError()!
        .getElement().textContent
    ).toBe(PERIOD_ERROR_FOR_SCRATCH);

    // Removing the period clears the field-level error reactively (the
    // dismissible step alert lingers until the next navigation).
    fireEvent.change(input, {
      target: { value: 'a deep scratch, thin hairline crack' },
    });
    expect(overrideFormField(container, 'scratch').findError()).toBeNull();

    // The corrected comma-bearing entry advances: the review step's
    // Create Job action replaces the Next button.
    clickNext();
    expect(
      await screen.findByRole('button', { name: 'Create Job' })
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Guidance rendering: constraint text and info affordance (Req 3.1)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — guidance renders under grounded-sam only (Req 3.1)', () => {
  it('carries the constraint text and info content on the entries, and neither under sam or llm:', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);

    // grounded-sam: the entry's constraint slot carries exactly the
    // shared module's PROMPT_GUIDANCE_CONSTRAINT string, and its info
    // slot carries the guidance content.
    selectAutoLabelModel(container, 'grounded-sam');
    const field = overrideFormField(container, 'scratch');
    expect(field.findConstraint()!.getElement().textContent).toBe(
      PROMPT_GUIDANCE_CONSTRAINT
    );
    expect(field.findInfo()!.getElement().textContent).toMatch(
      /short noun phrase naming the visual thing/
    );
    expect(screen.getByText(PROMPT_GUIDANCE_CONSTRAINT)).toBeInTheDocument();
    // The preview now mounts under grounded-sam too
    // (grounded-sam-prompt-tuning-preview Req 10.2): let its listing
    // settle before the model switch unmounts it.
    await screen.findByTestId('preview-prefix-empty');

    // sam: no override entry renders, so neither the constraint text nor
    // the guidance content does.
    selectAutoLabelModel(container, 'sam');
    expect(overrideInputs(container)).toHaveLength(0);
    expect(screen.queryByText(PROMPT_GUIDANCE_CONSTRAINT)).toBeNull();
    expect(
      screen.queryByText(/short noun phrase naming the visual thing/)
    ).toBeNull();

    // llm:: the family's own controls render instead; no entry, no
    // guidance. Let the preview's listing settle before asserting.
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    await screen.findByTestId('preview-prefix-empty');
    expect(overrideInputs(container)).toHaveLength(0);
    expect(screen.queryByText(PROMPT_GUIDANCE_CONSTRAINT)).toBeNull();
    expect(
      screen.queryByText(/short noun phrase naming the visual thing/)
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Guidance content pins (Req 3.2, 3.3, 3.4)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — guidance content pins (Req 3.2, 3.3, 3.4)', () => {
  it('teaches noun phrases with both examples, rejects instructions, and states the period/comma/empty rules', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    selectAutoLabelModel(container, 'grounded-sam');

    // Req 3.2 — a prompt is a short noun phrase naming the visual thing
    // to find, with both noun-phrase examples...
    expect(
      screen.getByText(/short noun phrase naming the visual thing/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/gap between broken cookie pieces/)
    ).toBeInTheDocument();
    expect(screen.getByText(/scratch\s+on metal surface/)).toBeInTheDocument();
    // ...and the division of labor: the detector localizes what the text
    // names, the mask model turns the boxes into masks.
    expect(
      screen.getByText(
        /detector localizes what the text names,\s+and the mask model turns the resulting boxes into masks/
      )
    ).toBeInTheDocument();

    // Req 3.3 — instruction-style text does not work: the model grounds
    // noun phrases rather than following directions.
    expect(
      screen.getByText(/draw a polygon around each\s+gap/)
    ).toBeInTheDocument();
    expect(screen.getByText(/produce json/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /does not work: the model grounds noun\s+phrases rather than following directions/
      )
    ).toBeInTheDocument();

    // Req 3.4 — periods are not allowed because they separate labels,
    // commas are acceptable, and an empty entry means the label name.
    expect(
      screen.getByText(
        /Periods are not allowed because they separate labels in the\s+detection caption/
      )
    ).toBeInTheDocument();
    expect(screen.getByText(/Commas are acceptable/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /Leave an entry empty to\s+use the label name itself as the prompt/
      )
    ).toBeInTheDocument();
  });
});
