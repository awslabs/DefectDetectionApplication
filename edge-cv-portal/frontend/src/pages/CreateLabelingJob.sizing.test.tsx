/**
 * Vitest tests for the job creation wizard's sizing state — the
 * Token_Budget_Selection pre-fill and the submission shape of the two
 * sizing values (llm-model-token-and-image-sizing task 11.5,
 * Requirements 3.1, 3.2, 3.10, 5.1, 5.2).
 *
 * Covers:
 * - the budget pre-fill from the selected model's catalog `token_limit`,
 *   with the 10000 fallback when the catalog carries none (Req 3.1);
 * - replacement, not merge, on a model change: the newly selected model's
 *   budget replaces the shown value (discarding an entered one) while the
 *   Detection_Prompt, the Label_Set, the selected Sample_Images, the
 *   Few_Shot_Option and the Downscale_Setting stay untouched (Req 3.2);
 * - the submission shape: `auto_label.downscale_max_edge` and
 *   `auto_label.token_budget` for an `llm:` model, the key omitted for an
 *   empty budget entry, and neither value ever submitted for `sam` or
 *   `bedrock:` selections (Req 3.10, 5.2, plus 3.6/5.7's persistence
 *   payload).
 *
 * Mocking and wizard navigation follow `CreateLabelingJob.fewshot.test.tsx`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob, {
  MODEL_TOKEN_LIMIT_DEFAULT,
} from './CreateLabelingJob';

const { apiMocks, navigateMock, fetchMock } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    createLabelingJob: vi.fn(),
    listWorkteams: vi.fn(),
    getBatchUploadUrls: vi.fn(),
    getImagePreview: vi.fn(),
  },
  navigateMock: vi.fn(),
  fetchMock: vi.fn(),
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

// A non-admin user: the skip-verification section stays hidden, so the
// toggles on the setup step are exactly [auto-label assist, few-shot].
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

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** No `token_limit` in the catalog payload: the pre-fill falls back to 10000. */
const NOVA = { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' };
/** A model whose catalog entry carries an explicit Model_Token_Limit. */
const BUDGET_20K = { id: 'model-20k', label: 'Twenty K', token_limit: 20000 };

/** Dataset images listed by the embedded Prompt_Tuning_Preview. */
const SAMPLE_KEYS = ['images/one.jpg', 'images/two.jpg'];

const pngFile = (name: string) =>
  new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC1', s3_bucket: 'out-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingTeams.mockResolvedValue({
    teams: [{ team_id: 't-1', team_name: 'Team One', members: ['a'] }],
    count: 1,
  });
  apiMocks.getBedrockModels.mockResolvedValue({
    models: [NOVA, BUDGET_20K],
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
  apiMocks.getImagePreview.mockResolvedValue({
    prefix: 'images/',
    bucket: 'data-bucket',
    total_found: SAMPLE_KEYS.length,
    offset: 0,
    limit: 50,
    has_more: false,
    images: SAMPLE_KEYS.map((key) => ({
      key,
      filename: key.slice(key.lastIndexOf('/') + 1),
      size: 2048,
      last_modified: '2024-05-01T00:00:00Z',
      presigned_url: `https://s3.example/${key}?sig=1`,
    })),
    expires_in_seconds: 900,
  });
  apiMocks.getBatchUploadUrls.mockImplementation(
    async (
      _usecaseId: string,
      data: { prefix?: string; files: { filename: string; content_type?: string }[] }
    ) => ({
      bucket: 'examples-bucket',
      prefix: data.prefix ?? '',
      uploads: data.files.map((f) => ({
        filename: f.filename,
        key: `${data.prefix}/${f.filename}`,
        upload_url: `https://upload.example/${data.prefix}/${f.filename}`,
        content_type: f.content_type ?? 'image/png',
      })),
      expires_in: 900,
    })
  );
  fetchMock.mockResolvedValue({ ok: true, status: 200 });
  vi.stubGlobal('fetch', fetchMock);
  // Cloudscape's file thumbnails go through object URLs, which jsdom lacks.
  if (!URL.createObjectURL) {
    URL.createObjectURL = () => 'blob:example';
    URL.revokeObjectURL = () => undefined;
  }
});

// ---------------------------------------------------------------------------
// Wizard navigation helpers (mirroring CreateLabelingJob.fewshot.test.tsx)
// ---------------------------------------------------------------------------

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
const clickPrevious = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Previous' }));

async function renderToDdaSetup(taskTypeValue: string) {
  const view = render(<CreateLabelingJob />);
  const wrapper = createWrapper(view.container);

  fireEvent.click(
    wrapper.findRadioGroup()!.findInputByValue('DDA')!.getElement()
  );
  clickNext();

  fireEvent.change(
    await screen.findByPlaceholderText('e.g., Defect Detection - Batch 1'),
    { target: { value: 'sizing-job' } }
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

function selectAutoLabelModel(container: HTMLElement, value: string) {
  const modelSelect = createWrapper(container).findAllSelects()[1];
  modelSelect.openDropdown();
  modelSelect.selectOptionByValue(value);
}

/** The Few_Shot_Option toggle input, or null while it is not rendered. */
function fewShotToggle(container: HTMLElement): HTMLInputElement | null {
  const toggles = createWrapper(container).findAllToggles();
  const toggle = toggles[1];
  return toggle
    ? (toggle.findNativeInput().getElement() as HTMLInputElement)
    : null;
}

/** Add files to the good (index 0) or bad (index 1) example FileUpload. */
function addExampleFiles(
  container: HTMLElement,
  kind: 'good' | 'bad',
  files: File[]
) {
  const uploads = createWrapper(container).findAllFileUploads();
  const input = uploads[kind === 'good' ? 0 : 1]
    .findNativeInput()
    .getElement();
  fireEvent.change(input, { target: { files } });
}

const promptField = () => screen.queryByLabelText('Detection prompt');

/** The native input of the preview's Token_Budget_Selection control. */
function budgetInput(): HTMLInputElement {
  return screen
    .getByTestId('preview-token-budget-input')
    .querySelector('input') as HTMLInputElement;
}

/** The preview's Downscale_Setting select. */
function downscaleSelect() {
  return createWrapper(
    screen.getByTestId('preview-downscale-select') as HTMLElement
  ).findSelect()!;
}

/** The native input of the checkbox belonging to one listed sample. */
function sampleCheckbox(key: string): HTMLInputElement {
  const item = document.querySelector(
    `[data-sample-key="${key}"]`
  ) as HTMLElement | null;
  if (!item) throw new Error(`No listed sample for key ${key}`);
  return createWrapper(item)
    .findCheckbox()!
    .findNativeInput()
    .getElement() as HTMLInputElement;
}

// ---------------------------------------------------------------------------
// Token_Budget_Selection pre-fill (Req 3.1, 3.2)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — token budget pre-fill', () => {
  it('pre-fills from the catalog token_limit, falls back to 10000, and replaces an entered value on model change', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    // The catalog carries no token_limit for this model: the 10000
    // fallback seeds the control (Req 3.1).
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(budgetInput().value).toBe(String(MODEL_TOKEN_LIMIT_DEFAULT));

    // The newly selected model's catalog token_limit replaces the shown
    // value (Req 3.2).
    selectAutoLabelModel(container, `llm:${BUDGET_20K.id}`);
    expect(budgetInput().value).toBe('20000');

    // An entered value is replaced outright on the next model change —
    // never merged with or restored for the previous model (Req 3.2).
    fireEvent.change(budgetInput(), { target: { value: '555' } });
    expect(budgetInput().value).toBe('555');
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(budgetInput().value).toBe(String(MODEL_TOKEN_LIMIT_DEFAULT));
  });

  it('replaces only the budget on a model change: prompt, labels, samples, few-shot and downscale stay', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);
    addExampleFiles(container, 'good', [pngFile('good-1.png')]);

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    fireEvent.change(promptField()!, { target: { value: 'find scratches' } });
    fireEvent.click(fewShotToggle(container)!);
    expect(fewShotToggle(container)!.checked).toBe(true);

    // Select a Sample_Image in the embedded preview.
    await waitFor(() =>
      expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
    );
    fireEvent.click(sampleCheckbox(SAMPLE_KEYS[0]));
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );

    // Choose a Downscale_Setting and confirm the pre-filled budget.
    const select = downscaleSelect();
    select.openDropdown();
    select.selectOptionByValue('1024');
    expect(select.findTrigger().getElement().textContent).toContain(
      '1024 pixels'
    );
    expect(budgetInput().value).toBe(String(MODEL_TOKEN_LIMIT_DEFAULT));

    // The model change replaces the budget…
    selectAutoLabelModel(container, `llm:${BUDGET_20K.id}`);
    expect(budgetInput().value).toBe('20000');

    // …and touches nothing else (Req 3.2).
    expect((promptField() as HTMLTextAreaElement).value).toBe(
      'find scratches'
    );
    expect(
      (screen.getByPlaceholderText('Label 1') as HTMLInputElement).value
    ).toBe('scratch');
    expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
      '1 of 5 sample images selected'
    );
    expect(sampleCheckbox(SAMPLE_KEYS[0])).toBeChecked();
    expect(fewShotToggle(container)!.checked).toBe(true);
    expect(
      downscaleSelect().findTrigger().getElement().textContent
    ).toContain('1024 pixels');
  });
});

// ---------------------------------------------------------------------------
// Submission shape (Req 3.10, 5.2)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — sizing submission shape', () => {
  it('submits downscale_max_edge and token_budget inside auto_label for an llm: model', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    selectAutoLabelModel(container, `llm:${BUDGET_20K.id}`);
    fireEvent.change(promptField()!, { target: { value: 'find scratches' } });
    const select = downscaleSelect();
    select.openDropdown();
    select.selectOptionByValue('1024');

    clickNext();
    fireEvent.click(await screen.findByRole('button', { name: 'Create Job' }));

    await waitFor(() =>
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
    );
    const payload = apiMocks.createLabelingJob.mock.calls[0][0];
    // The pre-filled catalog budget travels as a number and the chosen
    // Downscale_Setting as its Max_Image_Edge integer.
    expect(payload.auto_label).toEqual({
      enabled: true,
      model: `llm:${BUDGET_20K.id}`,
      detection_prompt: 'find scratches',
      downscale_max_edge: 1024,
      token_budget: 20000,
    });
  });

  it('omits token_budget when the entry is cleared, and a blank downscale select submits null', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    fireEvent.change(promptField()!, { target: { value: 'find scratches' } });
    // Clear the pre-filled budget: an empty entry omits the key so the
    // Effective_Token_Budget resolves from the Model_Token_Limits and the
    // default (Req 3.10).
    fireEvent.change(budgetInput(), { target: { value: '' } });

    clickNext();
    fireEvent.click(await screen.findByRole('button', { name: 'Create Job' }));

    await waitFor(() =>
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
    );
    const payload = apiMocks.createLabelingJob.mock.calls[0][0];
    expect(payload.auto_label).toEqual({
      enabled: true,
      model: `llm:${NOVA.id}`,
      detection_prompt: 'find scratches',
      downscale_max_edge: null,
    });
    expect(payload.auto_label).not.toHaveProperty('token_budget');
  });

  it('submits neither sizing value for sam and for bedrock: models', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    // Put non-default sizing state in place under an llm: model first, so
    // this test also proves nothing leaks across the family change
    // (Req 5.2).
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    const select = downscaleSelect();
    select.openDropdown();
    select.selectOptionByValue('1024');
    expect(budgetInput().value).toBe(String(MODEL_TOKEN_LIMIT_DEFAULT));

    // sam: the sizing controls disappear and the submission carries
    // neither value.
    selectAutoLabelModel(container, 'sam');
    expect(
      screen.queryByTestId('preview-downscale-select')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('preview-token-budget-input')
    ).not.toBeInTheDocument();

    clickNext();
    fireEvent.click(await screen.findByRole('button', { name: 'Create Job' }));
    await waitFor(() =>
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
    );
    expect(apiMocks.createLabelingJob.mock.calls[0][0].auto_label).toEqual({
      enabled: true,
      model: 'sam',
    });

    // bedrock:: same absence.
    clickPrevious();
    await screen.findByText('Model-assisted pre-labeling');
    selectAutoLabelModel(container, `bedrock:${NOVA.id}`);
    clickNext();
    fireEvent.click(await screen.findByRole('button', { name: 'Create Job' }));
    await waitFor(() =>
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(2)
    );
    expect(apiMocks.createLabelingJob.mock.calls[1][0].auto_label).toEqual({
      enabled: true,
      model: `bedrock:${NOVA.id}`,
    });
  });
});
