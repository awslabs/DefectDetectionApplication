/**
 * Vitest tests for the job creation wizard's Few_Shot_Option controls
 * (llm-autolabel-prompt-tuning task 11.3, Requirements 1.2, 6.1, 6.9,
 * 7.5, 10.5, plus the shared-upload behavior of 6.4/6.6).
 *
 * Covers:
 * - the Few_Shot_Option toggle: disabled by default, rendered only while a
 *   prompt-guided (`llm:`) model is selected, hidden for `sam`, `bedrock:`
 *   and a cleared selection, and cleared when the model family changes away
 *   from `llm:` so the submission carries `few_shot.enabled === false`
 *   (Req 1.2, 6.1, 6.9, 10.5);
 * - the attach/omit hint at every Model_Image_Limit boundary
 *   (`total < limit-1`, `total == limit-1`, `total > limit-1`, `limit == 1`),
 *   recomputed on a model change and on an example-list change (Req 7.5);
 * - `ensureExampleImagesUploaded`: the example images upload once per file
 *   set and the cached S3 URIs are reused at submission, with a changed file
 *   set forcing a fresh upload — asserted by upload call counts (Req 6.4, 6.6).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob from './CreateLabelingJob';

const { apiMocks, navigateMock, fetchMock } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    createLabelingJob: vi.fn(),
    listWorkteams: vi.fn(),
    getBatchUploadUrls: vi.fn(),
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

/** No `image_limit` in the catalog payload: the wizard falls back to 20. */
const NOVA = { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' };
/** Model_Image_Limit values that put three examples on each boundary. */
const LIMIT_4 = { id: 'model-limit-4', label: 'Limit Four', image_limit: 4 };
const LIMIT_2 = { id: 'model-limit-2', label: 'Limit Two', image_limit: 2 };
const LIMIT_1 = { id: 'model-limit-1', label: 'Limit One', image_limit: 1 };

const pngFile = (name: string) =>
  new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

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
    models: [NOVA, LIMIT_4, LIMIT_2, LIMIT_1],
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
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
// Wizard navigation helpers (mirroring CreateLabelingJob.test.tsx)
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
    { target: { value: 'few-shot-job' } }
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
  // [0] is the auto-labeling assist toggle; the few-shot toggle follows it
  // only while it is rendered.
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

/** The attach/omit hint text, whitespace-normalized, or null when absent. */
function attachHint(container: HTMLElement): string | null {
  const matches = Array.from(container.querySelectorAll('div, span, p')).filter(
    (el) => /will be attached/.test(el.textContent || '')
  );
  if (matches.length === 0) return null;
  // The innermost match is the hint itself.
  const innermost = matches.reduce((a, b) =>
    (a.textContent || '').length <= (b.textContent || '').length ? a : b
  );
  return (innermost.textContent || '').replace(/\s+/g, ' ').trim();
}

const promptField = () => screen.queryByLabelText('Detection prompt');

// ---------------------------------------------------------------------------
// Toggle default and visibility (Req 1.2, 6.1, 6.9, 10.5)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — Few_Shot_Option visibility and default', () => {
  it('is hidden for no model, sam and bedrock:, and appears disabled by default for llm:', async () => {
    // ObjectDetection is the one modality offering all three families.
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);

    // No model selected yet (Req 10.5).
    expect(fewShotToggle(container)).toBeNull();

    selectAutoLabelModel(container, 'sam');
    expect(fewShotToggle(container)).toBeNull();

    selectAutoLabelModel(container, `bedrock:${NOVA.id}`);
    expect(fewShotToggle(container)).toBeNull();

    // Prompt-guided family: offered, and disabled by default (Req 6.1).
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    const toggle = fewShotToggle(container);
    expect(toggle).not.toBeNull();
    expect(toggle!.checked).toBe(false);
    expect(screen.getByText('Few-shot examples')).toBeInTheDocument();
  });

  it('clears an enabled option when the model family changes away from llm:', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);
    addExampleFiles(container, 'good', [pngFile('good-1.png')]);

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    fireEvent.click(fewShotToggle(container)!);
    expect(fewShotToggle(container)!.checked).toBe(true);

    // Away from the `llm:` family: the control disappears and the option is
    // cleared (Req 6.9, 10.5).
    selectAutoLabelModel(container, 'sam');
    expect(fewShotToggle(container)).toBeNull();

    // Back to a prompt-guided model: the option is off again, not restored.
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    expect(fewShotToggle(container)!.checked).toBe(false);
  });

  it('submits few_shot disabled after the selection moves to a non-llm: model', async () => {
    const { container } = await renderToDdaSetup('ObjectDetection');
    await fillSetupAndEnableAutoLabel(container);
    addExampleFiles(container, 'good', [pngFile('good-1.png')]);

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    fireEvent.click(fewShotToggle(container)!);
    fireEvent.change(promptField()!, { target: { value: 'find scratches' } });

    // Switch to a Bedrock vision model, then submit (Req 6.9).
    selectAutoLabelModel(container, `bedrock:${NOVA.id}`);
    clickNext();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Create Job' })
    );

    await waitFor(() => {
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1);
    });
    expect(apiMocks.createLabelingJob).toHaveBeenCalledWith(
      expect.objectContaining({
        few_shot: { enabled: false, examples: [] },
        auto_label: { enabled: true, model: `bedrock:${NOVA.id}` },
      })
    );
  });
});

// ---------------------------------------------------------------------------
// Attach/omit hint at the Model_Image_Limit boundaries (Req 7.5)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — attach/omit hint', () => {
  it('reports the attached and omitted counts at every limit boundary and after a model change', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    // Three stored examples: two good, then one bad.
    addExampleFiles(container, 'good', [
      pngFile('good-1.png'),
      pngFile('good-2.png'),
    ]);
    addExampleFiles(container, 'bad', [pngFile('bad-1.png')]);

    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    // No hint while the option is off.
    expect(attachHint(container)).toBeNull();
    fireEvent.click(fewShotToggle(container)!);

    // total (3) < limit-1 (19): everything attaches.
    expect(attachHint(container)).toBe(
      '3 of 3 examples will be attached, 0 omitted (this model accepts 20 images per request, one reserved for the dataset image).'
    );

    // total (3) == limit-1 (3): still everything, nothing omitted.
    selectAutoLabelModel(container, `llm:${LIMIT_4.id}`);
    expect(attachHint(container)).toBe(
      '3 of 3 examples will be attached, 0 omitted (this model accepts 4 images per request, one reserved for the dataset image).'
    );

    // total (3) > limit-1 (1): the prefix attaches, the rest is omitted.
    selectAutoLabelModel(container, `llm:${LIMIT_2.id}`);
    expect(attachHint(container)).toBe(
      '1 of 3 examples will be attached, 2 omitted (this model accepts 2 images per request, one reserved for the dataset image).'
    );

    // limit == 1: the target image consumes the only slot.
    selectAutoLabelModel(container, `llm:${LIMIT_1.id}`);
    expect(attachHint(container)).toBe(
      '0 of 3 examples will be attached, 3 omitted (this model accepts 1 image per request, one reserved for the dataset image).'
    );

    // Recomputed on an example-list change as well as a model change.
    addExampleFiles(container, 'bad', [pngFile('bad-2.png')]);
    expect(attachHint(container)).toBe(
      '0 of 4 examples will be attached, 4 omitted (this model accepts 1 image per request, one reserved for the dataset image).'
    );
    selectAutoLabelModel(container, `llm:${LIMIT_2.id}`);
    expect(attachHint(container)).toBe(
      '1 of 4 examples will be attached, 3 omitted (this model accepts 2 images per request, one reserved for the dataset image).'
    );
  });

  it('shows the missing-example validation instead of a hint when no examples are uploaded', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    fireEvent.click(fewShotToggle(container)!);

    expect(attachHint(container)).toBeNull();
    expect(
      screen.getByText(
        'At least one example image is required for the few-shot examples option'
      )
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// ensureExampleImagesUploaded — one upload per file set (Req 6.4, 6.6)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob — example image upload caching', () => {
  it('uploads once per file set, reuses the cached URIs, and re-uploads a changed set', async () => {
    const { container } = await renderToDdaSetup('Segmentation');
    await fillSetupAndEnableAutoLabel(container);
    addExampleFiles(container, 'good', [
      pngFile('good-1.png'),
      pngFile('good-2.png'),
    ]);
    selectAutoLabelModel(container, `llm:${NOVA.id}`);
    fireEvent.click(fewShotToggle(container)!);
    fireEvent.change(promptField()!, { target: { value: 'find scratches' } });

    clickNext();
    const createButton = await screen.findByRole('button', {
      name: 'Create Job',
    });

    // First attempt: the job creation call fails, so the wizard stays put
    // with the uploaded examples already cached.
    apiMocks.createLabelingJob.mockRejectedValueOnce(new Error('boom'));
    fireEvent.click(createButton);
    await waitFor(() => {
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1);
    });
    // One batch-URL request for the single non-empty example kind, one PUT
    // per file.
    expect(apiMocks.getBatchUploadUrls).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const firstPayload = apiMocks.createLabelingJob.mock.calls[0][0];
    expect(firstPayload.example_images.good).toHaveLength(2);
    expect(firstPayload.example_images.bad).toEqual([]);
    expect(firstPayload.few_shot).toEqual({
      enabled: true,
      examples: [
        {
          ref: firstPayload.example_images.good[0],
          designation: 'good',
          position: 0,
        },
        {
          ref: firstPayload.example_images.good[1],
          designation: 'good',
          position: 1,
        },
      ],
    });

    // Second attempt with an unchanged file set: no new upload, the same
    // refs are submitted.
    fireEvent.click(screen.getByRole('button', { name: 'Create Job' }));
    await waitFor(() => {
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(2);
    });
    expect(apiMocks.getBatchUploadUrls).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const secondPayload = apiMocks.createLabelingJob.mock.calls[1][0];
    expect(secondPayload.example_images).toEqual(firstPayload.example_images);

    // Changing the file set invalidates the cache: the next submission
    // uploads the new set.
    clickPrevious();
    await screen.findByText('Model-assisted pre-labeling');
    addExampleFiles(container, 'good', [pngFile('good-3.png')]);
    clickNext();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Create Job' })
    );
    await waitFor(() => {
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(3);
    });
    expect(apiMocks.getBatchUploadUrls).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(5);
    const thirdPayload = apiMocks.createLabelingJob.mock.calls[2][0];
    expect(thirdPayload.example_images.good).toHaveLength(3);
    expect(thirdPayload.few_shot.examples.map((e: { position: number }) => e.position)).toEqual([
      0, 1, 2,
    ]);
  });
});
