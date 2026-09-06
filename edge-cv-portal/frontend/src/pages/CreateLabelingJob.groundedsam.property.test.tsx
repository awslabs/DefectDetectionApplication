/**
 * Property-based tests for the Grounded-SAM wizard surface
 * (grounded-sam-autolabel task 4.5), following the render-per-run walk of
 * `CreateLabelingJob.modelpicker.property.test.tsx`: every fast-check run
 * mounts `CreateLabelingJob` fresh, drives it to the DDA labeling-setup
 * step, and asserts against oracles that restate the specified semantics
 * independently of the implementation.
 *
 * **Feature: grounded-sam-autolabel, Property 1: The picker offers the
 * pre-feature options plus exactly the Grounded-SAM entry for its
 * modalities**
 *
 * *For any* wizard modality (Classification, Segmentation,
 * ObjectDetection) and *any* model catalog (options mixing
 * `image_input: true` / `false` / absent), the Auto_Label_Picker's option
 * structure SHALL equal the pre-feature oracle (SAM entry per its matrix,
 * capability-filtered decorated Bedrock and LLM groups) with exactly one
 * addition: the static entry
 * `{label: 'Grounded-SAM (text-prompted)', value: 'grounded-sam'}`
 * present immediately after the SAM entry when the modality is
 * Segmentation or ObjectDetection, and absent when the modality is
 * Classification.
 *
 * **Validates: Requirements 1.1, 1.2, 7.2**
 *
 * **Feature: grounded-sam-autolabel, Property 2: The submitted job
 * carries exactly the surviving overrides, raw, or no key at all**
 *
 * *For any* set of label rows and *any* Prompt_Override entry state
 * (values mixing empty, whitespace-only, unicode, boundary-length
 * strings, and entries keyed by labels since renamed), submitting a
 * `grounded-sam` job SHALL send `auto_label.prompt_overrides` equal to
 * exactly the entries that are non-empty after trimming and whose label
 * is in the effective Label_Set, each value character-for-character as
 * entered, with the key omitted entirely when no entry survives; and
 * *for any* non-`grounded-sam` submission the payload SHALL carry no
 * `prompt_overrides` key.
 *
 * **Validates: Requirements 2.3, 2.8**
 *
 * Assertion surfaces, per the modelpicker precedent: the barrel's
 * `Select` is wrapped with a pass-through spy so Property 1 can
 * deep-compare the exact `options` prop the wizard constructed
 * (`filteringTags` included — Cloudscape renders them only while a
 * matching search is typed, so no dropdown read could assert them), while
 * the real Cloudscape Select still renders and the dropdown reads stay
 * honest. Property 2 captures the mocked `apiService.createLabelingJob`
 * payload and compares it against a pruning oracle that tracks, in the
 * test, what was typed into which override entry and which label rows
 * were renamed afterwards.
 *
 * Generator domain notes (smart constraints, not oracle weakening):
 * - Label rows are generated pre-trimmed, distinct, and within the
 *   wizard's label constraints (1-10 labels of at most 64 characters);
 *   rows violating them never reach submission (the Next click is
 *   rejected), so they are outside this property's domain.
 * - Label names shadowing `Object.prototype` members (`toString`,
 *   `__proto__`, ...) are excluded: the wizard's established per-label
 *   plain-object state idiom (shared with the pre-existing
 *   skip-verification `perLabelPrompts`) reads such keys through the
 *   prototype. Prototype-key robustness is the draft module's covered
 *   surface (task 4.4), not the wizard walk's.
 * - Override values stay within the 256-character limit (including
 *   boundary lengths 255 and 256): longer values reject the wizard step
 *   by design (Requirement 2.6), so no submission exists to observe.
 * - No CR/LF in labels or override values: both are single-line `<input>`
 *   elements, whose HTML value sanitization strips newlines before the
 *   wizard ever sees them — not an enterable character.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
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

const { apiMocks, navigateMock, recordedSelectProps } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    listWorkteams: vi.fn(),
    createLabelingJob: vi.fn(),
  },
  navigateMock: vi.fn(),
  /** Latest props each rendered Select received, keyed by placeholder. */
  recordedSelectProps: new Map<string, unknown>(),
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
// real component (the modelpicker.property precedent).
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
// Fixtures and generators
// ---------------------------------------------------------------------------

/** One Model_Catalog entry as `getBedrockModels()` returns it. */
interface CatalogModel {
  id: string;
  label: string;
  image_limit?: number;
  token_limit?: number;
  image_input?: boolean;
}

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
}

beforeEach(() => {
  window.localStorage.clear();
  primeMocks([]);
});

/** Catalog ids: non-empty, unicode allowed. */
const idArb = fc.string({ unit: 'grapheme', minLength: 1, maxLength: 20 });

/** A Model_Option with the Image_Input_Capability tri-state. */
const modelArb: fc.Arbitrary<CatalogModel> = fc.record(
  {
    id: idArb,
    label: fc.string({ unit: 'grapheme', maxLength: 20 }),
    image_input: fc.boolean(),
    image_limit: fc.integer({ min: 1, max: 40 }),
    token_limit: fc.integer({ min: 100, max: 20000 }),
  },
  { requiredKeys: ['id', 'label'] }
);

/** A Model_Catalog: any mix of capabilities, any order, duplicates allowed. */
const catalogArb = fc.array(modelArb, { maxLength: 8 });

const modalityArb = fc.constantFrom(
  'Classification',
  'Segmentation',
  'ObjectDetection'
);

// ---------------------------------------------------------------------------
// Oracles (restate the specified semantics independently)
// ---------------------------------------------------------------------------

/**
 * The pre-feature capability filter restated
 * (llm-model-picker-search-and-image-filter Requirements 2.1/2.2, kept
 * unchanged by grounded-sam-autolabel Requirement 7.2): a Model_Option is
 * excluded exactly when positively known text-only.
 */
function imageCapableOracle(m: { image_input?: boolean }): boolean {
  return m.image_input !== false;
}

/**
 * The model/modality matrix restated: the three pre-feature families
 * exactly as before (Requirement 7.2), plus the Grounded_SAM_Family for
 * Segmentation and ObjectDetection only (Requirements 1.1, 1.2).
 */
const ORACLE_MODALITIES: Record<
  'sam' | 'groundedSam' | 'bedrock' | 'llm',
  string[]
> = {
  sam: ['Segmentation', 'ObjectDetection'],
  groundedSam: ['Segmentation', 'ObjectDetection'],
  bedrock: ['Classification', 'ObjectDetection'],
  llm: ['Classification', 'Segmentation', 'ObjectDetection'],
};

/** The Grounded_SAM_Entry, byte-for-byte (Requirement 1.1). */
const GROUNDED_SAM_ENTRY = {
  label: 'Grounded-SAM (text-prompted)',
  value: 'grounded-sam',
};

interface FamilyOption {
  label: string;
  value: string;
  filteringTags: string[];
}

/**
 * The exact option structure the wizard must hand the auto-label Select:
 * the pre-feature oracle of the modelpicker.property suite with exactly
 * one addition — the static Grounded-SAM entry immediately after the SAM
 * entry for its modalities, absent for Classification.
 */
function expectedAutoLabelOptions(modality: string, catalog: CatalogModel[]) {
  const capable = catalog.filter(imageCapableOracle);
  const bedrockFamily: FamilyOption[] = ORACLE_MODALITIES.bedrock.includes(
    modality
  )
    ? capable.map((m) => ({
        label: `Bedrock: ${m.label}`,
        value: `bedrock:${m.id}`,
        filteringTags: [m.id],
      }))
    : [];
  const llmFamily: FamilyOption[] = ORACLE_MODALITIES.llm.includes(modality)
    ? capable.map((m) => ({
        label: `${m.label} (prompt-guided)`,
        value: `llm:${m.id}`,
        filteringTags: [m.id],
      }))
    : [];
  const samEntries = ORACLE_MODALITIES.sam.includes(modality)
    ? [{ label: 'Segment Anything (SAM)', value: 'sam' }]
    : [];
  const groundedSamEntries = ORACLE_MODALITIES.groundedSam.includes(modality)
    ? [{ ...GROUNDED_SAM_ENTRY }]
    : [];
  const staticEntries = [...samEntries, ...groundedSamEntries];
  const grouped: unknown[] = [
    ...staticEntries,
    ...(bedrockFamily.length > 0
      ? [{ label: 'Bedrock vision models', options: bedrockFamily }]
      : []),
    ...(llmFamily.length > 0
      ? [{ label: 'Prompt-guided LLM models', options: llmFamily }]
      : []),
  ];
  const flat: Array<[string, string]> = [
    ...staticEntries.map((o) => [o.value, o.label] as [string, string]),
    ...bedrockFamily.map((o) => [o.value, o.label] as [string, string]),
    ...llmFamily.map((o) => [o.value, o.label] as [string, string]),
  ];
  const groupLabels = [
    ...(bedrockFamily.length > 0 ? ['Bedrock vision models'] : []),
    ...(llmFamily.length > 0 ? ['Prompt-guided LLM models'] : []),
  ];
  return { grouped, flat, groupLabels };
}

// ---------------------------------------------------------------------------
// Wizard walk helpers (the modelpicker.property / sizing-suite walk)
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
    { target: { value: 'grounded-sam-property-job' } }
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

/** Select the labeling team (required for a non-admin submission). */
async function selectTeam(container: HTMLElement) {
  const teamSelect = createWrapper(container).findAllSelects()[0];
  await waitFor(() => {
    expect(teamSelect.findTrigger().getElement()).not.toBeDisabled();
  });
  teamSelect.openDropdown();
  teamSelect.selectOptionByValue('t-1');
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

/** The open dropdown's (value, label) entries, in rendered order. */
function displayedOptionEntries(
  container: HTMLElement
): Array<[string, string]> {
  return modelSelect(container)
    .findDropdown()
    .findOptions()
    .map((option) => [
      option.getElement().getAttribute('data-value') ?? '',
      option.findLabel()!.getElement().textContent ?? '',
    ]);
}

/** The open dropdown's group header texts, in rendered order. */
function displayedGroupLabels(container: HTMLElement): string[] {
  return modelSelect(container)
    .findDropdown()
    .findGroups()
    .map((group) => group.getElement().textContent ?? '');
}

/**
 * Exact-attribute input lookup: generated labels are arbitrary unicode,
 * and Testing Library's accessible-name matching normalizes whitespace,
 * so both the label-row inputs (`aria-label="Label {n}"`) and the
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

// ---------------------------------------------------------------------------
// Feature: grounded-sam-autolabel, Property 1: The picker offers the
// pre-feature options plus exactly the Grounded-SAM entry for its
// modalities
// ---------------------------------------------------------------------------

describe('Feature: grounded-sam-autolabel, Property 1: The picker offers the pre-feature options plus exactly the Grounded-SAM entry for its modalities', () => {
  it('offers the pre-feature structure plus the static Grounded-SAM entry after SAM for Segmentation/ObjectDetection, absent for Classification', async () => {
    // The restated matrices and the shipped ones must agree before use.
    expect(SAM_MODALITIES).toEqual(ORACLE_MODALITIES.sam);
    expect(GROUNDED_SAM_MODALITIES).toEqual(ORACLE_MODALITIES.groundedSam);
    expect(BEDROCK_MODALITIES).toEqual(ORACLE_MODALITIES.bedrock);
    expect(LLM_MODALITIES).toEqual(ORACLE_MODALITIES.llm);

    await fc.assert(
      fc.asyncProperty(modalityArb, catalogArb, async (modality, catalog) => {
        cleanup();
        window.localStorage.clear();
        primeMocks(catalog);

        const { container } = await renderToDdaSetup(modality);
        enableAutoLabel(container);

        const expected = expectedAutoLabelOptions(modality, catalog);

        // The real options prop the wizard constructed: the pre-feature
        // families byte-for-byte (SAM entry, capability filter, catalog
        // order, `bedrock:<id>`/`llm:<id>` decoration, filteringTags,
        // group headers — Requirement 7.2) with exactly the static
        // Grounded-SAM entry spliced immediately after SAM for
        // Segmentation/ObjectDetection and absent for Classification
        // (Requirements 1.1, 1.2). Waited on: the catalog resolves
        // asynchronously after mount.
        await waitFor(() => {
          expect(autoLabelSelectProps()?.options).toEqual(expected.grouped);
        });

        // The type-to-search surface stays Cloudscape's built-in
        // filtering, unchanged by the new entry (Requirement 7.2).
        expect(autoLabelSelectProps()?.filteringType).toBe('auto');

        if (expected.flat.length > 0) {
          // What the Job_Creator is actually offered: the same entries,
          // in the same order, wearing the same labels — the
          // Grounded-SAM entry directly beside SAM.
          modelSelect(container).openDropdown();
          expect(displayedOptionEntries(container)).toEqual(expected.flat);
          expect(displayedGroupLabels(container)).toEqual(
            expected.groupLabels
          );
        } else {
          // Nothing to offer (Classification with both families empty —
          // the static entries never render there): the pre-feature
          // disabled Select.
          expect(modelSelect(container).isDisabled()).toBe(true);
        }
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});

// ---------------------------------------------------------------------------
// Feature: grounded-sam-autolabel, Property 2: The submitted job carries
// exactly the surviving overrides, raw, or no key at all
// ---------------------------------------------------------------------------

/**
 * Label rows: pre-trimmed distinct unicode names within the wizard's
 * label constraints; `Object.prototype` member names excluded (see the
 * header's generator domain notes).
 */
const labelArb = fc
  .string({ unit: 'grapheme', minLength: 1, maxLength: 8 })
  .filter(
    (s) =>
      s.trim() === s &&
      s.length > 0 &&
      s.length <= 64 &&
      !/[\r\n]/.test(s) &&
      !(s in Object.prototype)
  );

/**
 * Whitespace-only strings (pruned by trimming at submit). No CR/LF: the
 * override entries are single-line `<input>` elements, whose HTML value
 * sanitization strips newlines — a newline is not an enterable character,
 * so it is outside the property's domain (space, tab and NBSP survive the
 * input and are trimmed by the submit pruning).
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
 * boundary lengths 255/256 — always within the 256-character limit so the
 * wizard step accepts the submission (Requirement 2.6 rejects longer).
 */
const overrideValueArb: fc.Arbitrary<string> = fc
  .oneof(
    { weight: 4, arbitrary: fc.string({ unit: 'grapheme', minLength: 1, maxLength: 12 }) },
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
  .filter((v) => v.length <= MAX_PROMPT_OVERRIDE_LENGTH && !/[\r\n]/.test(v));

/** The fixed catalog model backing the `bedrock:` non-grounded-sam prong. */
const CATALOG_MODEL: CatalogModel = { id: 'm-1', label: 'Nova' };

interface RenameOp {
  rowIndex: number;
  newName: string;
  /** Optionally typed into the renamed row's override entry afterwards. */
  postOverride: string | null;
}

interface OverrideScenario {
  modality: string;
  initialLabels: string[];
  /** Per initial row: the override text to type, or null to leave untouched. */
  typedOverrides: Array<string | null>;
  renames: RenameOp[];
  submitModel: string;
}

/**
 * A full wizard scenario: 1-3 distinct label rows, arbitrary override
 * entries typed under a grounded-sam selection, then label renames
 * (leaving stale override keys behind), optional overrides re-typed for
 * the renamed rows, and the family actually submitted — grounded-sam
 * most of the time, `sam`/`bedrock:` otherwise (with the typed override
 * state still in the wizard, proving it never leaks into other
 * families' payloads).
 */
const scenarioArb: fc.Arbitrary<OverrideScenario> = fc
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
        submitRoll: fc.integer({ min: 0, max: 9 }),
      })
      .map(
        ({
          labelCount,
          overrideSlots,
          renameRows,
          postRenameSlots,
          submitRoll,
        }) => {
          const initialLabels = pool.slice(0, labelCount);
          // Rename targets come from the unused remainder of the distinct
          // pool, so the label set stays distinct after every rename.
          const renameTargets = pool.slice(labelCount);
          const renames: RenameOp[] = [];
          for (const rowIndex of renameRows) {
            if (rowIndex < labelCount && renames.length < renameTargets.length) {
              renames.push({
                rowIndex,
                newName: renameTargets[renames.length],
                postOverride: postRenameSlots[renames.length] ?? null,
              });
            }
          }
          const submitModel =
            submitRoll < 7
              ? 'grounded-sam'
              : submitRoll === 9 && modality === 'ObjectDetection'
                ? `bedrock:${CATALOG_MODEL.id}`
                : 'sam';
          return {
            modality,
            initialLabels,
            typedOverrides: initialLabels.map(
              (_, i) => overrideSlots[i] ?? null
            ),
            renames,
            submitModel,
          };
        }
      )
  );

describe('Feature: grounded-sam-autolabel, Property 2: The submitted job carries exactly the surviving overrides, raw, or no key at all', () => {
  it('sends exactly the non-blank overrides of the submitted Label_Set, raw, key omitted when none survive, and never for other families', async () => {
    await fc.assert(
      fc.asyncProperty(scenarioArb, async (scenario) => {
        cleanup();
        window.localStorage.clear();
        primeMocks([CATALOG_MODEL]);

        const { container } = await renderToDdaSetup(scenario.modality);
        await selectTeam(container);

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

        // Type the override entries, tracking what the wizard was handed:
        // the pruning oracle keys by the label name the entry belonged to
        // at typing time (Requirement 2.3's "whose label is in the
        // submitted Label_Set" is judged at submit).
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
        // name's entry becomes stale (never transmitted), and an override
        // optionally re-typed under the new name participates normally.
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

        // Non-grounded-sam submissions switch family AFTER overrides were
        // typed, so the retained override state is proven never to leak
        // into another family's payload (Requirement 2.8).
        if (scenario.submitModel !== 'grounded-sam') {
          selectAutoLabelModel(container, scenario.submitModel);
        }

        clickNext();
        fireEvent.click(
          await screen.findByRole('button', { name: 'Create Job' })
        );
        await waitFor(() =>
          expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
        );
        const payload = apiMocks.createLabelingJob.mock.calls[0][0] as {
          label_set: string[];
          auto_label?: Record<string, unknown>;
        };

        // The submitted Label_Set is the final rows, in row order.
        expect(payload.label_set).toEqual(finalLabels);

        if (scenario.submitModel === 'grounded-sam') {
          // Pruning oracle (Requirement 2.3 restated): exactly the
          // entries non-empty after trimming whose label is in the
          // submitted Label_Set, each RAW value character-for-character;
          // the key omitted entirely when none survive. Full-shape
          // equality also proves no other key rides along.
          const surviving = finalLabels
            .filter((label) => (typedState.get(label) ?? '').trim() !== '')
            .map(
              (label) =>
                [label, typedState.get(label) as string] as [string, string]
            );
          expect(payload.auto_label).toEqual({
            enabled: true,
            model: 'grounded-sam',
            ...(surviving.length > 0
              ? { prompt_overrides: Object.fromEntries(surviving) }
              : {}),
          });
        } else {
          // Any other family: the payload carries no `prompt_overrides`
          // key at all — byte-identical to a pre-feature submission
          // (Requirement 2.8).
          expect(payload.auto_label).toEqual({
            enabled: true,
            model: scenario.submitModel,
          });
        }
        expect(payload).not.toHaveProperty('prompt_overrides');
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});
