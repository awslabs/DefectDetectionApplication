/**
 * Property-based tests for the grounded-sam Prompt_Tuning_Preview wizard
 * surface (grounded-sam-prompt-tuning-preview task 2.7, design Properties
 * 1 and 3), following the render-per-run walk of
 * `CreateLabelingJob.groundedsam.property.test.tsx`: every fast-check run
 * mounts `CreateLabelingJob` fresh (`localStorage.clear()` per run),
 * drives it to the DDA labeling-setup step, and asserts against oracles
 * that restate the specified semantics independently of the
 * implementation.
 *
 * **Feature: grounded-sam-prompt-tuning-preview, Property 1: The
 * Preview_Panel renders exactly for the preview families, without the
 * llm-only controls under grounded-sam**
 *
 * *For any* auto-label model selection (`grounded-sam`, `sam`,
 * `bedrock:*`, `llm:*`, none) and any offered Labeling_Modality, the
 * wizard's setup step SHALL render the Preview_Panel exactly when
 * auto-labeling is enabled and the model is `llm:`-prefixed or
 * `grounded-sam`; and whenever the model is `grounded-sam`, the detection
 * prompt entry, few-shot toggle, and sizing controls SHALL be absent.
 *
 * **Validates: Requirements 1.1, 1.2, 1.4, 1.5**
 *
 * **Feature: grounded-sam-prompt-tuning-preview, Property 3: A started
 * grounded-sam run's request carries exactly the job's pruned prompts and
 * no llm-only fields**
 *
 * *For any* guardrail-clean Label_Set and Prompt_Override entry states,
 * the started run's request body SHALL carry `model: 'grounded-sam'`, the
 * wizard's modality and Label_Set, the selected samples, and a
 * `prompt_overrides` map equal to exactly the entries non-empty after
 * trimming whose label is in the effective Label_Set (values
 * character-for-character), with no `detection_prompt`, `few_shot`,
 * `downscale_max_edge`, or `token_budget` key. Per the shipped
 * `PromptTuningPreview.handleStartRun`, the `prompt_overrides` key is
 * omitted entirely when no entry survives — the oracle asserts that exact
 * shape.
 *
 * **Validates: Requirements 2.1, 7.1**
 *
 * Settling note: unlike the pre-feature wizard suites, the Preview_Panel
 * now mounts under grounded-sam and fires its dataset listing call on
 * mount, so this file's mock table resolves `getImagePreview` explicitly
 * with a five-image listing — deterministic settling on the sample grid,
 * and real checkboxes for Property 3's sample selection. `getPreviewRun`
 * answers an already-Completed run so the poll loop terminates on its
 * first iteration with no fake timers.
 *
 * Generator domain notes (smart constraints, not oracle weakening),
 * carried from the groundedsam.property precedent:
 * - Label rows are pre-trimmed, distinct, within the wizard's label
 *   constraints, exclude `Object.prototype` member names (the wizard's
 *   per-label plain-object state idiom), and exclude CR/LF (single-line
 *   `<input>` values cannot hold them).
 * - Labels and override values are period-free: the shared
 *   Prompt_Guardrail rejects any period-bearing Effective_Prompt before a
 *   request is issued, so guardrail-clean scenarios — this property's
 *   declared domain — exclude '.' by construction. The guardrail gate
 *   itself is design Property 2 (task 2.6), not this file's surface.
 * - Override values stay within the 256-character limit (boundary
 *   lengths included); catalog ids for the `llm:`/`bedrock:` prongs come
 *   from a small plain-ASCII pool — the id spelling is decoration for
 *   Property 1's visibility oracle, and dropdown selection by value stays
 *   robust.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { SelectProps } from '@cloudscape-design/components';
import * as fc from 'fast-check';

import CreateLabelingJob, {
  BEDROCK_MODALITIES,
  GROUNDED_SAM_MODALITIES,
  LLM_MODALITIES,
  MAX_PROMPT_OVERRIDE_LENGTH,
  SAM_MODALITIES,
} from './CreateLabelingJob';
import { SAMPLE_LIMIT } from '../components/labeling/PromptTuningPreview';

const { apiMocks, navigateMock, recordedSelectProps, fetchMock } = vi.hoisted(
  () => ({
    apiMocks: {
      listUseCases: vi.fn(),
      listLabelingTeams: vi.fn(),
      getBedrockModels: vi.fn(),
      listWorkteams: vi.fn(),
      createLabelingJob: vi.fn(),
      getImagePreview: vi.fn(),
      startPreviewRun: vi.fn(),
      getPreviewRun: vi.fn(),
    },
    navigateMock: vi.fn(),
    /** Latest props each rendered Select received, keyed by placeholder. */
    recordedSelectProps: new Map<string, unknown>(),
    fetchMock: vi.fn(),
  })
);

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

// A non-admin Job_Creator: the skip-verification section stays hidden, so
// the setup step's selects are exactly [team, auto-label model].
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

// Pass-through spy on the Cloudscape Select: record the props, render the
// real component (the modelpicker/groundedsam property precedent). Used
// here to wait deterministically until the catalog-backed `llm:` /
// `bedrock:` options are offered before selecting them.
vi.mock('@cloudscape-design/components', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@cloudscape-design/components')>();
  const RealSelect = actual.Select;
  const Select = (props: SelectProps) => {
    if (typeof props.placeholder === 'string') {
      recordedSelectProps.set(props.placeholder, props);
    }
    return <RealSelect {...props} />;
  };
  return { ...actual, Select };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** One Model_Catalog entry as `getBedrockModels()` returns it. */
interface CatalogModel {
  id: string;
  label: string;
  image_limit?: number;
  token_limit?: number;
  image_input?: boolean;
}

/** The wizard's dataset URI and the prefix derivation it applies. */
const DATASET_S3_URI = 's3://bucket/images/';
const DATASET_PREFIX = 'images/';

/** Five listable dataset objects — exactly the Sample_Limit. */
const LISTED_KEYS = Array.from(
  { length: SAMPLE_LIMIT },
  (_, i) => `${DATASET_PREFIX}img-${i}.jpg`
);

/** The listing the preview's mount-time `getImagePreview` call resolves. */
const listing = (keys: string[]) => ({
  prefix: DATASET_PREFIX,
  bucket: 'data-bucket',
  total_found: keys.length,
  offset: 0,
  limit: 50,
  has_more: false,
  images: keys.map((key) => ({
    key,
    filename: key.slice(key.lastIndexOf('/') + 1),
    size: 1024,
    last_modified: '2024-05-01T00:00:00Z',
    presigned_url: `https://s3.example/${key}?sig=1`,
  })),
  expires_in_seconds: 900,
});

/** Reset every mock to a benign default around a generated catalog. */
function primeMocks(models: CatalogModel[]) {
  vi.clearAllMocks();
  recordedSelectProps.clear();
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'UC1', s3_bucket: 'out-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingTeams.mockResolvedValue({
    teams: [{ team_id: 't-1', team_name: 'Team One', members: ['a'] }],
    count: 1,
  });
  apiMocks.getBedrockModels.mockResolvedValue({
    models,
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
  apiMocks.getImagePreview.mockResolvedValue(listing(LISTED_KEYS));
  apiMocks.startPreviewRun.mockResolvedValue({
    run_id: 'run-1',
    sample_count: 1,
    status: 'Running',
  });
  // A run that is already Completed terminates the poll loop on its first
  // iteration, so no fake timers are needed anywhere in this file.
  apiMocks.getPreviewRun.mockResolvedValue({
    run_id: 'run-1',
    status: 'Completed',
    sample_count: 0,
    few_shot: { enabled: false, attached: 0, omitted: 0 },
    results: [],
  });
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({}),
  });
}

beforeEach(() => {
  window.localStorage.clear();
  primeMocks([]);
  vi.stubGlobal('fetch', fetchMock);
});

// ---------------------------------------------------------------------------
// Wizard walk helpers (the groundedsam.property precedent)
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
    { target: { value: 'gsam-preview-property-job' } }
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
    { target: { value: DATASET_S3_URI } }
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

/** The auto-label model select (the team select is the step's first select). */
const modelSelect = (container: HTMLElement) =>
  createWrapper(container).findAllSelects()[1];

function selectAutoLabelModel(container: HTMLElement, value: string) {
  const select = modelSelect(container);
  select.openDropdown();
  select.selectOptionByValue(value);
}

/** Latest props the wizard handed the auto-label Select. */
function autoLabelSelectProps(): SelectProps | undefined {
  return recordedSelectProps.get('Select an auto-label model') as
    | SelectProps
    | undefined;
}

/** Every option value the Select currently offers, groups flattened. */
function offeredOptionValues(options: SelectProps.Options | undefined): string[] {
  const values: string[] = [];
  for (const option of options ?? []) {
    if ('options' in option && Array.isArray(option.options)) {
      for (const child of option.options) values.push(child.value ?? '');
    } else {
      values.push((option as SelectProps.Option).value ?? '');
    }
  }
  return values;
}

/**
 * Select a catalog-backed option once the asynchronously resolving
 * catalog has offered it (the static `sam`/`grounded-sam` entries need no
 * wait; `llm:`/`bedrock:` ones do).
 */
async function selectAutoLabelModelWhenOffered(
  container: HTMLElement,
  value: string
) {
  await waitFor(() => {
    expect(offeredOptionValues(autoLabelSelectProps()?.options)).toContain(
      value
    );
  });
  selectAutoLabelModel(container, value);
}

/**
 * Exact-attribute input lookup: generated labels are arbitrary unicode,
 * and Testing Library's accessible-name matching normalizes whitespace,
 * so the label-row inputs (`aria-label="Label {n}"`) and the
 * Prompt_Override inputs (`aria-label="Text prompt for {label}"`) are
 * located by verbatim attribute equality instead.
 */
function inputByExactAriaLabel(
  container: HTMLElement,
  ariaLabel: string
): HTMLInputElement {
  const match = Array.from(container.querySelectorAll('input')).find(
    (el) => el.getAttribute('aria-label') === ariaLabel
  );
  if (!match) {
    throw new Error(`No input with aria-label ${JSON.stringify(ariaLabel)}`);
  }
  return match as HTMLInputElement;
}

const labelRowInput = (container: HTMLElement, rowIndex: number) =>
  inputByExactAriaLabel(container, `Label ${rowIndex + 1}`);

const overrideInput = (container: HTMLElement, label: string) =>
  inputByExactAriaLabel(container, `Text prompt for ${label}`);

/** The native checkbox input of one listed sample in the Preview_Panel. */
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

// ---------------------------------------------------------------------------
// Feature: grounded-sam-prompt-tuning-preview, Property 1: The
// Preview_Panel renders exactly for the preview families, without the
// llm-only controls under grounded-sam
// ---------------------------------------------------------------------------

/**
 * One auto-label selection state the wizard can reach: auto-labeling left
 * disabled, enabled with the selection cleared (never made), or enabled
 * with one of the four families selected.
 */
type SelectionKind =
  | 'disabled'
  | 'none'
  | 'sam'
  | 'grounded-sam'
  | 'bedrock'
  | 'llm';

/**
 * The model/modality offering matrix restated (grounded-sam-autolabel
 * Requirements 1.1, 1.2, 7.2): which families the picker offers per
 * modality — a family can only be selected where it is offered, so the
 * generator draws (modality, kind) pairs from this matrix. Its agreement
 * with the shipped exports is asserted before the property runs.
 */
const ORACLE_MODALITIES: Record<
  'sam' | 'grounded-sam' | 'bedrock' | 'llm',
  string[]
> = {
  sam: ['Segmentation', 'ObjectDetection'],
  'grounded-sam': ['Segmentation', 'ObjectDetection'],
  bedrock: ['Classification', 'ObjectDetection'],
  llm: ['Classification', 'Segmentation', 'ObjectDetection'],
};

interface VisibilityScenario {
  modality: string;
  kind: SelectionKind;
  catalogId: string;
}

const visibilityScenarioArb: fc.Arbitrary<VisibilityScenario> = fc
  .constantFrom('Classification', 'Segmentation', 'ObjectDetection')
  .chain((modality) => {
    const kinds: SelectionKind[] = [
      'disabled',
      'none',
      ...(['sam', 'grounded-sam', 'bedrock', 'llm'] as const).filter((kind) =>
        ORACLE_MODALITIES[kind].includes(modality)
      ),
    ];
    return fc.record({
      modality: fc.constant(modality),
      kind: fc.constantFrom(...(kinds as [SelectionKind, ...SelectionKind[]])),
      /** Plain-ASCII decoration for the catalog-backed prongs. */
      catalogId: fc.constantFrom('m-1', 'nova-lite', 'claude35'),
    });
  });

describe('Feature: grounded-sam-prompt-tuning-preview, Property 1: The Preview_Panel renders exactly for the preview families, without the llm-only controls under grounded-sam', () => {
  /**
   * *For any* auto-label model selection (`grounded-sam`, `sam`,
   * `bedrock:*`, `llm:*`, none) and any offered Labeling_Modality, the
   * setup step renders the Preview_Panel exactly when auto-labeling is
   * enabled and the model is `llm:`-prefixed or `grounded-sam`; under
   * `grounded-sam` the detection prompt entry, few-shot toggle, and
   * sizing controls are absent while the panel is present.
   *
   * **Validates: Requirements 1.1, 1.2, 1.4, 1.5**
   */
  it('renders the panel iff auto-labeling is enabled and the model is llm:-prefixed or grounded-sam, with no llm-only controls under grounded-sam', async () => {
    // The generator's offering matrix and the shipped one must agree
    // before it constrains the scenario space.
    expect(SAM_MODALITIES).toEqual(ORACLE_MODALITIES.sam);
    expect(GROUNDED_SAM_MODALITIES).toEqual(ORACLE_MODALITIES['grounded-sam']);
    expect(BEDROCK_MODALITIES).toEqual(ORACLE_MODALITIES.bedrock);
    expect(LLM_MODALITIES).toEqual(ORACLE_MODALITIES.llm);

    await fc.assert(
      fc.asyncProperty(visibilityScenarioArb, async (scenario) => {
        cleanup();
        window.localStorage.clear();
        primeMocks([{ id: scenario.catalogId, label: 'Nova' }]);

        const { container } = await renderToDdaSetup(scenario.modality);

        if (scenario.kind !== 'disabled') {
          enableAutoLabel(container);
          if (scenario.kind === 'sam' || scenario.kind === 'grounded-sam') {
            // Static entries: offered synchronously with the toggle.
            selectAutoLabelModel(container, scenario.kind);
          } else if (scenario.kind === 'bedrock') {
            await selectAutoLabelModelWhenOffered(
              container,
              `bedrock:${scenario.catalogId}`
            );
          } else if (scenario.kind === 'llm') {
            await selectAutoLabelModelWhenOffered(
              container,
              `llm:${scenario.catalogId}`
            );
          }
        }

        const panelExpected =
          scenario.kind === 'llm' || scenario.kind === 'grounded-sam';

        if (panelExpected) {
          // The Preview_Panel renders for the preview families
          // (Requirements 1.1, 1.2 for grounded-sam; the llm: family kept
          // rendering it). Settle on the sample grid: the panel fires its
          // listing call on mount and the mock lists five images.
          expect(
            await screen.findByTestId('prompt-tuning-preview')
          ).toBeInTheDocument();
          await screen.findByTestId('preview-sample-grid');

          if (scenario.kind === 'grounded-sam') {
            // None of the llm:-only controls render under grounded-sam
            // (Requirement 1.4): no detection prompt entry, no few-shot
            // toggle, no sizing controls (downscale or token budget).
            expect(screen.queryByLabelText('Detection prompt')).toBeNull();
            expect(
              screen.queryByText('Attach example images as few-shot examples')
            ).toBeNull();
            expect(
              screen.queryByTestId('preview-sizing-controls')
            ).toBeNull();
            expect(screen.queryByLabelText('Image downscaling')).toBeNull();
            expect(screen.queryByLabelText('Output token budget')).toBeNull();
          }
        } else {
          // `sam`, `bedrock:`, a cleared selection, or auto-labeling
          // disabled: no Preview_Panel (Requirement 1.5). Flush pending
          // resolutions (catalog fetch) so the absence is a settled state,
          // not a not-yet-rendered one.
          await act(async () => {});
          expect(screen.queryByTestId('prompt-tuning-preview')).toBeNull();
        }
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});

// ---------------------------------------------------------------------------
// Feature: grounded-sam-prompt-tuning-preview, Property 3: A started
// grounded-sam run's request carries exactly the job's pruned prompts and
// no llm-only fields
// ---------------------------------------------------------------------------

/**
 * Label rows: pre-trimmed distinct unicode names within the wizard's
 * label constraints; `Object.prototype` member names and CR/LF excluded;
 * period-free so every Effective_Prompt is guardrail-clean by
 * construction (see the header's generator domain notes).
 */
const labelArb = fc
  .string({ unit: 'grapheme', minLength: 1, maxLength: 8 })
  .filter(
    (s) =>
      s.trim() === s &&
      s.length > 0 &&
      s.length <= 64 &&
      !/[\r\n]/.test(s) &&
      !s.includes('.') &&
      !(s in Object.prototype)
  );

/**
 * Whitespace-only strings (dropped by the trim pruning). No CR/LF: the
 * override entries are single-line `<input>` elements.
 */
const whitespaceOnlyArb = fc
  .array(fc.constantFrom(' ', '\t', '\u00a0'), {
    minLength: 1,
    maxLength: 4,
  })
  .map((chars) => chars.join(''));

/**
 * Override entry values: unicode text, empty, whitespace-only,
 * whitespace-padded (raw value must survive character-for-character), and
 * boundary lengths 255/256 — always within the 256-character limit, and
 * period-free so the Prompt_Guardrail holds (guardrail-clean scenarios
 * are this property's declared domain).
 */
const overrideValueArb: fc.Arbitrary<string> = fc
  .oneof(
    {
      weight: 4,
      arbitrary: fc.string({ unit: 'grapheme', minLength: 1, maxLength: 12 }),
    },
    { weight: 1, arbitrary: fc.constant('') },
    { weight: 2, arbitrary: whitespaceOnlyArb },
    {
      weight: 2,
      arbitrary: fc
        .tuple(
          whitespaceOnlyArb,
          fc.string({ unit: 'grapheme', minLength: 1, maxLength: 8 }),
          whitespaceOnlyArb
        )
        .map(([lead, body, tail]) => `${lead}${body}${tail}`),
    },
    {
      weight: 1,
      arbitrary: fc
        .constantFrom(MAX_PROMPT_OVERRIDE_LENGTH - 1, MAX_PROMPT_OVERRIDE_LENGTH)
        .map((n) => 'p'.repeat(n)),
    }
  )
  .filter(
    (v) =>
      v.length <= MAX_PROMPT_OVERRIDE_LENGTH &&
      !/[\r\n]/.test(v) &&
      !v.includes('.')
  );

interface RenameOp {
  rowIndex: number;
  newName: string;
  /** Optionally typed into the renamed row's override entry afterwards. */
  postOverride: string | null;
}

interface RunScenario {
  modality: string;
  initialLabels: string[];
  /** Per initial row: the override text to type, or null to leave untouched. */
  typedOverrides: Array<string | null>;
  renames: RenameOp[];
  /** How many of the five listed samples to select, 1..Sample_Limit. */
  sampleCount: number;
}

/**
 * A full tune-loop scenario: 1-3 distinct label rows, arbitrary override
 * entries typed under a grounded-sam selection, then label renames
 * (leaving stale override keys behind — keys outside the effective
 * Label_Set, which must drop), optional overrides re-typed for the
 * renamed rows, and 1..5 samples selected for the run.
 */
const runScenarioArb: fc.Arbitrary<RunScenario> = fc
  .tuple(
    fc.constantFrom('Segmentation', 'ObjectDetection'),
    fc.uniqueArray(labelArb, { minLength: 1, maxLength: 5 })
  )
  .chain(([modality, pool]) =>
    fc
      .record({
        labelCount: fc.integer({ min: 1, max: Math.min(3, pool.length) }),
        overrideSlots: fc.array(fc.option(overrideValueArb, { nil: null }), {
          minLength: 3,
          maxLength: 3,
        }),
        renameRows: fc.uniqueArray(fc.integer({ min: 0, max: 2 }), {
          maxLength: 2,
        }),
        postRenameSlots: fc.array(fc.option(overrideValueArb, { nil: null }), {
          minLength: 2,
          maxLength: 2,
        }),
        sampleCount: fc.integer({ min: 1, max: SAMPLE_LIMIT }),
      })
      .map(
        ({
          labelCount,
          overrideSlots,
          renameRows,
          postRenameSlots,
          sampleCount,
        }) => {
          const initialLabels = pool.slice(0, labelCount);
          // Rename targets come from the unused remainder of the distinct
          // pool, so the label set stays distinct after every rename.
          const renameTargets = pool.slice(labelCount);
          const renames: RenameOp[] = [];
          for (const rowIndex of renameRows) {
            if (
              rowIndex < labelCount &&
              renames.length < renameTargets.length
            ) {
              renames.push({
                rowIndex,
                newName: renameTargets[renames.length],
                postOverride: postRenameSlots[renames.length] ?? null,
              });
            }
          }
          return {
            modality,
            initialLabels,
            typedOverrides: initialLabels.map(
              (_, i) => overrideSlots[i] ?? null
            ),
            renames,
            sampleCount,
          };
        }
      )
  );

describe("Feature: grounded-sam-prompt-tuning-preview, Property 3: A started grounded-sam run's request carries exactly the job's pruned prompts and no llm-only fields", () => {
  /**
   * *For any* guardrail-clean Label_Set and Prompt_Override entry states,
   * the started run's request body carries `model: 'grounded-sam'`, the
   * wizard's modality and Label_Set, the selected samples, and a
   * `prompt_overrides` map equal to exactly the entries non-empty after
   * trimming whose label is in the effective Label_Set (values
   * character-for-character, the key omitted entirely when none survive),
   * with no `detection_prompt`, `few_shot`, `downscale_max_edge`, or
   * `token_budget` key.
   *
   * **Validates: Requirements 2.1, 7.1**
   */
  it('sends exactly the pruned overrides of the effective Label_Set, raw, key omitted when none survive, and none of the llm-only fields', async () => {
    await fc.assert(
      fc.asyncProperty(runScenarioArb, async (scenario) => {
        cleanup();
        window.localStorage.clear();
        primeMocks([]);

        const { container } = await renderToDdaSetup(scenario.modality);

        // Label rows, as typed (pre-trimmed by generation).
        fireEvent.change(labelRowInput(container, 0), {
          target: { value: scenario.initialLabels[0] },
        });
        for (let i = 1; i < scenario.initialLabels.length; i += 1) {
          fireEvent.click(screen.getByRole('button', { name: 'Add label' }));
          fireEvent.change(labelRowInput(container, i), {
            target: { value: scenario.initialLabels[i] },
          });
        }

        enableAutoLabel(container);
        selectAutoLabelModel(container, 'grounded-sam');

        // The Preview_Panel mounts under grounded-sam (the widened render
        // gate) and its listing settles on the five-image grid.
        await screen.findByTestId('prompt-tuning-preview');
        await screen.findByTestId('preview-sample-grid');

        // Type the override entries, tracking what the wizard was handed:
        // the pruning oracle keys by the label name the entry belonged to
        // at typing time (Requirement 2.1's "whose label is in the
        // effective Label_Set" is judged when the run starts).
        const typedState = new Map<string, string>();
        scenario.initialLabels.forEach((label, i) => {
          const value = scenario.typedOverrides[i];
          if (value !== null) {
            fireEvent.change(overrideInput(container, label), {
              target: { value },
            });
            typedState.set(label, value);
          }
        });

        // Rename label rows after their overrides were typed: the old
        // name's entry becomes stale (a key outside the effective
        // Label_Set — never transmitted), and an override optionally
        // re-typed under the new name participates normally.
        const finalLabels = [...scenario.initialLabels];
        for (const { rowIndex, newName, postOverride } of scenario.renames) {
          fireEvent.change(labelRowInput(container, rowIndex), {
            target: { value: newName },
          });
          finalLabels[rowIndex] = newName;
          if (postOverride !== null) {
            fireEvent.change(overrideInput(container, newName), {
              target: { value: postOverride },
            });
            typedState.set(newName, postOverride);
          }
        }

        // Select the run's samples and start it.
        const chosen = LISTED_KEYS.slice(0, scenario.sampleCount);
        for (const key of chosen) fireEvent.click(sampleCheckbox(key));

        await act(async () => {
          fireEvent.click(runButton());
        });

        await waitFor(() =>
          expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1)
        );
        const body = apiMocks.startPreviewRun.mock.calls[0][0] as Record<
          string,
          unknown
        >;

        // Pruning oracle (Requirement 2.1 restated): exactly the entries
        // non-empty after trimming whose label is in the effective
        // Label_Set, each RAW value character-for-character; the
        // `prompt_overrides` key omitted entirely when none survive (the
        // shipped handleStartRun shape). Full-shape equality also proves
        // no llm-only field — and nothing else — rides along.
        const surviving = finalLabels
          .filter((label) => (typedState.get(label) ?? '').trim() !== '')
          .map(
            (label) =>
              [label, typedState.get(label) as string] as [string, string]
          );
        expect(body).toEqual({
          usecase_id: 'uc-1',
          dataset_prefix: DATASET_PREFIX,
          model: 'grounded-sam',
          task_type: scenario.modality,
          label_set: finalLabels,
          sample_images: chosen,
          ...(surviving.length > 0
            ? { prompt_overrides: Object.fromEntries(surviving) }
            : {}),
        });
        // The llm-only fields, individually pinned absent (Requirement
        // 2.1; the tune loop sends each run's own entries, Req 7.1).
        expect(body).not.toHaveProperty('detection_prompt');
        expect(body).not.toHaveProperty('few_shot');
        expect(body).not.toHaveProperty('downscale_max_edge');
        expect(body).not.toHaveProperty('token_budget');

        // Let the (already-Completed) run's poll loop settle before the
        // next fc run unmounts this wizard.
        await waitFor(() => expect(runButton()).toBeEnabled());
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});
