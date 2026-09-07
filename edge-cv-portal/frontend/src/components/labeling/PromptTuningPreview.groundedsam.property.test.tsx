/**
 * Property-based test for the Prompt_Tuning_Preview's grounded-sam run
 * control (grounded-sam-prompt-tuning-preview task 2.6, design Property 2).
 *
 * The component is rendered directly with `model='grounded-sam'` and a
 * generated Label_Set + Prompt_Override map; the listing and start APIs
 * are mocked following the shipped `PromptTuningPreview.property.test.tsx`
 * scaffolding (a `vi.hoisted` proxy over the API service and a stubbed
 * global `fetch`). No fake timers are needed: the mocked run completes on
 * the poll loop's first iteration.
 *
 * The oracle is the shared `promptOverrideGuardrails` module — a label
 * offends exactly when `ALIGNMENT_BREAKING_PATTERN` matches its
 * `effectivePrompt` — walked in Label_Set order with the corrective
 * wording of `promptGuardrailMessage`, the same exports the panel builds
 * its violations from, so the test pins that the run control actually
 * walks every label, blocks the request, and lists every offender.
 *
 * Modality (Segmentation/ObjectDetection), Label_Set validity and the
 * 1..5 sample selection are held valid by construction so the
 * Prompt_Guardrail is the only variable under test.
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
import * as fc from 'fast-check';
import type { ComponentProps } from 'react';

import PromptTuningPreview, { SAMPLE_LIMIT } from './PromptTuningPreview';
import type { LabelingModality } from './AnnotationCanvas';
import {
  ALIGNMENT_BREAKING_PATTERN,
  effectivePrompt,
  promptGuardrailMessage,
} from '../../pages/promptOverrideGuardrails';

const { apiMocks, fetchMock } = vi.hoisted(() => ({
  apiMocks: {
    getImagePreview: vi.fn(),
    startPreviewRun: vi.fn(),
    getPreviewRun: vi.fn(),
  },
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

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PREFIX = 'images/';
/** Five listable dataset objects — exactly the Sample_Limit. */
const LISTED_KEYS = Array.from(
  { length: SAMPLE_LIMIT },
  (_, i) => `${PREFIX}img-${i}.jpg`
);

const listing = (keys: string[]) => ({
  prefix: PREFIX,
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

/** Reset every mock to a benign default. Called at the top of each fc run. */
function primeMocks() {
  vi.clearAllMocks();
  apiMocks.getImagePreview.mockResolvedValue(listing(LISTED_KEYS));
  apiMocks.startPreviewRun.mockResolvedValue({
    run_id: 'run-1',
    sample_count: 1,
    status: 'Running',
  });
  // A run that is already Completed terminates the poll loop on its first
  // iteration, so the guardrail-clean branch needs no fake timers.
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
  primeMocks();
  vi.stubGlobal('fetch', fetchMock);
});

type PreviewProps = ComponentProps<typeof PromptTuningPreview>;

const previewProps = (overrides: Partial<PreviewProps> = {}): PreviewProps => ({
  usecaseId: 'uc-1',
  datasetPrefix: PREFIX,
  model: 'grounded-sam',
  // The grounded-sam family has no Detection_Prompt input: an empty value
  // must trip no rule under this model (the family dispatch under test).
  detectionPrompt: '',
  taskType: 'Segmentation',
  labelSet: ['scratch-0'],
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

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/**
 * One Label_Set row. Indices keep rows distinct; the kinds cover plain
 * ASCII, period-bearing label names (offenders whenever no override
 * survives trimming) and period-free unicode.
 */
const labelRowArb = (index: number) =>
  fc.constantFrom(
    `scratch-${index}`,
    `weld.seam-${index}`,
    `キズ${index}`,
    `défaut-${index}`
  );

/** 1..4 distinct rows — valid under the panel's label-set rules. */
const labelSetArb = fc
  .integer({ min: 1, max: 4 })
  .chain((count) =>
    fc
      .tuple(...Array.from({ length: count }, (_, i) => labelRowArb(i)))
      .map((rows) => [...rows])
  );

/**
 * One Prompt_Override entry state: absent, empty, whitespace-only,
 * period-bearing, other-punctuation, unicode (including the CJK full stop
 * `。`, which is NOT the Alignment_Breaking_Character), or a plain
 * survivor.
 */
const overrideEntryArb: fc.Arbitrary<string | undefined> = fc.oneof(
  fc.constant(undefined),
  fc.constant(''),
  fc.constantFrom('   ', ' \t ', '\n  '),
  fc.constantFrom(
    'gap between broken cookie pieces.',
    'crack. on surface',
    '.',
    ' ends with a period.'
  ),
  fc.constantFrom(
    'gap, between pieces!',
    'scratch; dent?',
    'crack (hairline) — thin'
  ),
  fc.constantFrom('ひび割れの領域', 'zone défectueuse', '裂缝区域。'),
  fc.constantFrom('gap between broken pieces', 'hairline crack')
);

interface Scenario {
  taskType: LabelingModality;
  labelSet: string[];
  sampleCount: number;
  overrides: Record<string, string>;
}

const scenarioArb: fc.Arbitrary<Scenario> = fc
  .record({
    taskType: fc.constantFrom<LabelingModality>(
      'Segmentation',
      'ObjectDetection'
    ),
    labelSet: labelSetArb,
    sampleCount: fc.integer({ min: 1, max: SAMPLE_LIMIT }),
    stray: fc.boolean(),
  })
  .chain(({ taskType, labelSet, sampleCount, stray }) =>
    fc.tuple(...labelSet.map(() => overrideEntryArb)).map((entries) => {
      const overrides: Record<string, string> = {};
      labelSet.forEach((label, i) => {
        const entry = entries[i];
        if (entry !== undefined) overrides[label] = entry;
      });
      // A period-bearing entry for a label OUTSIDE the Label_Set must
      // produce no violation: the guardrail walks the Label_Set only.
      if (stray) overrides['unrelated-label'] = 'stray. value';
      return { taskType, labelSet, sampleCount, overrides };
    })
  );

// ---------------------------------------------------------------------------
// Oracle
// ---------------------------------------------------------------------------

/**
 * Expected offender messages, straight from the shared exports: one per
 * Label_Set label (in Label_Set order) whose Effective_Prompt matches
 * `ALIGNMENT_BREAKING_PATTERN`, worded by `promptGuardrailMessage` with
 * the source carrying the period — the surviving override, or the
 * label-name fallback.
 */
function expectedOffenderMessages(
  labelSet: string[],
  overrides: Record<string, string>
): string[] {
  const messages: string[] = [];
  for (const label of labelSet) {
    const override = overrides[label];
    if (ALIGNMENT_BREAKING_PATTERN.test(effectivePrompt(label, override))) {
      const survives = typeof override === 'string' && override.trim() !== '';
      messages.push(
        promptGuardrailMessage({
          label,
          source: survives ? 'override' : 'label',
        })
      );
    }
  }
  return messages;
}

// ---------------------------------------------------------------------------
// Property 2 (task 2.6)
// ---------------------------------------------------------------------------

describe('Feature: grounded-sam-prompt-tuning-preview, Property 2: The run control starts a grounded-sam run iff the Prompt_Guardrail holds, listing every offender', () => {
  /**
   * *For any* Label_Set rows and Prompt_Override entry states (empty,
   * whitespace-only, period-bearing, other-punctuation, unicode) with a
   * valid sample selection, activating the run control under a
   * grounded-sam selection issues a start request exactly when every
   * label's Effective_Prompt is period-free; on violation the panel lists
   * one corrective error per offending label using the shared guardrail
   * wording, issues no request, and leaves the selection and wizard state
   * unchanged.
   *
   * **Validates: Requirements 2.3**
   */
  it('issues the start request exactly when every Effective_Prompt is period-free, otherwise lists every offender and sends nothing', async () => {
    await fc.assert(
      fc.asyncProperty(scenarioArb, async (scenario) => {
        const expected = expectedOffenderMessages(
          scenario.labelSet,
          scenario.overrides
        );

        cleanup();
        primeMocks();

        render(
          <PromptTuningPreview
            {...previewProps({
              taskType: scenario.taskType,
              labelSet: scenario.labelSet,
              promptOverrides: scenario.overrides,
            })}
          />
        );
        await waitFor(() =>
          expect(screen.getByTestId('preview-sample-grid')).toBeInTheDocument()
        );

        const chosen = LISTED_KEYS.slice(0, scenario.sampleCount);
        for (const key of chosen) fireEvent.click(sampleCheckbox(key));

        const selectionBefore = screen.getByTestId(
          'preview-selection-count'
        ).textContent;
        const checkedBefore = LISTED_KEYS.map(
          (key) => sampleCheckbox(key).checked
        );

        await act(async () => {
          fireEvent.click(runButton());
        });

        if (expected.length > 0) {
          // Violation: every offender listed exactly once, in Label_Set
          // order, with the shared wording — and nothing was sent.
          const messages = screen
            .getAllByTestId('preview-validation-error')
            .map((node) => node.textContent || '');
          expect(messages).toEqual(expected);
          expect(apiMocks.startPreviewRun).not.toHaveBeenCalled();

          // The selection and the rest of the surface are untouched.
          expect(
            screen.getByTestId('preview-selection-count').textContent
          ).toBe(selectionBefore);
          expect(
            LISTED_KEYS.map((key) => sampleCheckbox(key).checked)
          ).toEqual(checkedBefore);
          expect(
            screen.queryByTestId('preview-results')
          ).not.toBeInTheDocument();
          expect(
            screen.queryByTestId('preview-run-error')
          ).not.toBeInTheDocument();
          expect(runButton()).toBeEnabled();
        } else {
          // Guardrail holds: exactly one start request, carrying this
          // grounded-sam run over the selected samples.
          await waitFor(() =>
            expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1)
          );
          const body = apiMocks.startPreviewRun.mock.calls[0][0] as {
            model: string;
            task_type: string;
            label_set: string[];
            sample_images: string[];
          };
          expect(body.model).toBe('grounded-sam');
          expect(body.task_type).toBe(scenario.taskType);
          expect(body.label_set).toEqual(scenario.labelSet);
          expect(body.sample_images).toEqual(chosen);
          expect(
            screen.queryAllByTestId('preview-validation-error')
          ).toHaveLength(0);
        }
      }),
      { numRuns: 100 }
    );
  }, 600_000);
});
