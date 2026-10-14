/**
 * Property-based tests for the Prompt_Tuning_Preview surface of the
 * labeling job creation flow (llm-autolabel-prompt-tuning tasks
 * 12.4–12.10, design Properties 14–18, 20 and 21).
 *
 * Two rendering scopes, because the properties live at two levels:
 *
 * - Properties 14, 20 and 21 are about the **wizard** — control
 *   visibility, the attach/omit hint and what job submission carries — so
 *   they mount `CreateLabelingJob` and drive it through the real steps.
 * - Properties 15, 16, 17 and 18 are about the **preview component**, so
 *   they render `PromptTuningPreview` directly with generated props and a
 *   generated Preview_API transcript.
 *
 * Mocking follows the example-based suites (`PromptTuningPreview.test.tsx`
 * and `CreateLabelingJob.fewshot.test.tsx`): a `vi.hoisted` proxy over the
 * API service, an `ApiError` carrying `status`, a stubbed global `fetch`
 * for result payloads and example-image PUTs, and fake timers wherever
 * short-polling is exercised.
 *
 * Every property runs at `numRuns: 100`. Each fast-check run unmounts the
 * previous tree and re-primes the mocks, so runs are independent.
 *
 * The llm-model-token-and-image-sizing feature extends this file (its task
 * 11.4) with the frontend halves of its criteria 3.1–3.4, 3.11, 5.1–5.4,
 * 5.6, 7.7 and 9.8: sizing-control visibility per model family, client-side
 * budget rejection (the client half of that feature's Property 11), the
 * per-sample Source → Sent sizing display, and the failed-result display of
 * the run's applied Downscale_Setting and Effective_Token_Budget. Those
 * describes extend the properties above and weaken none of them.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import * as fc from 'fast-check';
import type { ComponentProps } from 'react';

import PromptTuningPreview, {
  MAX_DETECTION_PROMPT_LENGTH,
  POLL_INTERVAL_MS,
  SAMPLE_LIMIT,
  validatePreviewRunInputs,
} from './PromptTuningPreview';
import type { LabelingModality } from './AnnotationCanvas';
import { ApiError } from '../../services/api';
import CreateLabelingJob, {
  BEDROCK_MODALITIES,
  fewShotAttachmentCounts,
  fewShotExamplesFromRefs,
  LLM_MODALITIES,
  MODEL_IMAGE_LIMIT_DEFAULT,
  SAM_MODALITIES,
} from '../../pages/CreateLabelingJob';

const { apiMocks, navigateMock, fetchMock } = vi.hoisted(() => ({
  apiMocks: {
    // Preview surface
    getImagePreview: vi.fn(),
    startPreviewRun: vi.fn(),
    getPreviewRun: vi.fn(),
    // Wizard surface
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

vi.mock('../../services/api', () => {
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

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  }),
}));

// A non-admin Job_Creator: the skip-verification section stays hidden, so
// the toggles on the setup step are exactly [auto-label assist, few-shot].
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'user', role: 'DataScientist' },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useLocation: () => ({ state: undefined }),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

vi.mock('../../components/S3Browser', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

const PREFIX = 'images/';
const DATASET_URI = `s3://bucket/${PREFIX}`;
/** Ten listable dataset objects: two disjoint pools of five for Property 17. */
const LISTED_KEYS = Array.from({ length: 10 }, (_, i) => `${PREFIX}img-${i}.jpg`);

const listedImage = (key: string) => ({
  key,
  filename: key.slice(key.lastIndexOf('/') + 1),
  size: 1024,
  last_modified: '2024-05-01T00:00:00Z',
  presigned_url: `https://s3.example/${key}?sig=1`,
});

const listing = (keys: string[], totalFound = keys.length, offset = 0) => ({
  prefix: PREFIX,
  bucket: 'data-bucket',
  total_found: totalFound,
  offset,
  limit: 50,
  has_more: offset + keys.length < totalFound,
  images: keys.map(listedImage),
  expires_in_seconds: 900,
});

type SampleState = 'Pending' | 'Succeeded' | 'Failed';

interface EntryFixture {
  index: number;
  sample_key: string;
  state: SampleState;
  result_url?: string;
  failure_category?: string;
  failure_reason?: string;
}

const runStatus = (
  runId: string,
  status: 'Running' | 'Completed' | 'Failed',
  results: EntryFixture[],
  fewShot = { enabled: false, attached: 0, omitted: 0 }
) => ({
  run_id: runId,
  status,
  sample_count: results.length,
  few_shot: fewShot,
  results,
});

/** The six Preview_Result failure categories (Requirement 9.6). */
const FAILURE_CATEGORIES = [
  'image_access_failure',
  'unsupported_image_content',
  'unreadable_example_image',
  'timeout',
  'model_error',
  'unusable_model_output',
] as const;

/** Category wording re-derived here rather than imported (Requirement 9.7). */
const CATEGORY_TEXT: Record<string, string> = {
  image_access_failure: 'Image access failure',
  unsupported_image_content: 'Unsupported image content',
  unreadable_example_image: 'Unreadable example image',
  timeout: 'Timeout',
  model_error: 'Model error',
  unusable_model_output: 'Unusable model output',
};

const MODALITIES: LabelingModality[] = [
  'Classification',
  'Segmentation',
  'ObjectDetection',
];

const pngFile = (name: string) =>
  new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

/** Reset every mock to a benign default. Called at the top of each fc run. */
function primeMocks(
  options: {
    images?: string[];
    models?: { id: string; label: string; image_limit?: number }[];
  } = {}
) {
  vi.clearAllMocks();
  apiMocks.getImagePreview.mockResolvedValue(listing(options.images ?? []));
  apiMocks.startPreviewRun.mockResolvedValue({
    run_id: 'run-1',
    sample_count: 1,
    status: 'Running',
  });
  apiMocks.getPreviewRun.mockResolvedValue(runStatus('run-1', 'Completed', []));
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC1', s3_bucket: 'out-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingTeams.mockResolvedValue({
    teams: [{ team_id: 't-1', team_name: 'Team One', members: ['a'] }],
    count: 1,
  });
  apiMocks.getBedrockModels.mockResolvedValue({
    models: options.models ?? [],
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
  apiMocks.getBatchUploadUrls.mockImplementation(
    async (
      _usecaseId: string,
      data: {
        prefix?: string;
        files: { filename: string; content_type?: string }[];
      }
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
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
}

beforeEach(() => {
  window.localStorage.clear();
  primeMocks();
  vi.stubGlobal('fetch', fetchMock);
  // Cloudscape's file thumbnails go through object URLs; jsdom has none.
  if (!URL.createObjectURL) {
    URL.createObjectURL = () => 'blob:example';
    URL.revokeObjectURL = () => undefined;
  }
  // jsdom has no 2D context, and `PreviewResultCanvas` paints decoded RLE
  // masks into one. A recording stand-in keeps Segmentation renders working.
  HTMLCanvasElement.prototype.getContext = ((kind: string) =>
    kind === '2d'
      ? ({
          clearRect: () => undefined,
          createImageData: (width: number, height: number) => ({
            data: new Uint8ClampedArray(width * height * 4),
            width,
            height,
            colorSpace: 'srgb' as const,
          }),
          putImageData: () => undefined,
        } as unknown as CanvasRenderingContext2D)
      : null) as unknown as HTMLCanvasElement['getContext'];
});

// ---------------------------------------------------------------------------
// Component-level helpers (Properties 15–18)
// ---------------------------------------------------------------------------

type PreviewProps = ComponentProps<typeof PromptTuningPreview>;

const previewProps = (overrides: Partial<PreviewProps> = {}): PreviewProps => ({
  usecaseId: 'uc-1',
  datasetPrefix: PREFIX,
  model: 'llm:us.amazon.nova-pro-v1:0',
  detectionPrompt: 'Find scratches on the surface',
  taskType: 'ObjectDetection',
  labelSet: ['scratch', 'dent'],
  fewShotEnabled: false,
  goodExampleCount: 0,
  badExampleCount: 0,
  ensureExampleImagesUploaded: vi.fn(async () => ({
    good: [] as string[],
    bad: [] as string[],
  })),
  ...overrides,
});

/** The native checkbox input of one listed sample. */
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

const runButton = () => screen.getByTestId('preview-run-button');

/** Keys of the currently displayed result entries, in display order. */
function displayedKeys(): string[] {
  return screen
    .queryAllByTestId('preview-result-entry')
    .map((node) => node.getAttribute('data-sample-key') || '');
}

/** Advance fake timers inside `act`, flushing the promises they unblock. */
const advance = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

// ---------------------------------------------------------------------------
// Wizard-level helpers (Properties 14, 20, 21)
// ---------------------------------------------------------------------------

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));

/** Drive `CreateLabelingJob` to the DDA labeling-setup step. */
async function renderToDdaSetup(modality: string) {
  const view = render(<CreateLabelingJob />);
  const wrapper = createWrapper(view.container);

  fireEvent.click(
    wrapper.findRadioGroup()!.findInputByValue('DDA')!.getElement()
  );
  clickNext();

  fireEvent.change(
    await screen.findByPlaceholderText('e.g., Defect Detection - Batch 1'),
    { target: { value: 'preview-property-job' } }
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
    { target: { value: DATASET_URI } }
  );
  clickNext();

  const taskSelect = wrapper.findSelect()!;
  taskSelect.openDropdown();
  taskSelect.selectOptionByValue(modality);
  clickNext();

  await screen.findByText('Model-assisted pre-labeling');
  return view;
}

/** Turn on the model-assisted pre-labeling toggle (the step's first toggle). */
function enableAutoLabel(container: HTMLElement) {
  fireEvent.click(
    createWrapper(container).findAllToggles()[0].findNativeInput().getElement()
  );
}

/** The model select (the team select is the step's first select). */
const modelSelect = (container: HTMLElement) =>
  createWrapper(container).findAllSelects()[1];

function selectAutoLabelModel(container: HTMLElement, value: string) {
  const select = modelSelect(container);
  select.openDropdown();
  select.selectOptionByValue(value);
}

/** Whether the model dropdown currently offers `value` at all. */
function modelOptionOffered(container: HTMLElement, value: string): boolean {
  const select = modelSelect(container);
  select.openDropdown();
  const offered = !!select.findDropdown().findOptionByValue(value);
  select.closeDropdown();
  return offered;
}

/** The Few_Shot_Option toggle input, or null while it is not rendered. */
function fewShotToggle(container: HTMLElement): HTMLInputElement | null {
  const toggle = createWrapper(container).findAllToggles()[1];
  return toggle
    ? (toggle.findNativeInput().getElement() as HTMLInputElement)
    : null;
}

/** Set the Few_Shot_Option to `target`, clicking only when it must change. */
function setFewShot(container: HTMLElement, target: boolean) {
  const toggle = fewShotToggle(container);
  if (!toggle) throw new Error('The few-shot toggle is not rendered');
  if (toggle.checked !== target) fireEvent.click(toggle);
}

/** Append files to the good (0) or bad (1) example FileUpload. */
function addExampleFiles(
  container: HTMLElement,
  kind: 'good' | 'bad',
  files: File[]
) {
  if (files.length === 0) return;
  const uploads = createWrapper(container).findAllFileUploads();
  const input = uploads[kind === 'good' ? 0 : 1].findNativeInput().getElement();
  fireEvent.change(input, { target: { files } });
}

/** Pick the labeling team and one label so the setup step validates. */
async function fillTeamAndLabel(container: HTMLElement, modality: string) {
  const wrapper = createWrapper(container);
  const teamSelect = wrapper.findAllSelects()[0];
  await waitFor(() => {
    expect(teamSelect.findTrigger().getElement()).not.toBeDisabled();
  });
  teamSelect.openDropdown();
  teamSelect.selectOptionByValue('t-1');
  if (modality !== 'Classification') {
    fireEvent.change(screen.getByPlaceholderText('Label 1'), {
      target: { value: 'scratch' },
    });
  }
}

const promptField = () => screen.getByLabelText('Detection prompt');

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

// ---------------------------------------------------------------------------
// Property 14 (task 12.4)
// ---------------------------------------------------------------------------

/**
 * Model/modality compatibility re-derived from the acceptance criteria
 * rather than taken from the wizard's own predicate: SAM is geometry only,
 * Bedrock vision models answer classification/detection prompts, and the
 * prompt-guided LLM family covers every modality.
 */
const ORACLE_MODALITIES: Record<'sam' | 'bedrock' | 'llm', string[]> = {
  sam: ['Segmentation', 'ObjectDetection'],
  bedrock: ['Classification', 'ObjectDetection'],
  llm: ['Classification', 'Segmentation', 'ObjectDetection'],
};

const PREVIEW_MODEL = { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' };

describe('Feature: llm-autolabel-prompt-tuning, Property 14: Prompt Tuning controls appear exactly for the `llm:` family', () => {
  /**
   * *For any* auto-label model selection state in the job creation flow, the
   * Prompt_Tuning_Preview controls and the Few_Shot_Option control are
   * present if and only if the selected model identifier is in the `llm:`
   * family, the Few_Shot_Option defaults to disabled whenever it is
   * presented, and any transition of the selection away from the `llm:`
   * family clears the option.
   *
   * **Validates: Requirements 1.1, 1.2, 6.1, 6.9, 10.5**
   */
  it('shows the preview and few-shot controls exactly for a compatible llm: selection', async () => {
    // The oracle and the shipped matrix must agree before it is used.
    expect(SAM_MODALITIES).toEqual(ORACLE_MODALITIES.sam);
    expect(BEDROCK_MODALITIES).toEqual(ORACLE_MODALITIES.bedrock);
    expect(LLM_MODALITIES).toEqual(ORACLE_MODALITIES.llm);

    await fc.assert(
      fc.asyncProperty(
        fc.constantFrom(...MODALITIES),
        fc.constantFrom<'none' | 'sam' | 'bedrock' | 'llm'>(
          'none',
          'sam',
          'bedrock',
          'llm'
        ),
        async (modality, family) => {
          cleanup();
          primeMocks({ models: [PREVIEW_MODEL] });

          const { container } = await renderToDdaSetup(modality);
          enableAutoLabel(container);

          // No auto-label model selected yet (Requirement 10.5).
          expect(
            screen.queryByTestId('prompt-tuning-preview')
          ).not.toBeInTheDocument();
          expect(fewShotToggle(container)).toBeNull();

          const value =
            family === 'none'
              ? null
              : family === 'sam'
                ? 'sam'
                : `${family}:${PREVIEW_MODEL.id}`;
          const compatible =
            value !== null &&
            ORACLE_MODALITIES[family as 'sam' | 'bedrock' | 'llm'].includes(
              modality
            );

          if (value !== null) {
            // An incompatible family is not even offered for this modality.
            expect(modelOptionOffered(container, value)).toBe(compatible);
            if (compatible) selectAutoLabelModel(container, value);
          }

          const expectVisible = compatible && family === 'llm';
          expect(!!screen.queryByTestId('prompt-tuning-preview')).toBe(
            expectVisible
          );
          expect(!!fewShotToggle(container)).toBe(expectVisible);

          if (!expectVisible) return;

          // Presented => disabled by default (Requirement 6.1).
          expect(fewShotToggle(container)!.checked).toBe(false);
          setFewShot(container, true);
          expect(fewShotToggle(container)!.checked).toBe(true);

          // Moving away from the `llm:` family removes both controls and
          // clears the option; coming back does not restore it
          // (Requirements 6.9, 10.5).
          const awayFamily = ORACLE_MODALITIES.sam.includes(modality)
            ? 'sam'
            : 'bedrock';
          const awayValue =
            awayFamily === 'sam' ? 'sam' : `bedrock:${PREVIEW_MODEL.id}`;
          selectAutoLabelModel(container, awayValue);
          expect(
            screen.queryByTestId('prompt-tuning-preview')
          ).not.toBeInTheDocument();
          expect(fewShotToggle(container)).toBeNull();

          selectAutoLabelModel(container, `llm:${PREVIEW_MODEL.id}`);
          expect(screen.queryByTestId('prompt-tuning-preview')).toBeInTheDocument();
          expect(fewShotToggle(container)!.checked).toBe(false);
        }
      ),
      { numRuns: 100 }
    );
  }, 900_000);
});

// ---------------------------------------------------------------------------
// Property 15 (task 12.5)
// ---------------------------------------------------------------------------

/** The pre-flight rules, as independent kinds with a distinctive wording. */
type RuleKind =
  | 'model'
  | 'prompt-empty'
  | 'prompt-long'
  | 'modality'
  | 'labels'
  | 'samples'
  | 'fewshot-none'
  | 'fewshot-many';

const RULE_MATCHERS: Record<RuleKind, RegExp> = {
  model: /is not a prompt-guided LLM model/,
  'prompt-empty': /^A detection prompt is required$/,
  'prompt-long': /^The detection prompt exceeds /,
  modality: /is not supported$/,
  labels:
    /(fixed label set|at least one non-empty label|at most 10 labels|at most 64 characters|must be distinct)/,
  samples: /^Select between 1 and 5 sample images/,
  'fewshot-none': /^At least one example image is required/,
  'fewshot-many': /^At most 10 good and 10 bad example images/,
};

interface PreflightInput {
  model: string;
  detectionPrompt: string;
  taskType: string;
  labelSet: string[];
  selectedCount: number;
  fewShotEnabled: boolean;
  goodExampleCount: number;
  badExampleCount: number;
}

/**
 * The violated rules, re-derived from the acceptance criteria (Requirements
 * 1.4, 2.4, 6.2 and the API's rules in 8.4) rather than read off the
 * component's own validator.
 */
function violatedRules(input: PreflightInput): RuleKind[] {
  const kinds: RuleKind[] = [];

  if (!input.model.startsWith('llm:') || input.model === 'llm:') {
    kinds.push('model');
  }

  if (input.detectionPrompt.trim() === '') {
    kinds.push('prompt-empty');
  } else if (input.detectionPrompt.length > MAX_DETECTION_PROMPT_LENGTH) {
    kinds.push('prompt-long');
  }

  if (!(MODALITIES as string[]).includes(input.taskType)) {
    kinds.push('modality');
  } else if (input.taskType === 'Classification') {
    const fixed =
      input.labelSet.length === 2 &&
      input.labelSet[0] === 'normal' &&
      input.labelSet[1] === 'anomaly';
    if (!fixed) kinds.push('labels');
  } else {
    const labels = input.labelSet.map((l) => l.trim());
    const invalid =
      labels.length === 0 ||
      labels.some((l) => !l) ||
      labels.length > 10 ||
      labels.some((l) => l.length > 64) ||
      new Set(labels).size !== labels.length;
    if (invalid) kinds.push('labels');
  }

  if (input.selectedCount < 1 || input.selectedCount > SAMPLE_LIMIT) {
    kinds.push('samples');
  }

  if (input.fewShotEnabled) {
    if (input.goodExampleCount + input.badExampleCount === 0) {
      kinds.push('fewshot-none');
    }
    if (input.goodExampleCount > 10 || input.badExampleCount > 10) {
      kinds.push('fewshot-many');
    }
  }

  return kinds;
}

/** Label_Set shapes covering the valid and every invalid case. */
const labelSetArb = fc.constantFrom<string[]>(
  ['normal', 'anomaly'],
  ['scratch', 'dent'],
  [],
  ['   '],
  ['scratch', 'scratch'],
  ['a'.repeat(65)],
  Array.from({ length: 11 }, (_, i) => `label-${i}`)
);

describe('Feature: llm-autolabel-prompt-tuning, Property 15: Client-side validation names every violation, sends nothing, and keeps state', () => {
  /**
   * *For any* job creation flow state violating a non-empty subset of the
   * preview start rules, the Portal displays a validation message
   * identifying every violated rule, issues no Preview_API request, and
   * leaves every entered value unchanged.
   *
   * **Validates: Requirements 1.4, 2.4, 6.2**
   */
  it('lists every violated rule, issues no request and touches no state', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.record({
          model: fc.constantFrom(
            'llm:us.amazon.nova-pro-v1:0',
            'llm:',
            'sam',
            'bedrock:us.amazon.nova-pro-v1:0',
            ''
          ),
          detectionPrompt: fc.constantFrom(
            'Find scratches on the surface',
            '',
            '  \n\t ',
            'x'.repeat(MAX_DETECTION_PROMPT_LENGTH + 1)
          ),
          taskType: fc.constantFrom(
            'Classification',
            'Segmentation',
            'ObjectDetection',
            'Pose'
          ),
          labelSet: labelSetArb,
          selectedCount: fc.integer({ min: 0, max: SAMPLE_LIMIT }),
          fewShotEnabled: fc.boolean(),
          goodExampleCount: fc.integer({ min: 0, max: 12 }),
          badExampleCount: fc.integer({ min: 0, max: 12 }),
        }),
        async (input) => {
          const expectedKinds = violatedRules(input);
          // The property is about rejection, so only invalid states qualify.
          fc.pre(expectedKinds.length > 0);

          cleanup();
          primeMocks({ images: LISTED_KEYS.slice(0, SAMPLE_LIMIT) });

          render(
            <PromptTuningPreview
              {...previewProps({
                model: input.model,
                detectionPrompt: input.detectionPrompt,
                taskType: input.taskType as LabelingModality,
                labelSet: input.labelSet,
                fewShotEnabled: input.fewShotEnabled,
                goodExampleCount: input.goodExampleCount,
                badExampleCount: input.badExampleCount,
              })}
            />
          );
          await waitFor(() =>
            expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
          );

          const chosen = LISTED_KEYS.slice(0, input.selectedCount);
          for (const key of chosen) fireEvent.click(sampleCheckbox(key));

          const selectionBefore = screen.getByTestId('preview-selection-count')
            .textContent;
          const checkedBefore = LISTED_KEYS.slice(0, SAMPLE_LIMIT).map(
            (key) => sampleCheckbox(key).checked
          );

          fireEvent.click(runButton());

          // Every violated rule is named exactly once.
          const messages = screen
            .getAllByTestId('preview-validation-error')
            .map((node) => node.textContent || '');
          expect(messages).toHaveLength(expectedKinds.length);
          for (const kind of expectedKinds) {
            expect(
              messages.filter((m) => RULE_MATCHERS[kind].test(m))
            ).toHaveLength(1);
          }
          // ...and the component's own validator agrees message for message.
          expect(messages).toEqual(
            validatePreviewRunInputs({
              model: input.model,
              detectionPrompt: input.detectionPrompt,
              taskType: input.taskType,
              labelSet: input.labelSet,
              selectedCount: input.selectedCount,
              fewShotEnabled: input.fewShotEnabled,
              goodExampleCount: input.goodExampleCount,
              badExampleCount: input.badExampleCount,
            })
          );

          // Nothing was sent and nothing was uploaded.
          expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();
          expect(fetchMock).not.toHaveBeenCalled();

          // Every entered value is untouched and no result set appeared.
          expect(
            screen.getByTestId('preview-selection-count').textContent
          ).toBe(selectionBefore);
          expect(
            LISTED_KEYS.slice(0, SAMPLE_LIMIT).map(
              (key) => sampleCheckbox(key).checked
            )
          ).toEqual(checkedBefore);
          expect(screen.queryByTestId('preview-results')).not.toBeInTheDocument();
          expect(screen.queryByTestId('preview-run-error')).not.toBeInTheDocument();
          expect(runButton()).toBeEnabled();
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});

// ---------------------------------------------------------------------------
// Property 16 (task 12.6)
// ---------------------------------------------------------------------------

/** How a Preview_Run can fail after a first run has already produced results. */
type FailureMode =
  | 'start-rejection'
  | 'run-failed'
  | 'status-404'
  | 'status-transport'
  | 'overall-bound';

/** Establish one completed run whose single result is on screen. */
async function establishFirstRun(key: string) {
  apiMocks.startPreviewRun.mockResolvedValue({
    run_id: 'run-1',
    sample_count: 1,
    status: 'Running',
  });
  apiMocks.getPreviewRun.mockResolvedValue(
    runStatus('run-1', 'Completed', [
      {
        index: 0,
        sample_key: key,
        state: 'Succeeded',
        result_url: 'https://payloads.example/0.json',
      },
    ])
  );
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      sample_key: key,
      state: 'Succeeded',
      prelabel: {
        modality: 'ObjectDetection',
        boxes: [{ class: 'scratch', left: 10, top: 10, width: 20, height: 20 }],
      },
      image_width: 100,
      image_height: 100,
    }),
  });

  render(<PromptTuningPreview {...previewProps()} />);
  await advance();
  fireEvent.click(sampleCheckbox(key));
  await act(async () => {
    fireEvent.click(runButton());
  });
}

describe('Feature: llm-autolabel-prompt-tuning, Property 16: Preview run failures leave the flow usable and intact', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    return () => {
      vi.useRealTimers();
    };
  });

  /**
   * *For any* Preview_Run failure — request rejection, transport error,
   * non-2xx response, run status failure, or a run that returns no result
   * within the per-Sample_Image bound — the Portal displays an error
   * indicating the failure, re-enables starting a Preview_Run, and leaves
   * every value entered in the job creation flow unchanged.
   *
   * **Validates: Requirements 1.8, 4.7**
   */
  it('reports the failure, re-enables the run control and keeps the flow intact', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.constantFrom<FailureMode>(
          'start-rejection',
          'run-failed',
          'status-404',
          'status-transport',
          'overall-bound'
        ),
        async (mode) => {
          cleanup();
          primeMocks({ images: LISTED_KEYS.slice(0, 1) });
          const key = LISTED_KEYS[0];

          await establishFirstRun(key);
          expect(displayedKeys()).toEqual([key]);
          expect(screen.getByTestId('preview-box')).toBeInTheDocument();
          expect(screen.queryByTestId('preview-run-error')).not.toBeInTheDocument();

          // Second run: the generated failure mode.
          apiMocks.startPreviewRun.mockReset();
          apiMocks.getPreviewRun.mockReset();
          if (mode === 'start-rejection') {
            apiMocks.startPreviewRun.mockRejectedValue(
              new Error('preview run rejected')
            );
          } else {
            apiMocks.startPreviewRun.mockResolvedValue({
              run_id: 'run-2',
              sample_count: 1,
              status: 'Running',
            });
          }
          const pending: EntryFixture[] = [
            { index: 0, sample_key: key, state: 'Pending' },
          ];
          if (mode === 'run-failed') {
            apiMocks.getPreviewRun.mockResolvedValue(
              runStatus('run-2', 'Failed', pending)
            );
          } else if (mode === 'status-404') {
            apiMocks.getPreviewRun.mockRejectedValue(
              new ApiError('Preview run not found', 404)
            );
          } else if (mode === 'status-transport') {
            apiMocks.getPreviewRun.mockRejectedValue(new Error('network down'));
          } else if (mode === 'overall-bound') {
            apiMocks.getPreviewRun.mockResolvedValue(
              runStatus('run-2', 'Running', pending)
            );
          }

          await act(async () => {
            fireEvent.click(runButton());
          });
          if (mode === 'overall-bound') {
            // 1 sample => 1 x 120 s + 60 s; advance past the bound.
            await advance(1 * 120_000 + 60_000 + POLL_INTERVAL_MS);
          }

          // The failure is surfaced and the control is usable again.
          expect(screen.getByTestId('preview-run-error')).toBeInTheDocument();
          expect(runButton()).toBeEnabled();

          // Nothing entered in the flow was lost: the selection stands and
          // the previous run's result is still displayed.
          expect(sampleCheckbox(key).checked).toBe(true);
          expect(screen.getByTestId('preview-selection-count')).toHaveTextContent(
            '1 of 5 sample images selected'
          );
          expect(displayedKeys()).toEqual([key]);
          expect(screen.getByTestId('preview-box')).toBeInTheDocument();
          expect(
            screen.queryByTestId('preview-result-pending')
          ).not.toBeInTheDocument();
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});

// ---------------------------------------------------------------------------
// Property 17 (task 12.7)
// ---------------------------------------------------------------------------

const failedEntry = (key: string, index: number): EntryFixture => ({
  index,
  sample_key: key,
  state: 'Failed',
  failure_category: 'model_error',
  failure_reason: `model error for ${key}`,
});

const pendingEntry = (key: string, index: number): EntryFixture => ({
  index,
  sample_key: key,
  state: 'Pending',
});

describe('Feature: llm-autolabel-prompt-tuning, Property 17: Results are displayed per sample, replaced wholly, and preserved on failure', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    return () => {
      vi.useRealTimers();
    };
  });

  /**
   * *For any* pair of consecutive Preview_Runs, when the second run returns
   * results the displayed set is exactly the second run's Preview_Results
   * (one entry per requested Sample_Image, keyed by its own Sample_Image,
   * with no entry from the first run remaining); and when the second run
   * fails before returning any Preview_Result the displayed set remains
   * exactly the first run's Preview_Results unchanged.
   *
   * **Validates: Requirements 4.6, 5.3, 5.4**
   */
  it('replaces the previous set wholesale on results and preserves it on failure', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.subarray(LISTED_KEYS.slice(0, 5), { minLength: 1, maxLength: 5 }),
        fc.subarray(LISTED_KEYS.slice(5, 10), { minLength: 1, maxLength: 5 }),
        fc.constantFrom<'results' | 'fail-start' | 'fail-status'>(
          'results',
          'fail-start',
          'fail-status'
        ),
        async (firstKeys, secondKeys, mode) => {
          cleanup();
          primeMocks({ images: LISTED_KEYS });

          apiMocks.startPreviewRun.mockResolvedValue({
            run_id: 'run-1',
            sample_count: firstKeys.length,
            status: 'Running',
          });
          apiMocks.getPreviewRun.mockResolvedValue(
            runStatus('run-1', 'Completed', firstKeys.map(failedEntry))
          );

          render(<PromptTuningPreview {...previewProps()} />);
          await advance();

          for (const key of firstKeys) fireEvent.click(sampleCheckbox(key));
          await act(async () => {
            fireEvent.click(runButton());
          });

          // One entry per requested Sample_Image, keyed by sample key.
          expect(displayedKeys()).toEqual(firstKeys);

          // Re-select for the second run.
          for (const key of firstKeys) fireEvent.click(sampleCheckbox(key));
          for (const key of secondKeys) fireEvent.click(sampleCheckbox(key));

          apiMocks.startPreviewRun.mockReset();
          apiMocks.getPreviewRun.mockReset();
          if (mode === 'fail-start') {
            apiMocks.startPreviewRun.mockRejectedValue(
              new Error('preview run rejected')
            );
          } else {
            apiMocks.startPreviewRun.mockResolvedValue({
              run_id: 'run-2',
              sample_count: secondKeys.length,
              status: 'Running',
            });
          }
          if (mode === 'fail-status') {
            apiMocks.getPreviewRun.mockRejectedValue(
              new ApiError('Preview run not found', 404)
            );
          } else if (mode === 'results') {
            apiMocks.getPreviewRun
              .mockResolvedValueOnce(
                runStatus('run-2', 'Running', secondKeys.map(pendingEntry))
              )
              .mockResolvedValue(
                runStatus('run-2', 'Completed', secondKeys.map(failedEntry))
              );
          }

          await act(async () => {
            fireEvent.click(runButton());
          });

          if (mode === 'results') {
            // Nothing resolved yet: the first run's set is still displayed.
            expect(displayedKeys()).toEqual(firstKeys);
            await advance(POLL_INTERVAL_MS);
            // The first non-Pending entry replaces the set wholesale.
            expect(displayedKeys()).toEqual(secondKeys);
            for (const key of firstKeys) {
              expect(
                document.querySelector(
                  `[data-testid="preview-result-entry"][data-sample-key="${key}"]`
                )
              ).toBeNull();
            }
            expect(
              screen.getAllByTestId('preview-result-failure')
            ).toHaveLength(secondKeys.length);
          } else {
            // The run failed before any Preview_Result: the previous set
            // stands unchanged and the failure is reported.
            expect(screen.getByTestId('preview-run-error')).toBeInTheDocument();
            expect(displayedKeys()).toEqual(firstKeys);
            expect(
              screen.getAllByTestId('preview-result-failure')
            ).toHaveLength(firstKeys.length);
          }
          expect(runButton()).toBeEnabled();
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});

// ---------------------------------------------------------------------------
// Property 18 (task 12.8)
// ---------------------------------------------------------------------------

type SampleOutcome =
  | { kind: 'populated'; classes: string[] }
  | { kind: 'empty' }
  | { kind: 'failure'; category: string; reason: string; raw: string };

const reasonArb = fc
  .array(fc.constantFrom('alpha', 'beta', 'gamma', 'denied', '404', 'x'), {
    minLength: 1,
    maxLength: 4,
  })
  .map((words) => words.join(' '));

const rawOutputArb = fc
  .array(
    fc.constantFrom('Sure!', '\n', '```json', '{"detections":', '[', '}', '  '),
    { minLength: 1, maxLength: 8 }
  )
  .map((parts) => parts.join(''));

const outcomeArb = (labelSet: string[]) =>
  fc.oneof(
    fc
      .array(fc.constantFrom(...labelSet), { minLength: 1, maxLength: 3 })
      .map((classes): SampleOutcome => ({ kind: 'populated', classes })),
    fc.constant<SampleOutcome>({ kind: 'empty' }),
    fc
      .record({
        category: fc.constantFrom(...FAILURE_CATEGORIES),
        reason: reasonArb,
        raw: rawOutputArb,
      })
      .map(
        (f): SampleOutcome => ({
          kind: 'failure',
          category: f.category,
          reason: f.reason,
          raw: f.raw,
        })
      )
  );

/** The result-payload JSON the Preview_API would write for one outcome. */
function payloadFor(
  modality: LabelingModality,
  key: string,
  outcome: SampleOutcome
): Record<string, unknown> {
  if (outcome.kind === 'failure') {
    return {
      sample_key: key,
      state: 'Failed',
      failure_category: outcome.category,
      failure_reason: outcome.reason,
      raw_model_output: outcome.raw,
    };
  }
  const classes = outcome.kind === 'populated' ? outcome.classes : [];
  let prelabel: Record<string, unknown>;
  if (modality === 'ObjectDetection') {
    prelabel = {
      modality,
      boxes: classes.map((className, i) => ({
        class: className,
        left: 5 + i * 10,
        top: 5 + i * 10,
        width: 20,
        height: 20,
      })),
    };
  } else if (modality === 'Segmentation') {
    prelabel = {
      modality,
      // Column-major counts over the 4x4 image: the lower half of each column.
      regions: classes.map((className) => ({ class: className, rle: '8 8' })),
    };
  } else {
    prelabel = {
      modality,
      label: outcome.kind === 'populated' ? 'anomaly' : 'normal',
    };
  }
  return {
    sample_key: key,
    state: 'Succeeded',
    prelabel,
    image_width: modality === 'Segmentation' ? 4 : 100,
    image_height: modality === 'Segmentation' ? 4 : 100,
  };
}

describe('Feature: llm-autolabel-prompt-tuning, Property 18: Rendering reflects each result’s modality, emptiness and failure', () => {
  /**
   * *For any* Preview_Result set, the Portal renders for each successful
   * result its Pre_Label content in the job's modality — boxes with their
   * Label_Set class names, mask regions with their class names associated,
   * or the classification label beside the image — renders a zero-detection
   * result with an empty-result indication distinct from both a populated
   * and a failed result, and renders each failed result with its failure
   * category and reason beside its Sample_Image, making the complete raw
   * model output viewable for `unusable_model_output`.
   *
   * **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 9.7, 9.8**
   */
  it('renders every result according to its modality, emptiness and failure', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc
          .constantFrom(...MODALITIES)
          .chain((modality) => {
            const labelSet =
              modality === 'Classification'
                ? ['normal', 'anomaly']
                : ['scratch', 'dent'];
            return fc.record({
              modality: fc.constant(modality),
              labelSet: fc.constant(labelSet),
              outcomes: fc.array(outcomeArb(labelSet), {
                minLength: 1,
                maxLength: SAMPLE_LIMIT,
              }),
            });
          }),
        async ({ modality, labelSet, outcomes }) => {
          cleanup();
          primeMocks({ images: LISTED_KEYS.slice(0, outcomes.length) });
          const keys = LISTED_KEYS.slice(0, outcomes.length);

          apiMocks.startPreviewRun.mockResolvedValue({
            run_id: 'run-1',
            sample_count: keys.length,
            status: 'Running',
          });
          apiMocks.getPreviewRun.mockResolvedValue(
            runStatus(
              'run-1',
              'Completed',
              keys.map((key, index) => {
                const outcome = outcomes[index];
                return {
                  index,
                  sample_key: key,
                  state: (outcome.kind === 'failure'
                    ? 'Failed'
                    : 'Succeeded') as SampleState,
                  result_url: `https://payloads.example/${index}.json`,
                  ...(outcome.kind === 'failure'
                    ? {
                        failure_category: outcome.category,
                        failure_reason: outcome.reason,
                      }
                    : {}),
                };
              })
            )
          );
          fetchMock.mockImplementation(async (url: string) => {
            const index = Number(/\/(\d+)\.json$/.exec(url)?.[1] ?? '0');
            return {
              ok: true,
              status: 200,
              json: async () =>
                payloadFor(modality, keys[index], outcomes[index]),
            };
          });

          render(
            <PromptTuningPreview
              {...previewProps({ taskType: modality, labelSet })}
            />
          );
          await waitFor(() =>
            expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
          );
          for (const key of keys) fireEvent.click(sampleCheckbox(key));
          await act(async () => {
            fireEvent.click(runButton());
          });
          await waitFor(() =>
            expect(screen.getByTestId('preview-results')).toBeInTheDocument()
          );

          // Exactly one entry per requested Sample_Image, in request order.
          expect(displayedKeys()).toEqual(keys);
          const entries = screen.getAllByTestId('preview-result-entry');

          outcomes.forEach((outcome, index) => {
            const entry = within(entries[index]);

            if (outcome.kind === 'failure') {
              expect(entry.getByTestId('preview-result-failure')).toBeInTheDocument();
              expect(
                entry.getByTestId('preview-failure-category').textContent
              ).toBe(CATEGORY_TEXT[outcome.category]);
              expect(entry.getByTestId('preview-failure-reason')).toHaveTextContent(
                outcome.reason
              );
              // Failure rendering is distinct from a result rendering.
              expect(entry.queryByTestId('preview-result-image')).toBeNull();
              expect(entry.queryByTestId('preview-empty-result')).toBeNull();
              // The raw model output is offered only for unusable output.
              if (outcome.category === 'unusable_model_output') {
                const disclosure = entry.getByTestId('preview-raw-output');
                expect(disclosure.tagName).toBe('DETAILS');
                expect(
                  entry.getByTestId('preview-raw-output-text').textContent
                ).toBe(outcome.raw);
              } else {
                expect(entry.queryByTestId('preview-raw-output')).toBeNull();
              }
              return;
            }

            // Successful results draw the Sample_Image with their overlays.
            expect(entry.getByTestId('preview-result-image')).toBeInTheDocument();
            expect(entry.queryByTestId('preview-result-failure')).toBeNull();
            expect(entry.queryByTestId('preview-raw-output')).toBeNull();
            const populated = outcome.kind === 'populated';
            const classes = populated ? outcome.classes : [];

            if (modality === 'ObjectDetection') {
              expect(
                entry.queryAllByTestId('preview-box').map(() => true)
              ).toHaveLength(classes.length);
              expect(
                entry.queryAllByTestId('preview-box-class').map((n) => n.textContent)
              ).toEqual(classes);
            } else if (modality === 'Segmentation') {
              expect(entry.getByTestId('preview-mask-overlay')).toBeInTheDocument();
              expect(
                entry.queryAllByTestId('preview-region-class').map((n) => n.textContent)
              ).toEqual(classes);
              expect(!!entry.queryByTestId('preview-region-legend')).toBe(
                classes.length > 0
              );
            } else {
              expect(
                entry.getByTestId('preview-classification-label')
              ).toHaveTextContent(populated ? 'anomaly' : 'normal');
            }

            // The empty-result indication marks exactly the zero-detection
            // results (for Classification, the `normal` outcome).
            expect(!!entry.queryByTestId('preview-empty-result')).toBe(!populated);
          });
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});

// ---------------------------------------------------------------------------
// Property 20 (task 12.9)
// ---------------------------------------------------------------------------

/** Catalog entries spanning Model_Image_Limit boundaries, plus an unlisted one. */
const LIMIT_MODELS = [1, 2, 3, 4, 5, 10, 20, 25].map((n) => ({
  id: `model-limit-${n}`,
  label: `Limit ${n}`,
  image_limit: n,
}));
const DEFAULT_LIMIT_MODEL = { id: 'model-default', label: 'Catalog default' };
const LIMIT_CATALOG = [...LIMIT_MODELS, DEFAULT_LIMIT_MODEL];

const resolvedLimit = (model: {
  id: string;
  label: string;
  image_limit?: number;
}) => model.image_limit ?? MODEL_IMAGE_LIMIT_DEFAULT;

/**
 * The attached/omitted split re-derived from Requirements 7.2/7.4: one image
 * slot is always the target image, so a limit of 1 attaches nothing.
 */
function oracleCounts(total: number, limit: number) {
  const usable = Math.max(0, limit - 1);
  const attached = Math.min(total, usable);
  return { attached, omitted: total - attached };
}

const HINT_PATTERN =
  /^(\d+) of (\d+) examples? will be attached, (\d+) omitted \(this model accepts (\d+) images? per request, one reserved for the dataset image\)\.$/;

/** Assert the displayed hint reports the shared selection's split. */
function expectHint(container: HTMLElement, total: number, limit: number) {
  const hint = attachHint(container);
  expect(hint).not.toBeNull();
  const match = HINT_PATTERN.exec(hint!);
  expect(match, `unparseable hint: ${hint}`).not.toBeNull();
  const [, attached, shownTotal, omitted, shownLimit] = match!;
  const oracle = oracleCounts(total, limit);
  expect({
    attached: Number(attached),
    omitted: Number(omitted),
  }).toEqual(oracle);
  // ...and the shared helper the wizard and the backend agree on.
  expect({
    attached: Number(attached),
    omitted: Number(omitted),
  }).toEqual(fewShotAttachmentCounts(total, limit));
  expect(Number(shownTotal)).toBe(total);
  expect(Number(shownLimit)).toBe(limit);
}

const exampleFiles = (kind: string, count: number) =>
  Array.from({ length: count }, (_, i) => pngFile(`${kind}-${i}.png`));

describe('Feature: llm-autolabel-prompt-tuning, Property 20: Attached and omitted counts shown match what is attached', () => {
  /**
   * *For any* stored example set and any resolved Model_Image_Limit, the
   * attached and omitted example counts the Portal displays equal the sizes
   * of the attached and omitted lists the shared selection produces for that
   * set and limit, recomputed after every change to the selected model or
   * the example set.
   *
   * **Validates: Requirements 7.5**
   */
  it('reports the shared selection split and recomputes on every change', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 0, max: 10 }),
        fc.integer({ min: 0, max: 10 }),
        fc.constantFrom(...LIMIT_CATALOG),
        fc.constantFrom(...LIMIT_CATALOG),
        async (goodCount, badCount, firstModel, secondModel) => {
          // The hint is shown only once at least one example image is stored.
          fc.pre(goodCount + badCount > 0);

          cleanup();
          primeMocks({ models: LIMIT_CATALOG });

          const { container } = await renderToDdaSetup('ObjectDetection');
          enableAutoLabel(container);
          addExampleFiles(container, 'good', exampleFiles('good', goodCount));
          addExampleFiles(container, 'bad', exampleFiles('bad', badCount));

          selectAutoLabelModel(container, `llm:${firstModel.id}`);
          // No hint while the option is off.
          expect(attachHint(container)).toBeNull();
          setFewShot(container, true);

          const total = goodCount + badCount;
          expectHint(container, total, resolvedLimit(firstModel));

          // Recomputed when the selected model changes.
          selectAutoLabelModel(container, `llm:${secondModel.id}`);
          expectHint(container, total, resolvedLimit(secondModel));

          // Recomputed when the stored example set changes.
          addExampleFiles(container, 'good', [pngFile('good-extra.png')]);
          expectHint(container, total + 1, resolvedLimit(secondModel));
        }
      ),
      { numRuns: 100 }
    );
  }, 900_000);
});

// ---------------------------------------------------------------------------
// Property 21 (task 12.10)
// ---------------------------------------------------------------------------

const SUBMIT_MODEL_A = { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' };
const SUBMIT_MODEL_B = {
  id: 'us.anthropic.claude-sonnet-4-v1:0',
  label: 'Claude Sonnet',
};
const SUBMIT_CATALOG = [SUBMIT_MODEL_A, SUBMIT_MODEL_B];

describe('Feature: llm-autolabel-prompt-tuning, Property 21: Submission carries the form’s values, not a run’s values', () => {
  /**
   * *For any* sequence of Preview_Runs followed by job submission, the
   * submitted Detection_Prompt, LLM_Auto_Label_Model and Few_Shot_Option
   * equal the values held in the job creation form at submission time,
   * independently of the configuration of any completed Preview_Run. (The
   * persistence half of the property — designations and positions recovering
   * the submitted example set — is asserted by the backend creation test of
   * task 5.2.)
   *
   * **Validates: Requirements 5.5, 6.4**
   */
  it('submits the form values even after a run used different ones', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.record({
          runModel: fc.constantFrom(SUBMIT_MODEL_A.id, SUBMIT_MODEL_B.id),
          runPrompt: fc.constantFrom('find scratches', 'find dents'),
          runFewShot: fc.boolean(),
          formModel: fc.constantFrom(SUBMIT_MODEL_A.id, SUBMIT_MODEL_B.id),
          formPrompt: fc.constantFrom('find scratches', 'find dents'),
          formFewShot: fc.boolean(),
        }),
        async (values) => {
          // The property is about divergence, so at least one value differs.
          fc.pre(
            values.runModel !== values.formModel ||
              values.runPrompt !== values.formPrompt ||
              values.runFewShot !== values.formFewShot
          );

          cleanup();
          primeMocks({
            models: SUBMIT_CATALOG,
            images: [LISTED_KEYS[0]],
          });
          const sampleKey = LISTED_KEYS[0];
          apiMocks.getPreviewRun.mockResolvedValue(
            runStatus('run-1', 'Completed', [failedEntry(sampleKey, 0)])
          );

          const { container } = await renderToDdaSetup('ObjectDetection');
          await fillTeamAndLabel(container, 'ObjectDetection');
          enableAutoLabel(container);
          addExampleFiles(container, 'good', [pngFile('good-1.png')]);

          // Configure and run a preview with the run's values.
          selectAutoLabelModel(container, `llm:${values.runModel}`);
          fireEvent.change(promptField(), {
            target: { value: values.runPrompt },
          });
          setFewShot(container, values.runFewShot);

          await waitFor(() =>
            expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
          );
          fireEvent.click(sampleCheckbox(sampleKey));
          await act(async () => {
            fireEvent.click(screen.getByTestId('preview-run-button'));
          });
          await waitFor(() =>
            expect(apiMocks.getPreviewRun).toHaveBeenCalledTimes(1)
          );
          expect(apiMocks.startPreviewRun).toHaveBeenCalledWith(
            expect.objectContaining({
              model: `llm:${values.runModel}`,
              detection_prompt: values.runPrompt,
              few_shot: expect.objectContaining({
                enabled: values.runFewShot,
              }),
            })
          );

          // Now diverge the form and submit.
          selectAutoLabelModel(container, `llm:${values.formModel}`);
          fireEvent.change(promptField(), {
            target: { value: values.formPrompt },
          });
          setFewShot(container, values.formFewShot);

          clickNext();
          fireEvent.click(
            await screen.findByRole('button', { name: 'Create Job' })
          );
          await waitFor(() =>
            expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
          );

          const payload = apiMocks.createLabelingJob.mock.calls[0][0];
          expect(payload.auto_label).toEqual({
            enabled: true,
            model: `llm:${values.formModel}`,
            detection_prompt: values.formPrompt,
            // The sizing controls' form values at submission time — the
            // untouched defaults here: a blank downscale select submits
            // `null` (Downscale_Off) and the budget pre-fill (the catalog
            // carries no token_limit, so the 10000 fallback) travels as
            // the Token_Budget_Selection (llm-model-token-and-image-sizing
            // Req 3.1, 3.6, 5.7).
            downscale_max_edge: null,
            token_budget: 10000,
          });
          expect(payload.few_shot.enabled).toBe(values.formFewShot);
          expect(payload.few_shot.examples).toEqual(
            values.formFewShot
              ? fewShotExamplesFromRefs(payload.example_images)
              : []
          );

          // Explicitly: nothing from the completed run leaked into the job.
          if (values.runModel !== values.formModel) {
            expect(payload.auto_label.model).not.toBe(`llm:${values.runModel}`);
          }
          if (values.runPrompt !== values.formPrompt) {
            expect(payload.auto_label.detection_prompt).not.toBe(
              values.runPrompt
            );
          }
          if (values.runFewShot !== values.formFewShot) {
            expect(payload.few_shot.enabled).not.toBe(values.runFewShot);
          }
        }
      ),
      { numRuns: 100 }
    );
  }, 900_000);
});

// ---------------------------------------------------------------------------
// llm-model-token-and-image-sizing (task 11.4) — the frontend halves of the
// sizing criteria the design's placement table routes to this file
// (3.1–3.4, 3.11, 5.1–5.4, 5.6, 7.7, 9.8). The backend halves live in the
// backend property suites; nothing above is weakened.
// ---------------------------------------------------------------------------

/** Max_Image_Edge_Options, restated from the requirement (criterion 5.1). */
const SIZING_EDGE_OPTIONS = [512, 768, 1024, 1280, 1536, 2048] as const;
/** The accepted Token_Budget_Selection range (criteria 3.1, 3.3). */
const SIZING_BUDGET_RANGE = '1 to 128000';
/** The exact client-side violation wording for an out-of-range budget. */
const SIZING_BUDGET_VIOLATION = `The output token budget must be a whole number from ${SIZING_BUDGET_RANGE}`;

/** Wording for a run's applied Downscale_Setting (criterion 9.8). */
const appliedDownscaleText = (value: number | null | undefined): string =>
  typeof value === 'number' &&
  (SIZING_EDGE_OPTIONS as readonly number[]).includes(value)
    ? `${value} pixels`
    : 'off';

/** The Downscale_Setting select, or null while the control is not rendered. */
function downscaleSelect() {
  const host = screen.queryByTestId('preview-downscale-select');
  return host ? createWrapper(host).findSelect() : null;
}

/** The Token_Budget_Selection native input, or null while not rendered. */
function budgetInput(): HTMLInputElement | null {
  const host = screen.queryByTestId('preview-token-budget-input');
  if (!host) return null;
  return createWrapper(host)
    .findInput()!
    .findNativeInput()
    .getElement() as HTMLInputElement;
}

/** Select a Downscale_Setting in the control; `null` picks Downscale_Off. */
function setDownscale(value: number | null) {
  const select = downscaleSelect();
  if (!select) throw new Error('The downscale control is not rendered');
  select.openDropdown();
  if (value === null) select.selectOption(1);
  else select.selectOptionByValue(String(value));
}

const normalized = (node: HTMLElement): string =>
  (node.textContent || '').replace(/\s+/g, ' ').trim();

describe('Feature: llm-model-token-and-image-sizing, Frontend halves (criteria 3.1, 3.4, 5.1, 5.2, 5.3): sizing controls appear exactly for the llm: family and ride its run requests', () => {
  /**
   * *For any* auto-label model family (`llm:`, `sam`, `bedrock:`, none),
   * *any* wizard-held Downscale_Setting and *any* valid-or-empty
   * Token_Budget_Selection entry: the Downscale_Setting control and the
   * Token_Budget_Selection control are presented if and only if the model
   * is in the `llm:` family; when presented, the select offers exactly
   * seven options — Downscale_Off, selected by default, plus the six
   * Max_Image_Edge values labelled with their value in pixels — and the
   * budget control displays the accepted range alongside; a started run
   * carries the selected Downscale_Setting, and carries the entered budget
   * exactly when the entry is non-empty; for every other family no
   * Preview_API request is issued at all, so neither value is ever sent.
   *
   * **Validates: Requirements 3.1, 3.4, 5.1, 5.2, 5.3**
   */
  it('offers the two controls for llm: only and sends exactly the control values', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.constantFrom<'llm' | 'sam' | 'bedrock' | 'none'>(
          'llm',
          'sam',
          'bedrock',
          'none'
        ),
        fc.constantFrom<number | null | undefined>(
          undefined,
          null,
          ...SIZING_EDGE_OPTIONS
        ),
        fc.oneof(
          fc.constant(''),
          fc.integer({ min: 1, max: 128000 }).map(String)
        ),
        async (family, downscale, budgetEntry) => {
          cleanup();
          primeMocks({ images: LISTED_KEYS.slice(0, 1) });

          const model =
            family === 'llm'
              ? `llm:${PREVIEW_MODEL.id}`
              : family === 'sam'
                ? 'sam'
                : family === 'bedrock'
                  ? `bedrock:${PREVIEW_MODEL.id}`
                  : '';

          render(
            <PromptTuningPreview
              {...previewProps({
                model,
                downscaleMaxEdge: downscale,
                tokenBudget: budgetEntry,
              })}
            />
          );
          await waitFor(() =>
            expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
          );
          fireEvent.click(sampleCheckbox(LISTED_KEYS[0]));

          if (family !== 'llm') {
            // Hidden for `sam`, `bedrock:` and no model (criterion 5.2)...
            expect(screen.queryByTestId('preview-sizing-controls')).toBeNull();
            expect(screen.queryByTestId('preview-downscale-select')).toBeNull();
            expect(
              screen.queryByTestId('preview-token-budget-input')
            ).toBeNull();

            // ...and the preview issues no request at all for those
            // families, so no request of theirs can carry either value.
            fireEvent.click(runButton());
            expect(
              screen
                .getAllByTestId('preview-validation-error')
                .some((node) =>
                  /is not a prompt-guided LLM model/.test(node.textContent || '')
                )
            ).toBe(true);
            expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();
            return;
          }

          // Presented within the preview controls (criteria 3.1, 5.1).
          expect(
            screen.getByTestId('preview-sizing-controls')
          ).toBeInTheDocument();

          const select = downscaleSelect()!;
          const triggerText = normalized(select.findTrigger().getElement());

          // Exactly seven options: Downscale_Off plus each Max_Image_Edge
          // value labelled with its value in pixels (criterion 5.1).
          select.openDropdown();
          const optionLabels = select
            .findDropdown()
            .findOptions()
            .map((option) => normalized(option.getElement()));
          select.closeDropdown();
          expect(optionLabels).toHaveLength(7);
          expect(optionLabels.slice(1)).toEqual(
            SIZING_EDGE_OPTIONS.map((edge) => `${edge} pixels`)
          );

          // The control shows the wizard's setting; absent and null are the
          // Downscale_Off default (criterion 5.1).
          if (typeof downscale === 'number') {
            expect(triggerText).toBe(`${downscale} pixels`);
          } else {
            expect(triggerText).toBe(optionLabels[0]);
          }

          // The budget control holds the wizard's entry and displays the
          // accepted range alongside (criterion 3.1).
          expect(budgetInput()!.value).toBe(budgetEntry);
          expect(
            screen.getByTestId('preview-sizing-controls').textContent
          ).toContain(`Whole number from ${SIZING_BUDGET_RANGE}`);

          // A started run carries the setting as selected, and the budget
          // exactly when the entry is non-empty (criteria 3.4, 5.3).
          await act(async () => {
            fireEvent.click(runButton());
          });
          expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1);
          const request = apiMocks.startPreviewRun.mock.calls[0][0];
          expect(request.downscale_max_edge).toBe(downscale ?? null);
          if (budgetEntry === '') {
            expect('token_budget' in request).toBe(false);
          } else {
            expect(request.token_budget).toBe(Number(budgetEntry));
          }
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});

describe('Feature: llm-model-token-and-image-sizing, Frontend half of Property 11 (criterion 3.3): client-side budget rejection sends nothing and keeps state', () => {
  /**
   * *For any* non-empty Token_Budget_Selection entry that is not a whole
   * number from 1 to 128000 — non-numeric text, decimals, exponents, zero,
   * negatives and above-ceiling integers — starting a Preview_Run is
   * rejected with a validation message naming the accepted range, no
   * Preview_API request and no upload is issued, and every entered value
   * (the sample selection, the Downscale_Setting and the entry itself) is
   * retained; correcting the entry to any whole number in range then starts
   * a run carrying that number over the same retained selection.
   *
   * **Validates: Requirements 3.3, 3.4, 3.11**
   */
  it('rejects the start naming the range, issues nothing and retains every value', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.oneof(
          fc.constantFrom(
            '0',
            '-1',
            '128001',
            '12.5',
            'abc',
            '1e3',
            '- 7',
            'ten thousand'
          ),
          fc.integer({ min: 128001, max: 100_000_000 }).map(String),
          fc.integer({ min: -100_000_000, max: 0 }).map(String)
        ),
        fc.constantFrom<number | null>(null, ...SIZING_EDGE_OPTIONS),
        fc.integer({ min: 1, max: SAMPLE_LIMIT }),
        fc.integer({ min: 1, max: 128000 }),
        async (invalidEntry, downscale, selectedCount, correctedBudget) => {
          cleanup();
          primeMocks({ images: LISTED_KEYS.slice(0, SAMPLE_LIMIT) });

          render(
            <PromptTuningPreview
              {...previewProps({
                downscaleMaxEdge: downscale,
                tokenBudget: invalidEntry,
              })}
            />
          );
          await waitFor(() =>
            expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
          );
          const chosen = LISTED_KEYS.slice(0, selectedCount);
          for (const key of chosen) fireEvent.click(sampleCheckbox(key));

          const selectionBefore = screen.getByTestId('preview-selection-count')
            .textContent;
          const triggerBefore = normalized(
            downscaleSelect()!.findTrigger().getElement()
          );

          fireEvent.click(runButton());

          // The one violated rule is named with the accepted range
          // (criterion 3.3).
          expect(
            screen
              .getAllByTestId('preview-validation-error')
              .map((node) => node.textContent || '')
          ).toEqual([SIZING_BUDGET_VIOLATION]);

          // Nothing was sent, nothing was uploaded, no result set appeared.
          expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();
          expect(fetchMock).not.toHaveBeenCalled();
          expect(screen.queryByTestId('preview-results')).toBeNull();

          // Every entered value is retained and the flow stays usable.
          expect(
            screen.getByTestId('preview-selection-count').textContent
          ).toBe(selectionBefore);
          expect(chosen.every((key) => sampleCheckbox(key).checked)).toBe(true);
          expect(
            normalized(downscaleSelect()!.findTrigger().getElement())
          ).toBe(triggerBefore);
          expect(runButton()).toBeEnabled();

          // Correcting the entry lets the same flow start a run carrying
          // the corrected number and the retained setting and samples
          // (criteria 3.4, 5.3).
          fireEvent.change(budgetInput()!, {
            target: { value: String(correctedBudget) },
          });
          await act(async () => {
            fireEvent.click(runButton());
          });
          expect(
            screen.queryAllByTestId('preview-validation-error')
          ).toHaveLength(0);
          expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1);
          const request = apiMocks.startPreviewRun.mock.calls[0][0];
          expect(request.token_budget).toBe(correctedBudget);
          expect(request.downscale_max_edge).toBe(downscale);
          expect(request.sample_images).toEqual(chosen);
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});

// ---------------------------------------------------------------------------
// Sizing display (criteria 5.4, 5.11, 7.7)
// ---------------------------------------------------------------------------

/** The four payload dimension fields of the sizing display. */
const SIZING_DIM_FIELDS = [
  'source_width',
  'source_height',
  'sent_width',
  'sent_height',
] as const;

interface SizingDims {
  source_width?: number;
  source_height?: number;
  sent_width?: number;
  sent_height?: number;
}

const presentDimsArb = fc.record({
  source_width: fc.integer({ min: 1, max: 8000 }),
  source_height: fc.integer({ min: 1, max: 8000 }),
  sent_width: fc.integer({ min: 1, max: 8000 }),
  sent_height: fc.integer({ min: 1, max: 8000 }),
});

/**
 * Dimension quadruples over every display branch: arbitrary positive pairs
 * (hitting the 1% floor and the 100% cap), the equal pair (exactly 100%),
 * the backend's floor-scaled downscale algebra, and quadruples with a
 * missing or non-positive subset (the unavailable branch, criterion 5.11).
 */
const sizingDimsArb: fc.Arbitrary<SizingDims> = fc.oneof(
  presentDimsArb,
  fc
    .record({
      w: fc.integer({ min: 1, max: 8000 }),
      h: fc.integer({ min: 1, max: 8000 }),
    })
    .map(({ w, h }) => ({
      source_width: w,
      source_height: h,
      sent_width: w,
      sent_height: h,
    })),
  fc
    .record({
      w: fc.integer({ min: 600, max: 8000 }),
      h: fc.integer({ min: 600, max: 8000 }),
      edge: fc.constantFrom(...SIZING_EDGE_OPTIONS),
    })
    .map(({ w, h, edge }) => {
      const long = Math.max(w, h);
      if (long <= edge) {
        return {
          source_width: w,
          source_height: h,
          sent_width: w,
          sent_height: h,
        };
      }
      return {
        source_width: w,
        source_height: h,
        sent_width: Math.max(1, Math.floor((w * edge) / long)),
        sent_height: Math.max(1, Math.floor((h * edge) / long)),
      };
    }),
  fc
    .record({
      base: presentDimsArb,
      knockout: fc.subarray([...SIZING_DIM_FIELDS], {
        minLength: 1,
        maxLength: 4,
      }),
      replacement: fc.constantFrom<number | undefined>(undefined, 0, -3),
    })
    .map(({ base, knockout, replacement }) => {
      const dims: SizingDims = { ...base };
      for (const field of knockout) {
        if (replacement === undefined) delete dims[field];
        else dims[field] = replacement;
      }
      return dims;
    })
);

/**
 * The expected sizing row, re-derived from criteria 5.4 and 5.11 rather
 * than taken from the component's own helper: all four extents present and
 * positive yields `W × H → w × h (P%)` with `P` the longer-edge ratio
 * rounded to the nearest whole percent and clamped into [1, 100]; anything
 * else yields the unavailable indication.
 */
function expectedSizingText(dims: SizingDims): string {
  const values = [
    dims.source_width,
    dims.source_height,
    dims.sent_width,
    dims.sent_height,
  ];
  if (
    !values.every(
      (value) =>
        typeof value === 'number' && Number.isFinite(value) && value > 0
    )
  ) {
    return 'dimensions unavailable';
  }
  const [sourceWidth, sourceHeight, sentWidth, sentHeight] =
    values as number[];
  const percent = Math.min(
    100,
    Math.max(
      1,
      Math.round(
        (Math.max(sentWidth, sentHeight) /
          Math.max(sourceWidth, sourceHeight)) *
          100
      )
    )
  );
  return `${sourceWidth} × ${sourceHeight} → ${sentWidth} × ${sentHeight} (${percent}%)`;
}

type SizingOutcome =
  | { kind: 'ok'; dims: SizingDims }
  | {
      kind: 'failed';
      dims: SizingDims;
      category: (typeof FAILURE_CATEGORIES)[number];
      reason: string;
    };

const sizingOutcomeArb: fc.Arbitrary<SizingOutcome> = fc.oneof(
  sizingDimsArb.map((dims): SizingOutcome => ({ kind: 'ok', dims })),
  fc
    .record({
      dims: sizingDimsArb,
      category: fc.constantFrom(...FAILURE_CATEGORIES),
      reason: reasonArb,
    })
    .map((f): SizingOutcome => ({ kind: 'failed', ...f }))
);

/**
 * The Source space every successful payload of the sizing-display property
 * declares, and a probe box whose expected overlay percentages differ per
 * axis — 15%/20%/25%/40% — so geometry positioned against anything but
 * `image_width`/`image_height` (the Source_Dimensions) is caught.
 */
const SIZING_SOURCE_SPACE = { width: 200, height: 100 };
const SIZING_PROBE_BOX = {
  class: 'scratch',
  left: 30,
  top: 20,
  width: 50,
  height: 40,
};

describe('Feature: llm-model-token-and-image-sizing, Frontend halves (criteria 5.4, 5.11, 7.7): the sizing display reports Source → Sent with a clamped whole percent or the unavailable indication', () => {
  /**
   * *For any* mix of resolved Preview_Results and *any* dimension
   * quadruples on their payloads: each entry whose Source_Dimensions and
   * Sent_Dimensions are all determined shows them with the longer-edge
   * ratio as a whole percent within 1 to 100 inclusive, as read-only text;
   * each entry with any missing or non-positive extent shows the
   * unavailable indication instead, with the rest of that result still
   * rendered (the failure's category and reason, or the successful
   * result's overlay); and successful geometry stays positioned in the
   * Source_Dimensions space, unaffected by the Sent_Dimensions.
   *
   * **Validates: Requirements 5.4, 5.11, 7.7**
   */
  it('renders each sizing row from its own payload and keeps geometry in Source space', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(sizingOutcomeArb, { minLength: 1, maxLength: SAMPLE_LIMIT }),
        async (outcomes) => {
          cleanup();
          primeMocks({ images: LISTED_KEYS.slice(0, outcomes.length) });
          const keys = LISTED_KEYS.slice(0, outcomes.length);

          apiMocks.startPreviewRun.mockResolvedValue({
            run_id: 'run-1',
            sample_count: keys.length,
            status: 'Running',
          });
          apiMocks.getPreviewRun.mockResolvedValue(
            runStatus(
              'run-1',
              'Completed',
              keys.map((key, index) => {
                const outcome = outcomes[index];
                return {
                  index,
                  sample_key: key,
                  state: (outcome.kind === 'failed'
                    ? 'Failed'
                    : 'Succeeded') as SampleState,
                  result_url: `https://payloads.example/${index}.json`,
                  ...(outcome.kind === 'failed'
                    ? {
                        failure_category: outcome.category,
                        failure_reason: outcome.reason,
                      }
                    : {}),
                };
              })
            )
          );
          fetchMock.mockImplementation(async (url: string) => {
            const index = Number(/\/(\d+)\.json$/.exec(url)?.[1] ?? '0');
            const outcome = outcomes[index];
            const body =
              outcome.kind === 'failed'
                ? {
                    sample_key: keys[index],
                    state: 'Failed',
                    failure_category: outcome.category,
                    failure_reason: outcome.reason,
                    ...outcome.dims,
                  }
                : {
                    sample_key: keys[index],
                    state: 'Succeeded',
                    prelabel: {
                      modality: 'ObjectDetection',
                      boxes: [SIZING_PROBE_BOX],
                    },
                    image_width: SIZING_SOURCE_SPACE.width,
                    image_height: SIZING_SOURCE_SPACE.height,
                    ...outcome.dims,
                  };
            return { ok: true, status: 200, json: async () => body };
          });

          render(<PromptTuningPreview {...previewProps()} />);
          await waitFor(() =>
            expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
          );
          for (const key of keys) fireEvent.click(sampleCheckbox(key));
          await act(async () => {
            fireEvent.click(runButton());
          });
          await waitFor(() =>
            expect(screen.getByTestId('preview-results')).toBeInTheDocument()
          );

          expect(displayedKeys()).toEqual(keys);
          const entries = screen.getAllByTestId('preview-result-entry');

          outcomes.forEach((outcome, index) => {
            const entry = within(entries[index]);

            // The sizing row equals the payload-derived oracle (5.4, 5.11).
            const sizingNode = entry.getByTestId('preview-result-sizing');
            const sizingText = normalized(sizingNode);
            expect(sizingText).toBe(expectedSizingText(outcome.dims));

            // A displayed percentage is a whole percent within 1..100, and
            // the row is read-only text (criterion 5.4).
            const percentMatch = /\((\d+)%\)$/.exec(sizingText);
            if (sizingText !== 'dimensions unavailable') {
              expect(percentMatch).not.toBeNull();
              const percent = Number(percentMatch![1]);
              expect(percent).toBeGreaterThanOrEqual(1);
              expect(percent).toBeLessThanOrEqual(100);
            }
            expect(
              sizingNode.querySelector('input, textarea, select, button')
            ).toBeNull();

            if (outcome.kind === 'failed') {
              // The rest of the failed result still renders (5.11).
              expect(
                entry.getByTestId('preview-result-failure')
              ).toBeInTheDocument();
              expect(
                entry.getByTestId('preview-failure-category').textContent
              ).toBe(CATEGORY_TEXT[outcome.category]);
              expect(
                entry.getByTestId('preview-failure-reason')
              ).toHaveTextContent(outcome.reason);
            } else {
              // The rest of the successful result still renders, and its
              // geometry is positioned against the Source_Dimensions alone
              // (criterion 7.7): the probe box's percentages derive from
              // image_width/image_height whatever the sent_* fields say.
              expect(
                entry.getByTestId('preview-result-image')
              ).toBeInTheDocument();
              const box = entry.getByTestId('preview-box');
              expect(box.style.left).toBe('15%');
              expect(box.style.top).toBe('20%');
              expect(box.style.width).toBe('25%');
              expect(box.style.height).toBe('40%');
            }
          });
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});

describe('Feature: llm-model-token-and-image-sizing, Frontend halves (criteria 3.11, 5.6, 9.8): failed results name the run’s applied setting and budget, and changed controls apply to the next run', () => {
  /**
   * *For any* completed Preview_Run whose results failed, *any* applied
   * Downscale_Setting and Effective_Token_Budget reported by the run
   * status, and *any* subsequently chosen control values: every failed
   * result shows the run's applied Downscale_Setting and
   * Effective_Token_Budget beside its category and reason; changing either
   * control afterwards keeps the sample selection, the displayed results
   * and every other value, keeps the run control enabled, and the next run
   * carries the changed values — whose failures then report the new
   * applied values.
   *
   * **Validates: Requirements 3.11, 5.6, 9.8**
   */
  it('shows the applied values beside each failure and applies changed controls to the next run', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.record({
          firstDownscale: fc.constantFrom<number | null | undefined>(
            undefined,
            null,
            ...SIZING_EDGE_OPTIONS
          ),
          firstBudget: fc.oneof(
            fc.constant(undefined),
            fc.integer({ min: 1, max: 128000 })
          ),
          category: fc.constantFrom(...FAILURE_CATEGORIES),
          reason: reasonArb,
          sampleCount: fc.integer({ min: 1, max: 3 }),
          nextDownscale: fc.constantFrom<number | null>(
            null,
            ...SIZING_EDGE_OPTIONS
          ),
          nextBudget: fc.integer({ min: 1, max: 128000 }),
        }),
        async (input) => {
          cleanup();
          primeMocks({ images: LISTED_KEYS.slice(0, input.sampleCount) });
          const keys = LISTED_KEYS.slice(0, input.sampleCount);

          const failedEntries: EntryFixture[] = keys.map((key, index) => ({
            index,
            sample_key: key,
            state: 'Failed',
            failure_category: input.category,
            failure_reason: input.reason,
          }));
          apiMocks.getPreviewRun.mockResolvedValue({
            ...runStatus('run-1', 'Completed', failedEntries),
            // `undefined` models a status response omitting the field.
            ...(input.firstDownscale !== undefined
              ? { downscale_max_edge: input.firstDownscale }
              : {}),
            ...(input.firstBudget !== undefined
              ? { token_budget: input.firstBudget }
              : {}),
          });

          render(<PromptTuningPreview {...previewProps()} />);
          await waitFor(() =>
            expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
          );
          for (const key of keys) fireEvent.click(sampleCheckbox(key));
          await act(async () => {
            fireEvent.click(runButton());
          });

          // Every failed result names the *run's* applied values beside its
          // category and reason (criterion 9.8).
          expect(displayedKeys()).toEqual(keys);
          for (const element of screen.getAllByTestId('preview-result-entry')) {
            const entry = within(element);
            expect(
              entry.getByTestId('preview-failure-category').textContent
            ).toBe(CATEGORY_TEXT[input.category]);
            expect(
              entry.getByTestId('preview-failure-reason')
            ).toHaveTextContent(input.reason);
            expect(
              normalized(entry.getByTestId('preview-failure-downscale'))
            ).toBe(
              `Image downscaling: ${appliedDownscaleText(input.firstDownscale)}`
            );
            expect(
              normalized(entry.getByTestId('preview-failure-token-budget'))
            ).toBe(
              `Output token budget: ${
                typeof input.firstBudget === 'number'
                  ? input.firstBudget
                  : 'not reported'
              }`
            );
          }

          // Changing both controls after the completed run keeps the sample
          // selection and the displayed run — which keeps naming *its*
          // applied values — and the run control stays usable (3.11, 5.6).
          setDownscale(input.nextDownscale);
          fireEvent.change(budgetInput()!, {
            target: { value: String(input.nextBudget) },
          });

          expect(displayedKeys()).toEqual(keys);
          expect(keys.every((key) => sampleCheckbox(key).checked)).toBe(true);
          expect(
            screen.getByTestId('preview-selection-count')
          ).toHaveTextContent(
            `${keys.length} of ${SAMPLE_LIMIT} sample images selected`
          );
          expect(runButton()).toBeEnabled();
          expect(
            normalized(
              within(
                screen.getAllByTestId('preview-result-entry')[0]
              ).getByTestId('preview-failure-downscale')
            )
          ).toBe(
            `Image downscaling: ${appliedDownscaleText(input.firstDownscale)}`
          );

          // The next run carries the changed values over the same samples
          // (criteria 3.11, 5.6)...
          apiMocks.startPreviewRun.mockReset();
          apiMocks.getPreviewRun.mockReset();
          apiMocks.startPreviewRun.mockResolvedValue({
            run_id: 'run-2',
            sample_count: keys.length,
            status: 'Running',
          });
          apiMocks.getPreviewRun.mockResolvedValue({
            ...runStatus('run-2', 'Completed', failedEntries),
            downscale_max_edge: input.nextDownscale,
            token_budget: input.nextBudget,
          });
          await act(async () => {
            fireEvent.click(runButton());
          });
          expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1);
          const request = apiMocks.startPreviewRun.mock.calls[0][0];
          expect(request.downscale_max_edge).toBe(input.nextDownscale);
          expect(request.token_budget).toBe(input.nextBudget);
          expect(request.sample_images).toEqual(keys);
          expect(request.detection_prompt).toBe(
            'Find scratches on the surface'
          );

          // ...and its failures report the new applied values (9.8).
          for (const element of screen.getAllByTestId('preview-result-entry')) {
            const entry = within(element);
            expect(
              normalized(entry.getByTestId('preview-failure-downscale'))
            ).toBe(
              `Image downscaling: ${appliedDownscaleText(input.nextDownscale)}`
            );
            expect(
              normalized(entry.getByTestId('preview-failure-token-budget'))
            ).toBe(`Output token budget: ${input.nextBudget}`);
          }
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});
