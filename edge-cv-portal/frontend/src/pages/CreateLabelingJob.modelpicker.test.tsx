/**
 * Vitest render-level example tests for the Auto_Label_Picker's
 * type-to-search surface and the picker's preservation contrasts
 * (llm-model-picker-search-and-image-filter task 2.4, Requirements 2.2,
 * 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.2, 4.6).
 *
 * Covers:
 * - the Picker_Search filter entry is present while the option list is
 *   open (Req 3.1);
 * - queries by label fragment, by raw catalog id fragment, and by case
 *   variation display exactly the matching entries (Req 3.2, 3.3);
 * - a gibberish query shows "No models match the search" (Req 3.4);
 * - type-then-clear leaves the recorded selection untouched and restores
 *   the full capability-filtered list, and selecting under a Search_Text
 *   records the same `llm:<id>` value as selecting unfiltered
 *   (Req 3.5, 2.5);
 * - a query uniquely naming a Text_Only model's label yields noMatch —
 *   search never reintroduces an excluded model (Req 3.6);
 * - field-absent (Unknown_Capability) models are offered in both
 *   families (Req 2.2);
 * - `sam` stays offered with an all-Text_Only catalog (Req 2.3);
 * - the all-Text_Only indication plus its free-text affordance drives
 *   the `llm:<id>` selection state (Req 2.4);
 * - a Text_Only model absent from both auto-label families is still
 *   present in the admin Skip_Verification_Picker (Req 4.2);
 * - a free-text-entered Text_Only model id still resolves its
 *   `image_limit` hint and `token_limit` pre-fill from the full catalog
 *   (Req 4.6).
 *
 * Mocking and wizard navigation follow `CreateLabelingJob.test.tsx` and
 * `CreateLabelingJob.fewshot.test.tsx`. Search interactions go through
 * the Cloudscape test-utils Select wrapper (`findFilteringInput`), the
 * component-owned handle for the built-in `filteringType="auto"` input.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob from './CreateLabelingJob';

/** The test-utils Select wrapper type, as returned by createWrapper. */
type SelectWrapper = NonNullable<
  ReturnType<ReturnType<typeof createWrapper>['findSelect']>
>;

const { apiMocks, navigateMock, authState } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    createLabelingJob: vi.fn(),
    listWorkteams: vi.fn(),
    getImagePreview: vi.fn(),
  },
  navigateMock: vi.fn(),
  // Mutable so single tests can present an admin (Skip_Verification is
  // admin-only); beforeEach resets it to a non-admin.
  authState: { role: 'DataScientist' },
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

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'user', role: authState.role },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useLocation: () => ({ state: undefined }),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

vi.mock('../components/S3Browser', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Fixtures — capability-mixed Model_Catalog entries
// ---------------------------------------------------------------------------

/** Image_Capable vision models (`image_input: true`). */
const NOVA = {
  id: 'us.amazon.nova-pro-v1:0',
  label: 'Nova Pro',
  image_input: true,
};
const CLAUDE = {
  id: 'us.anthropic.claude-sonnet-4-v1:0',
  label: 'Claude Sonnet',
  image_input: true,
};
/**
 * Text_Only models (`image_input: false`) — excluded from the auto-label
 * families. TITAN_EMBED carries explicit per-model limits so Req 4.6 can
 * observe full-catalog lookups for an excluded model.
 */
const TITAN_EMBED = {
  id: 'amazon.titan-embed-text-v2:0',
  label: 'Titan Embeddings',
  image_input: false,
  image_limit: 2,
  token_limit: 20000,
};
const TITAN_TEXT = {
  id: 'amazon.titan-text-express-v1',
  label: 'Titan Text Express',
  image_input: false,
};
/** Unknown_Capability (field absent) — must stay included (Req 2.2). */
const PIXTRAL_UNKNOWN = {
  id: 'eu.mistral.pixtral-large-v1:0',
  label: 'Pixtral Large',
};

/** Two Image_Capable models plus one Text_Only model. */
const MIXED_CATALOG = [NOVA, CLAUDE, TITAN_EMBED];
/** Every model positively known text-only. */
const ALL_TEXT_ONLY_CATALOG = [TITAN_TEXT, TITAN_EMBED];

const pngFile = (name: string) =>
  new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  authState.role = 'DataScientist';
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC1', s3_bucket: 'out-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingTeams.mockResolvedValue({
    teams: [{ team_id: 't-1', team_name: 'Team One', members: ['a'] }],
    count: 1,
  });
  apiMocks.getBedrockModels.mockResolvedValue({
    models: MIXED_CATALOG,
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
  // The embedded Prompt_Tuning_Preview lists dataset images while an
  // `llm:` model is selected; an empty listing keeps it settled.
  apiMocks.getImagePreview.mockResolvedValue({
    prefix: 'images/',
    bucket: 'data-bucket',
    total_found: 0,
    offset: 0,
    limit: 50,
    has_more: false,
    images: [],
    expires_in_seconds: 900,
  });
  // Cloudscape's file thumbnails go through object URLs, which jsdom lacks.
  if (!URL.createObjectURL) {
    URL.createObjectURL = () => 'blob:example';
    URL.revokeObjectURL = () => undefined;
  }
});

// ---------------------------------------------------------------------------
// Wizard navigation helpers (mirroring CreateLabelingJob.test.tsx)
// ---------------------------------------------------------------------------

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));

async function renderToDdaSetup(taskTypeValue: string) {
  const view = render(<CreateLabelingJob />);
  const wrapper = createWrapper(view.container);

  fireEvent.click(
    wrapper.findRadioGroup()!.findInputByValue('DDA')!.getElement()
  );
  clickNext();

  fireEvent.change(
    await screen.findByPlaceholderText('e.g., Defect Detection - Batch 1'),
    { target: { value: 'picker-job' } }
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

  const taskSelect = wrapper.findSelect()!;
  taskSelect.openDropdown();
  taskSelect.selectOptionByValue(taskTypeValue);
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

  // The first toggle on the step is the auto-labeling assist toggle.
  fireEvent.click(
    createWrapper(container).findAllToggles()[0].findNativeInput().getElement()
  );
}

/** The Auto_Label_Picker: the second select on the DDA setup step. */
function modelSelect(container: HTMLElement): SelectWrapper {
  return createWrapper(container).findAllSelects()[1];
}

function selectAutoLabelModel(container: HTMLElement, value: string) {
  const select = modelSelect(container);
  select.openDropdown();
  select.selectOptionByValue(value);
}

/** Type a Search_Text into the open picker's built-in filtering input. */
function typeSearch(select: SelectWrapper, text: string) {
  fireEvent.change(
    select.findFilteringInput()!.findNativeInput().getElement(),
    { target: { value: text } }
  );
}

/** The values of the option entries currently displayed in the dropdown. */
function displayedOptionCount(select: SelectWrapper): number {
  return select.findDropdown().findOptions().length;
}

const optionByValue = (select: SelectWrapper, value: string) =>
  select.findDropdown().findOptionByValue(value);

const triggerText = (select: SelectWrapper) =>
  select.findTrigger().getElement().textContent || '';

const promptField = () => screen.queryByLabelText('Detection prompt');

/** The attach/omit hint text, whitespace-normalized, or null when absent. */
function attachHint(container: HTMLElement): string | null {
  const matches = Array.from(container.querySelectorAll('div, span, p')).filter(
    (el) => /will be attached/.test(el.textContent || '')
  );
  if (matches.length === 0) return null;
  const innermost = matches.reduce((a, b) =>
    (a.textContent || '').length <= (b.textContent || '').length ? a : b
  );
  return (innermost.textContent || '').replace(/\s+/g, ' ').trim();
}

/** The native input of the preview's Token_Budget_Selection control. */
function budgetInput(): HTMLInputElement {
  return screen
    .getByTestId('preview-token-budget-input')
    .querySelector('input') as HTMLInputElement;
}

// ---------------------------------------------------------------------------
// Picker_Search (Req 3.1-3.6)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — Picker_Search', () => {
  it('presents the filter entry while the option list is open (Req 3.1)', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    const select = modelSelect(container);
    select.openDropdown();

    const filteringInput = select.findFilteringInput();
    expect(filteringInput).not.toBeNull();
    const native = filteringInput!.findNativeInput().getElement();
    expect(native.getAttribute('placeholder')).toBe(
      'Search by model name or id'
    );
    expect(native.getAttribute('aria-label')).toBe('Search models');
  });

  it('narrows by label fragment, raw id fragment, and case variation (Req 3.2, 3.3)', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    const select = modelSelect(container);
    select.openDropdown();
    // The full capability-filtered list: sam + {bedrock:, llm:} × {Nova,
    // Claude}; the Text_Only Titan is excluded up front (Req 2.1).
    expect(displayedOptionCount(select)).toBe(5);

    // Label fragment: both families' Nova entries and nothing else.
    typeSearch(select, 'nova');
    expect(displayedOptionCount(select)).toBe(2);
    expect(optionByValue(select, `bedrock:${NOVA.id}`)).not.toBeNull();
    expect(optionByValue(select, `llm:${NOVA.id}`)).not.toBeNull();
    expect(optionByValue(select, `llm:${CLAUDE.id}`)).toBeNull();
    expect(optionByValue(select, 'sam')).toBeNull();

    // Raw catalog id fragment ('us.anthropic' appears in no label): the
    // Claude entries match through the Model_Option id (Req 3.2).
    typeSearch(select, 'us.anthropic');
    expect(displayedOptionCount(select)).toBe(2);
    expect(optionByValue(select, `bedrock:${CLAUDE.id}`)).not.toBeNull();
    expect(optionByValue(select, `llm:${CLAUDE.id}`)).not.toBeNull();
    expect(optionByValue(select, `llm:${NOVA.id}`)).toBeNull();

    // Case variation: matching is case-insensitive (Req 3.2, 3.3).
    typeSearch(select, 'NOVA');
    expect(displayedOptionCount(select)).toBe(2);
    expect(optionByValue(select, `bedrock:${NOVA.id}`)).not.toBeNull();
    expect(optionByValue(select, `llm:${NOVA.id}`)).not.toBeNull();
  });

  it('shows "No models match the search" for a gibberish query (Req 3.4)', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    const select = modelSelect(container);
    select.openDropdown();
    typeSearch(select, 'zzz-no-such-model');

    expect(displayedOptionCount(select)).toBe(0);
    expect(screen.getByText('No models match the search')).toBeInTheDocument();
  });

  it('leaves the recorded selection untouched across type-then-clear and records the same llm: value under search (Req 3.5, 2.5)', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);
    const select = modelSelect(container);

    // Select with no Search_Text entered.
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(triggerText(select)).toContain('Nova Pro (prompt-guided)');
    expect(promptField()).toBeInTheDocument();

    // Typing narrows the display only: the recorded selection is
    // untouched (Req 3.5).
    select.openDropdown();
    typeSearch(select, 'claude');
    expect(displayedOptionCount(select)).toBe(2);
    expect(triggerText(select)).toContain('Nova Pro (prompt-guided)');
    expect(promptField()).toBeInTheDocument();

    // Clearing restores the full set of offered entries — still the
    // capability-filtered set, never the excluded Titan (Req 3.5, 3.6).
    typeSearch(select, '');
    expect(displayedOptionCount(select)).toBe(5);
    expect(optionByValue(select, `llm:${TITAN_EMBED.id}`)).toBeNull();
    select.closeDropdown();
    expect(triggerText(select)).toContain('Nova Pro (prompt-guided)');
    expect(promptField()).toBeInTheDocument();

    // Selecting the same entry unfiltered and under a Search_Text records
    // the same selection (Req 2.5, 3.5).
    selectAutoLabelModel(container, `llm:${CLAUDE.id}`);
    const unfilteredTrigger = triggerText(select);

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    select.openDropdown();
    typeSearch(select, 'us.anthropic');
    select.selectOptionByValue(`llm:${CLAUDE.id}`);
    expect(triggerText(select)).toBe(unfilteredTrigger);

    // The submitted payload carries the canonical `llm:<id>` value.
    fireEvent.change(promptField()!, { target: { value: 'find scratches' } });
    clickNext();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Create Job' })
    );
    await waitFor(() => {
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1);
    });
    expect(
      apiMocks.createLabelingJob.mock.calls[0][0].auto_label.model
    ).toBe(`llm:${CLAUDE.id}`);
  });

  it('yields no match for a query uniquely naming a Text_Only model (Req 3.6)', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    const select = modelSelect(container);
    select.openDropdown();
    // 'titan' names exactly the Text_Only Titan Embeddings entry, which
    // the capability filter excluded — search must not reintroduce it.
    typeSearch(select, 'titan');

    expect(displayedOptionCount(select)).toBe(0);
    expect(optionByValue(select, `llm:${TITAN_EMBED.id}`)).toBeNull();
    expect(optionByValue(select, `bedrock:${TITAN_EMBED.id}`)).toBeNull();
    expect(screen.getByText('No models match the search')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Capability filtering of the auto-label families (Req 2.2, 2.3, 2.4)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — auto-label family capability filtering', () => {
  it('offers field-absent (Unknown_Capability) models in both families (Req 2.2)', async () => {
    apiMocks.getBedrockModels.mockResolvedValue({
      models: [NOVA, PIXTRAL_UNKNOWN, TITAN_TEXT],
      region: 'us-east-1',
    });
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    const select = modelSelect(container);
    select.openDropdown();

    // The unknown-capability model is included in both families; only the
    // positively known Text_Only model is excluded.
    expect(optionByValue(select, `bedrock:${PIXTRAL_UNKNOWN.id}`)).not.toBeNull();
    expect(optionByValue(select, `llm:${PIXTRAL_UNKNOWN.id}`)).not.toBeNull();
    expect(optionByValue(select, `bedrock:${NOVA.id}`)).not.toBeNull();
    expect(optionByValue(select, `llm:${NOVA.id}`)).not.toBeNull();
    expect(optionByValue(select, `bedrock:${TITAN_TEXT.id}`)).toBeNull();
    expect(optionByValue(select, `llm:${TITAN_TEXT.id}`)).toBeNull();
    expect(displayedOptionCount(select)).toBe(5);
  });

  it('keeps sam offered when every catalog model is Text_Only (Req 2.3)', async () => {
    apiMocks.getBedrockModels.mockResolvedValue({
      models: ALL_TEXT_ONLY_CATALOG,
      region: 'us-east-1',
    });
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    const select = modelSelect(container);
    select.openDropdown();

    // The sam entry derives from the modality matrix alone; both catalog
    // families are empty.
    expect(optionByValue(select, 'sam')).not.toBeNull();
    expect(displayedOptionCount(select)).toBe(1);
  });

  it('shows the all-Text_Only indication whose free-text entry drives the llm: selection (Req 2.4)', async () => {
    apiMocks.getBedrockModels.mockResolvedValue({
      models: ALL_TEXT_ONLY_CATALOG,
      region: 'us-east-1',
    });
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);

    // The catalog loaded (so the Catalog_Unavailable notice is absent)
    // but every model is Text_Only: the new indication appears with the
    // Free_Text_Fallback identifier entry.
    expect(
      screen.getByText(/No model in the catalog accepts image input/)
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/The model catalog is unavailable/)
    ).toBeNull();
    const freeText = screen.getByLabelText('Prompt-guided model identifier');

    // Typing an identifier selects the llm: model (the Detection prompt
    // field appears), and the submission records `llm:<id>`.
    expect(promptField()).toBeNull();
    fireEvent.change(freeText, { target: { value: NOVA.id } });
    expect(promptField()).toBeInTheDocument();
    fireEvent.change(promptField()!, { target: { value: 'find scratches' } });

    clickNext();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Create Job' })
    );
    await waitFor(() => {
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1);
    });
    expect(
      apiMocks.createLabelingJob.mock.calls[0][0].auto_label.model
    ).toBe(`llm:${NOVA.id}`);
  });
});

// ---------------------------------------------------------------------------
// Preservation contrasts (Req 4.2, 4.6)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — preservation of full-catalog consumers', () => {
  it('offers a Text_Only model in the Skip_Verification_Picker while both auto-label families exclude it (Req 4.2)', async () => {
    authState.role = 'PortalAdmin';
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    // Both auto-label families exclude the Text_Only Titan.
    const autoLabelSelect = modelSelect(container);
    autoLabelSelect.openDropdown();
    expect(optionByValue(autoLabelSelect, `bedrock:${TITAN_EMBED.id}`)).toBeNull();
    expect(optionByValue(autoLabelSelect, `llm:${TITAN_EMBED.id}`)).toBeNull();
    autoLabelSelect.closeDropdown();

    // Enable skip verification (admin-only): after the auto-label assist
    // toggle, the skip-verification toggle is the second on the step.
    fireEvent.click(
      createWrapper(container)
        .findAllToggles()[1]
        .findNativeInput()
        .getElement()
    );

    // The Skip_Verification_Picker keeps the full catalog, Text_Only
    // included, with raw (unprefixed) Model_Option ids.
    const skipSelect = createWrapper(container)
      .findAllSelects()
      .find((s) =>
        (s.findTrigger().getElement().textContent || '').includes(
          'Select a Bedrock model'
        )
      );
    expect(skipSelect).toBeDefined();
    skipSelect!.openDropdown();
    expect(optionByValue(skipSelect!, TITAN_EMBED.id)).not.toBeNull();
    expect(optionByValue(skipSelect!, NOVA.id)).not.toBeNull();
    expect(optionByValue(skipSelect!, CLAUDE.id)).not.toBeNull();
    expect(displayedOptionCount(skipSelect!)).toBe(3);
  });

  it('resolves the image_limit hint and token_limit pre-fill from the full catalog for a free-text Text_Only id (Req 4.6)', async () => {
    // The only catalog model is Text_Only (excluded from the families,
    // so the free-text affordance is offered) and carries explicit
    // per-model limits.
    apiMocks.getBedrockModels.mockResolvedValue({
      models: [TITAN_EMBED],
      region: 'us-east-1',
    });
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);

    // Three stored example images for the attach/omit hint.
    const uploads = createWrapper(container).findAllFileUploads();
    fireEvent.change(uploads[0].findNativeInput().getElement(), {
      target: {
        files: [
          pngFile('good-1.png'),
          pngFile('good-2.png'),
          pngFile('good-3.png'),
        ],
      },
    });

    // Enter the Text_Only model's id through the free-text affordance.
    const freeText = screen.getByLabelText('Prompt-guided model identifier');
    fireEvent.change(freeText, { target: { value: TITAN_EMBED.id } });
    expect(promptField()).toBeInTheDocument();

    // Token_Budget_Selection pre-fills from the full catalog's
    // token_limit — not the filtered families, which are empty (Req 4.6).
    expect(budgetInput().value).toBe('20000');

    // Enable the few-shot option: the attach/omit hint resolves the
    // model's image_limit (2) from the full catalog: one slot is reserved
    // for the dataset image, so 1 of 3 examples attaches.
    fireEvent.click(
      createWrapper(container)
        .findAllToggles()[1]
        .findNativeInput()
        .getElement()
    );
    expect(attachHint(container)).toBe(
      '1 of 3 examples will be attached, 2 omitted (this model accepts 2 images per request, one reserved for the dataset image).'
    );
  });
});
