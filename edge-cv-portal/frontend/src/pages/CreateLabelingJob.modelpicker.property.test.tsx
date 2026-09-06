/**
 * Property-based test for the Auto_Label_Picker family construction
 * (llm-model-picker-search-and-image-filter task 2.3).
 *
 * **Feature: llm-model-picker-search-and-image-filter, Property 3: The
 * auto-label families offer exactly the not-known-text-only catalog,
 * decorated and searchable as before**
 *
 * *For any* Model_Catalog (options mixing `image_input: true`,
 * `image_input: false`, and field-absent, in any order, with any ids and
 * labels), the Bedrock_Auto_Label_Family and the LLM_Auto_Label_Family
 * option lists built by the wizard SHALL each contain exactly the
 * Model_Options for which `isImageCapableModel` holds
 * (`image_input !== false`) — so every field-absent option is included and
 * every `image_input: false` option is excluded — in the catalog's order,
 * with each Bedrock entry carrying value `bedrock:<id>` and label
 * `Bedrock: <label>`, each LLM entry carrying value `llm:<id>` and label
 * `<label> (prompt-guided)`, and each entry carrying `filteringTags`
 * equal to `[<id>]`.
 *
 * **Validates: Requirements 2.1, 2.2, 2.5, 3.2, 4.7**
 *
 * Approach — noting the choice between the two repo precedents for
 * structural option assertions:
 *
 * - `workflows/aravisCameraReference.property.test.ts` (Property 7) tests
 *   the exported pure picker filter against an in-test oracle; the first
 *   `it` below does the same for the exported `isImageCapableModel`.
 * - `PromptTuningPreview.property.test.tsx` (Property 14) mounts
 *   `CreateLabelingJob` once per fast-check run at `numRuns: 100` and
 *   asserts on the real auto-label Select's dropdown; the second `it`
 *   follows that walk with a generated catalog behind a mocked
 *   `getBedrockModels` and reads the rendered dropdown's (value, label)
 *   entries and group headers.
 *
 * One piece of the contract is not DOM-observable: Cloudscape renders
 * `filteringTags` only while a matching Search_Text is typed (they are
 * match-only metadata, `internal/components/option/option-parts.js`), so
 * no dropdown read can assert `filteringTags === [<id>]` structurally.
 * Rather than mirror the component's mapping in the test (a
 * reimplementation would validate nothing), the barrel's `Select` is
 * wrapped with a pass-through spy: the *real* Cloudscape Select still
 * renders everything (the DOM assertions stay honest, and the Cloudscape
 * test-utils keep working), while the spy records the exact `options`
 * prop the wizard constructed — which is then deep-compared against the
 * oracle, `filteringTags` included.
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
  isImageCapableModel,
  LLM_MODALITIES,
  SAM_MODALITIES,
} from './CreateLabelingJob';

const { apiMocks, navigateMock, recordedSelectProps } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    listWorkteams: vi.fn(),
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
// real component. Nothing in the pages under test passes a `ref` to
// `Select`, so a plain function wrapper is transparent.
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
}

beforeEach(() => {
  window.localStorage.clear();
  primeMocks([]);
});

/** Catalog ids: non-empty, unicode allowed. */
const idArb = fc.string({ unit: 'grapheme', minLength: 1, maxLength: 20 });

/**
 * A Model_Option with the Image_Input_Capability tri-state: `image_input`
 * present-true, present-false, or absent (Unknown_Capability), alongside
 * the optional pre-feature annotation fields.
 */
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
 * Requirements 2.1/2.2 restated: a Model_Option is excluded exactly when
 * its Image_Input_Capability is positively Text_Only; Image_Capable and
 * Unknown_Capability (field absent) are always offered.
 */
function imageCapableOracle(m: { image_input?: boolean }): boolean {
  if (m.image_input === true) return true; // Image_Capable
  if (m.image_input === false) return false; // Text_Only — the one exclusion
  return true; // Unknown_Capability never shrinks the list
}

/**
 * The pre-feature modality compatibility matrix, restated from the
 * dda-data-labeling / llm-auto-labeling acceptance criteria (Requirement
 * 4.7 keeps it unchanged).
 */
const ORACLE_MODALITIES: Record<'sam' | 'bedrock' | 'llm', string[]> = {
  sam: ['Segmentation', 'ObjectDetection'],
  bedrock: ['Classification', 'ObjectDetection'],
  llm: ['Classification', 'Segmentation', 'ObjectDetection'],
};

interface FamilyOption {
  label: string;
  value: string;
  filteringTags: string[];
}

/** The exact option structure the wizard must hand the auto-label Select. */
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
  const grouped: unknown[] = [
    ...samEntries,
    ...(bedrockFamily.length > 0
      ? [{ label: 'Bedrock vision models', options: bedrockFamily }]
      : []),
    ...(llmFamily.length > 0
      ? [{ label: 'Prompt-guided LLM models', options: llmFamily }]
      : []),
  ];
  const flat: Array<[string, string]> = [
    ...samEntries.map((o) => [o.value, o.label] as [string, string]),
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
// Wizard helpers (the Property 14 walk from PromptTuningPreview.property)
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
    { target: { value: 'model-picker-property-job' } }
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

/** The auto-label model select (the team select is the step's first select). */
const modelSelect = (container: HTMLElement) =>
  createWrapper(container).findAllSelects()[1];

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

// ---------------------------------------------------------------------------
// Property 3
// ---------------------------------------------------------------------------

describe('Feature: llm-model-picker-search-and-image-filter, Property 3: The auto-label families offer exactly the not-known-text-only catalog, decorated and searchable as before', () => {
  it('isImageCapableModel excludes exactly the positively-known text-only models', () => {
    fc.assert(
      fc.property(catalogArb, (models) => {
        // Per-entry agreement with the tri-state oracle (Req 2.1, 2.2).
        for (const model of models) {
          expect(isImageCapableModel(model)).toBe(imageCapableOracle(model));
        }
        // List-level: filtering keeps exactly the oracle's set, in order.
        expect(models.filter(isImageCapableModel)).toEqual(
          models.filter(imageCapableOracle)
        );
      }),
      { numRuns: 100 }
    );
  });

  it('builds both families from exactly the image-capable catalog, decorated and search-tagged', async () => {
    // The restated matrix and the shipped one must agree before use.
    expect(SAM_MODALITIES).toEqual(ORACLE_MODALITIES.sam);
    expect(BEDROCK_MODALITIES).toEqual(ORACLE_MODALITIES.bedrock);
    expect(LLM_MODALITIES).toEqual(ORACLE_MODALITIES.llm);

    await fc.assert(
      fc.asyncProperty(modalityArb, catalogArb, async (modality, catalog) => {
        cleanup();
        primeMocks(catalog);

        const { container } = await renderToDdaSetup(modality);
        enableAutoLabel(container);

        const expected = expectedAutoLabelOptions(modality, catalog);

        // The real options prop the wizard constructed: family membership
        // (`image_input !== false` only), catalog order, `bedrock:<id>` /
        // `Bedrock: <label>` and `llm:<id>` / `<label> (prompt-guided)`
        // decoration, `filteringTags: [<id>]` on every family entry, and
        // the pre-feature group headers (Req 2.1, 2.2, 2.5, 3.2, 4.7).
        // Waited on: the catalog resolves asynchronously after mount.
        await waitFor(() => {
          expect(autoLabelSelectProps()?.options).toEqual(expected.grouped);
        });

        // The type-to-search surface rides Cloudscape's built-in
        // filtering, which is what reads filteringTags (Req 3.2).
        expect(autoLabelSelectProps()?.filteringType).toBe('auto');

        if (expected.flat.length > 0) {
          // What the Job_Creator is actually offered: the same entries,
          // in the same order, wearing the same decorated labels.
          modelSelect(container).openDropdown();
          expect(displayedOptionEntries(container)).toEqual(expected.flat);
          expect(displayedGroupLabels(container)).toEqual(
            expected.groupLabels
          );
        } else {
          // Nothing to offer (no sam for this modality and both families
          // empty): the pre-feature disabled Select. The all-excluded
          // free-text affordance is task 2.4's example suite.
          expect(modelSelect(container).isDisabled()).toBe(true);
        }
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});
