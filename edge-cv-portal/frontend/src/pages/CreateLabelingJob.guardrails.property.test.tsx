/**
 * Property-based test for the grounded-sam Prompt_Guardrail on the
 * wizard's DDA labeling-setup step
 * (grounded-sam-prompt-guardrails-and-prelabel-retry task 4.5), following
 * the render-per-run walk of `CreateLabelingJob.groundedsam.property.test.tsx`:
 * every fast-check run mounts `CreateLabelingJob` fresh, drives it to the
 * setup step, types label rows and Prompt_Override entries, clicks Next,
 * and judges the outcome against an oracle restating the specified
 * semantics independently of the implementation.
 *
 * **Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
 * Property 1: Wizard accepts iff the guardrail holds**
 *
 * *For any* Label_Set rows (labels with and without periods), *any*
 * Prompt_Override entry state (values mixing empty, whitespace-only,
 * period-bearing, comma/question/exclamation/semicolon-bearing, unicode),
 * and *any* auto-label model selection, advancing the wizard's setup step
 * SHALL succeed exactly when the model is not `grounded-sam` or every
 * label's Effective_Prompt (per the Data Models oracle) contains no
 * period — a violation blocking the step with the error naming the first
 * offending label and its source (override vs label name).
 *
 * **Validates: Requirements 1.1, 1.2, 1.4, 1.5**
 *
 * Oracle (the design's Data Models guardrail oracle, restated):
 *   effective(l) = the typed override when non-blank after trimming,
 *                  else the label name l
 *   violation    iff model === 'grounded-sam' and ∃ l: '.' ∈ effective(l)
 * On clicking Next from the setup step: a violation blocks the step —
 * the step error is exactly the FIRST offending label's (in Label_Set
 * row order) source-specific message, both variants restated verbatim
 * below — while no violation advances the wizard to the review step (the
 * Create Job submit button becomes reachable).
 *
 * Generator domain notes (smart constraints, not oracle weakening):
 * - Label rows respect the wizard's own label rules — pre-trimmed,
 *   distinct, 1-64 characters, no CR/LF (single-line `<input>` elements
 *   strip newlines before the wizard sees them) — and exclude
 *   `Object.prototype` member names per the groundedsam.property
 *   precedent (the wizard's established per-label plain-object state
 *   idiom reads such keys through the prototype; prototype-key
 *   robustness is the draft module's covered surface). Rows violating
 *   the label rules reject the step for pre-existing reasons, so they
 *   are outside this property's domain. Period-bearing labels are built
 *   explicitly (inner/leading/trailing '.') so the label-source arm
 *   (Requirement 1.2) is exercised often.
 * - Override values stay within the 256-character limit and carry no
 *   CR/LF (structurally: short period-free pieces composed with
 *   whitespace and single punctuation marks), so the pre-existing
 *   over-length rule never fires and the Prompt_Guardrail is the only
 *   step gate in play.
 * - The `sam` arm types the same (possibly period-bearing) entries under
 *   a grounded-sam selection first — override inputs render only under
 *   grounded-sam — then switches the model to `sam` before Next, so
 *   Requirement 1.5 is proven against retained period-bearing override
 *   state as well as period-bearing label names.
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
import * as fc from 'fast-check';

import CreateLabelingJob from './CreateLabelingJob';

const { apiMocks, navigateMock } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    listWorkteams: vi.fn(),
    createLabelingJob: vi.fn(),
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
// the setup step's selects are exactly [team, auto-label model] and its
// first toggle is the model-assisted pre-labeling toggle.
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

/**
 * Reset every mock to a benign default. The model catalog stays empty:
 * the auto-label select then offers exactly the static SAM and
 * Grounded-SAM entries for Segmentation/ObjectDetection — both model
 * arms of this property — with no LLM/Bedrock group in the way.
 */
function primeMocks() {
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
    models: [],
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
}

beforeEach(() => {
  window.localStorage.clear();
  primeMocks();
});

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/**
 * Building block: non-empty trim-stable unicode text free of periods and
 * CR/LF. Short grapheme counts keep every composite value well inside
 * the 64-character label rule and the 256-character override rule.
 */
const cleanPieceArb = fc
  .string({ unit: 'grapheme', minLength: 1, maxLength: 6 })
  .filter(
    (s) =>
      s.trim() === s &&
      s.length > 0 &&
      !s.includes('.') &&
      !/[\r\n]/.test(s)
  );

/** A period-free label row within the wizard's label constraints. */
const cleanLabelArb = cleanPieceArb.filter(
  (s) => s.length <= 64 && !(s in Object.prototype)
);

/**
 * A period-bearing label row (inner, leading, or trailing '.'), still
 * trim-stable — a period is non-whitespace, so every composite keeps its
 * non-whitespace edges — and within the label constraints. No
 * `Object.prototype` member name contains a period, so the exclusion
 * holds structurally (asserted anyway for defense).
 */
const periodLabelArb = fc
  .tuple(
    cleanPieceArb,
    cleanPieceArb,
    fc.constantFrom('inner', 'leading', 'trailing')
  )
  .map(([a, b, where]) =>
    where === 'inner' ? `${a}.${b}` : where === 'leading' ? `.${a}` : `${a}.`
  )
  .filter((s) => s.length <= 64 && !(s in Object.prototype));

/** Label rows: with and without periods (Requirements 1.1/1.2 need both). */
const labelArb = fc.oneof(
  { weight: 3, arbitrary: cleanLabelArb },
  { weight: 2, arbitrary: periodLabelArb }
);

/**
 * Whitespace-only entries (blank after trim → the label-name fallback).
 * No CR/LF: the override entries are single-line `<input>` elements,
 * whose HTML value sanitization strips newlines — not an enterable
 * character (the groundedsam.property precedent's domain note).
 */
const whitespaceOnlyArb = fc
  .array(fc.constantFrom(' ', '\t', '\u00a0'), { minLength: 1, maxLength: 4 })
  .map((chars) => chars.join(''));

/** A composite piece: clean text, empty, or whitespace-only padding. */
const pieceOrBlankArb = fc.oneof(
  { weight: 3, arbitrary: cleanPieceArb },
  { weight: 1, arbitrary: fc.constant('') },
  { weight: 1, arbitrary: whitespaceOnlyArb }
);

/**
 * Period-bearing override entries: '.' with any surrounding text or
 * padding (lone '.', ' . ', inner, leading, trailing). Every one
 * survives trimming — a period is non-whitespace — so each is an
 * override-source violation under grounded-sam (Requirement 1.1).
 */
const periodOverrideArb = fc
  .tuple(pieceOrBlankArb, pieceOrBlankArb)
  .map(([a, b]) => `${a}.${b}`);

/**
 * The accepted punctuation set: commas, question marks, exclamation
 * points, and semicolons do not split caption spans and MUST pass the
 * step exactly as before the feature (Requirement 1.4).
 */
const acceptedPunctuationArb = fc
  .tuple(pieceOrBlankArb, fc.constantFrom(',', '?', '!', ';'), pieceOrBlankArb)
  .map(([a, p, b]) => `${a}${p}${b}`);

/**
 * One Prompt_Override entry state: the five task-specified classes —
 * empty, whitespace-only, period-bearing, accepted-punctuation-bearing,
 * and clean unicode. All ≤ 256 characters and CR/LF-free by
 * construction, so the pre-existing over-length rule never interferes.
 */
const overrideValueArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 1, arbitrary: fc.constant('') },
  { weight: 2, arbitrary: whitespaceOnlyArb },
  { weight: 3, arbitrary: periodOverrideArb },
  { weight: 2, arbitrary: acceptedPunctuationArb },
  { weight: 3, arbitrary: cleanPieceArb }
);

/**
 * A full wizard scenario: 1-3 distinct label rows (mixing period-free
 * and period-bearing names), per-row override entries (null = never
 * touched) typed under a grounded-sam selection, and the model actually
 * in effect when Next is clicked — `grounded-sam` (the guardrail arm) or
 * `sam` (the no-guardrail arm, Requirement 1.5).
 */
const scenarioArb = fc.record({
  modality: fc.constantFrom('Segmentation', 'ObjectDetection'),
  labels: fc.uniqueArray(labelArb, { minLength: 1, maxLength: 3 }),
  overrideSlots: fc.array(fc.option(overrideValueArb, { nil: null }), {
    minLength: 3,
    maxLength: 3,
  }),
  submitModel: fc.constantFrom('grounded-sam', 'sam'),
});

// ---------------------------------------------------------------------------
// Oracle (restates the specified semantics independently)
// ---------------------------------------------------------------------------

interface ExpectedViolation {
  label: string;
  source: 'override' | 'label';
}

/**
 * The Data Models guardrail oracle: effective(l) is the typed entry when
 * non-blank after trimming, else the label name; the first label in row
 * order whose Effective_Prompt contains a period is the offender, its
 * source telling which value carried the period (Requirements 1.1, 1.2).
 */
function firstGuardrailOffender(
  labels: string[],
  typed: Map<string, string>
): ExpectedViolation | null {
  for (const label of labels) {
    const entry = typed.get(label);
    const survives = entry !== undefined && entry.trim() !== '';
    const effective = survives ? (entry as string) : label;
    if (effective.includes('.')) {
      return { label, source: survives ? 'override' : 'label' };
    }
  }
  return null;
}

/**
 * The two specified error variants, restated verbatim from the design
 * (Components §1: `promptGuardrailMessage`) — override source says the
 * prompt "contains a period", label source says the label "has no text
 * prompt" and directs to an override (Requirements 1.1, 1.2).
 */
function expectedGuardrailError(violation: ExpectedViolation): string {
  if (violation.source === 'override') {
    return `The text prompt for label "${violation.label}" contains a period. Periods separate labels in the detection caption — remove them or split the idea into a short noun phrase`;
  }
  return `Label "${violation.label}" contains a period and has no text prompt. Grounded-SAM uses the label name as its text prompt — enter a text prompt without periods for this label`;
}

// ---------------------------------------------------------------------------
// Wizard walk helpers (the groundedsam.property walk)
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
    { target: { value: 'guardrail-property-job' } }
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

/** Select the labeling team (required for a non-admin setup step). */
async function selectTeam(container: HTMLElement) {
  const teamSelect = createWrapper(container).findAllSelects()[0];
  await waitFor(() => {
    expect(teamSelect.findTrigger().getElement()).not.toBeDisabled();
  });
  teamSelect.openDropdown();
  teamSelect.selectOptionByValue('t-1');
}

/** The auto-label model select (the team select is the step's first select). */
function selectAutoLabelModel(container: HTMLElement, value: string) {
  const select = createWrapper(container).findAllSelects()[1];
  select.openDropdown();
  select.selectOptionByValue(value);
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
// Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property 1:
// Wizard accepts iff the guardrail holds
// ---------------------------------------------------------------------------

describe('Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property 1: Wizard accepts iff the guardrail holds', () => {
  it('advances the setup step exactly when the model is not grounded-sam or every Effective_Prompt is period-free, blocking violations with the first offender named by source', async () => {
    await fc.assert(
      fc.asyncProperty(scenarioArb, async (scenario) => {
        cleanup();
        window.localStorage.clear();
        primeMocks();

        const { container } = await renderToDdaSetup(scenario.modality);
        await selectTeam(container);

        // Label rows, as typed (pre-trimmed by generation, so the
        // effective Label_Set equals the rows in row order).
        fireEvent.change(labelRowInput(container, 0), {
          target: { value: scenario.labels[0] },
        });
        for (let i = 1; i < scenario.labels.length; i += 1) {
          fireEvent.click(screen.getByRole('button', { name: 'Add label' }));
          fireEvent.change(labelRowInput(container, i), {
            target: { value: scenario.labels[i] },
          });
        }

        enableAutoLabel(container);
        selectAutoLabelModel(container, 'grounded-sam');

        // Type the override entries under the grounded-sam selection —
        // the only selection rendering them — tracking the typed state
        // for the oracle (null = the entry is never touched).
        const typed = new Map<string, string>();
        scenario.labels.forEach((label, i) => {
          const value = scenario.overrideSlots[i] ?? null;
          if (value !== null) {
            fireEvent.change(overrideInput(container, label), {
              target: { value },
            });
            typed.set(label, value);
          }
        });

        // The sam arm switches family AFTER the entries were typed: the
        // retained (possibly period-bearing) override state and the
        // period-bearing label names must trip no guardrail under a
        // non-grounded-sam selection (Requirement 1.5).
        if (scenario.submitModel !== 'grounded-sam') {
          selectAutoLabelModel(container, scenario.submitModel);
        }

        const violation =
          scenario.submitModel === 'grounded-sam'
            ? firstGuardrailOffender(scenario.labels, typed)
            : null;

        clickNext();

        if (violation === null) {
          // Acceptance: the step advances — the review step's Create Job
          // submit button is reachable (Requirements 1.4, 1.5; period-free
          // grounded-sam runs pin the pre-feature acceptance, including
          // comma/question/exclamation/semicolon-bearing prompts).
          expect(
            await screen.findByRole('button', { name: 'Create Job' })
          ).toBeInTheDocument();
        } else {
          // Rejection: the step error is exactly the first offending
          // label's source-specific message (Requirements 1.1, 1.2). The
          // error Alert renders above the wizard, so it is the first
          // alert in DOM order; textContent comparison avoids Testing
          // Library's whitespace normalization over unicode labels.
          const expectedError = expectedGuardrailError(violation);
          await waitFor(() => {
            const alert = createWrapper(container).findAlert();
            expect(alert?.findHeader()?.getElement().textContent).toBe(
              "Couldn't create the labeling job"
            );
            expect(alert?.findContent()?.getElement().textContent).toBe(
              expectedError
            );
          });

          // ...and the wizard stays on the setup step: the review step
          // is unreachable.
          expect(
            screen.queryByRole('button', { name: 'Create Job' })
          ).toBeNull();
          expect(
            screen.getByText('Model-assisted pre-labeling')
          ).toBeInTheDocument();
        }
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});
