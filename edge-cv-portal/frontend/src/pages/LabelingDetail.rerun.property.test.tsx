/**
 * Property-based tests for the LabelingDetail re-run pre-labels surface
 * (grounded-sam-prompt-guardrails-and-prelabel-retry task 4.7), following
 * the render-per-run discipline of the page's sibling property suites:
 * every fast-check run mounts `LabelingDetail` fresh against a mocked
 * `apiService.getLabelingJob` record and asserts against oracles that
 * restate the specified semantics independently of the implementation.
 *
 * **Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
 * Property 10: The re-run action renders iff the record satisfies the
 * eligibility predicate**
 *
 * *For any* job-detail record shape (backend, status, `review_finalized`,
 * `auto_label.enabled`, `skip_verification`, `prelabel_failed_count` drawn
 * across present/absent/zero/positive), the Job_Detail_Page SHALL render
 * the "Re-run pre-labels" action (with the failed count) exactly when the
 * record is a Retry_Eligible_Job — the predicate restated below: DDA and
 * InProgress and (auto-label enabled or skip-verification) and
 * review_finalized not truthy and failed count >= 1. The exported
 * `canRerunPrelabels` must agree with the restated oracle (cross-check),
 * but the oracle, not the export, decides the expected rendering.
 *
 * **Validates: Requirements 7.1, 7.2**
 *
 * **Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
 * Property 11: The dialog's request carries exactly the pruned overrides,
 * omitted iff equal to the persisted map**
 *
 * *For any* grounded-sam Retry_Eligible record with a persisted
 * `prompt_overrides` map and *any* dialog edit script (per label: keep the
 * pre-filled value, clear it, or re-type a period-free value, plain or
 * whitespace-padded), submitting the re-run dialog SHALL call
 * `apiService.rerunPrelabels` with `prompt_overrides` equal to exactly
 * the entries non-empty after trimming whose label belongs to the
 * Label_Set, raw values character-for-character — the body omitted
 * exactly when that pruned map equals the persisted map (own-keys string
 * equality). The mocked 202 closes the dialog and refetches the detail.
 *
 * **Validates: Requirements 7.5**
 *
 * Generator domain notes (smart constraints, not oracle weakening):
 * - Property 10 weights the all-gates-pass branch: a uniform draw over
 *   the six eligibility dimensions would render the button in only ~3%
 *   of runs, so a dedicated eligible arm keeps both directions of the
 *   iff exercised.
 * - Property 11's labels and typed values are period-free and within the
 *   256-character raw-length limit: a period or an over-length value
 *   blocks the dialog's submission by design (Requirements 7.3, 1.1), so
 *   no request exists to observe. Labels are period-free so a cleared
 *   entry's label-name fallback cannot trip the guardrail either.
 * - No CR/LF in labels or values: the override entries are single-line
 *   `<input>` elements whose HTML value sanitization strips newlines —
 *   not an enterable character (the groundedsam.property precedent).
 * - Labels shadowing `Object.prototype` members (`toString`,
 *   `__proto__`, ...) are excluded: the page's per-label plain-object
 *   override state (shared idiom with the wizard) reads such keys
 *   through the prototype.
 * - Persisted override values are non-blank after trimming: creation
 *   prunes blank entries before persisting, so a blank persisted value
 *   is outside the reachable record space.
 */
import { describe, expect, it, vi } from 'vitest';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from '@testing-library/react';
import * as fc from 'fast-check';

import LabelingDetail, { canRerunPrelabels } from './LabelingDetail';

const { getLabelingJob, rerunPrelabels } = vi.hoisted(() => ({
  getLabelingJob: vi.fn(),
  rerunPrelabels: vi.fn(),
}));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getLabelingJob, rerunPrelabels },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => Promise.resolve({});
      },
    }
  );
  return { apiService };
});

vi.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => vi.fn(),
}));

// The manifest transform modal pulls heavy dependencies; these tests
// exercise only the DDA detail view (the LabelingDetail.test.tsx mock).
vi.mock('../components/ManifestTransformer', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------

/** A `GET /labeling/{id}` job payload as the mocked apiService serves it. */
type JobDetailRecord = Record<string, unknown> & {
  labeling_backend?: 'DDA' | 'GroundTruth';
  status?: string;
  auto_label?: {
    enabled?: boolean;
    model?: string;
    detection_prompt?: string;
    prompt_overrides?: Record<string, string>;
  };
  skip_verification?: boolean;
  review_finalized?: boolean;
  prelabel_failed_count?: number;
  prelabel_available_count?: number;
  label_set?: string[];
};

/** Complete base record so both the DDA and Ground Truth views render. */
const BASE_JOB = {
  job_id: 'job-1',
  usecase_id: 'usecase-1',
  job_name: 'rerun-property-job',
  sagemaker_job_name: '',
  status: 'InProgress',
  task_type: 'Segmentation',
  dataset_prefix: 'datasets/d1',
  image_count: 10,
  label_categories: [] as string[],
  manifest_s3_uri: '',
  output_s3_uri: '',
  workforce_arn: '',
  created_at: 1700000000000,
  created_by: 'admin',
  updated_at: 1700000000000,
  labeling_backend: 'DDA' as const,
  label_set: ['scratch', 'dent'],
  submitted_count: 2,
};

/** Mount the page against the record and let the loadJob effect resolve. */
async function renderDetail(record: JobDetailRecord) {
  getLabelingJob.mockResolvedValue({ job: record });
  const view = render(<LabelingDetail />);
  await act(async () => {});
  // Guard both directions of the iff: an absent button must mean an
  // ineligible record, never a page still loading.
  expect(screen.queryByText('Loading labeling job details...')).toBeNull();
  return view;
}

/**
 * Exact-attribute input lookup: generated labels are arbitrary unicode
 * and Testing Library's accessible-name matching normalizes whitespace,
 * so the dialog's Prompt_Override inputs
 * (`aria-label="Text prompt for {label}"`) are located by verbatim
 * attribute equality (the groundedsam.property precedent). The Modal
 * renders through a portal, so the whole document is searched.
 */
function inputByExactAriaLabel(ariaLabel: string): HTMLInputElement {
  const match = Array.from(document.querySelectorAll('input')).find(
    (el) => el.getAttribute('aria-label') === ariaLabel
  );
  if (!match) {
    throw new Error(`No input with aria-label ${JSON.stringify(ariaLabel)}`);
  }
  return match as HTMLInputElement;
}

// ---------------------------------------------------------------------------
// Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property 10:
// The re-run action renders iff the record satisfies the eligibility
// predicate
// ---------------------------------------------------------------------------

/** One point of the eligibility-record space, pre-record-assembly. */
interface EligibilityParts {
  backend: 'DDA' | 'GroundTruth';
  status: 'InProgress' | 'Stopped' | 'Completed' | 'Failed';
  /** undefined = attribute absent on the record. */
  reviewFinalized: boolean | undefined;
  /** 'absent' = no auto_label object at all. */
  autoLabelEnabled: 'absent' | boolean;
  skipVerification: boolean;
  /** undefined = attribute absent on the record. */
  failedCount: number | undefined;
  /** Noise: the count of resolved-Available tasks. */
  availableCount: number | undefined;
  /** Noise: the family does not participate in the predicate. */
  model: string;
}

const statusArb = fc.constantFrom<EligibilityParts['status']>(
  'InProgress',
  'Stopped',
  'Completed',
  'Failed'
);
const availableCountArb = fc.constantFrom<number | undefined>(
  undefined,
  0,
  3
);
const modelNoiseArb = fc.constantFrom(
  'grounded-sam',
  'sam',
  'bedrock:m-1',
  'llm:m-1'
);

/** Unconstrained draw: every gate free (mostly ineligible records). */
const freePartsArb: fc.Arbitrary<EligibilityParts> = fc.record({
  backend: fc.constantFrom<EligibilityParts['backend']>(
    'DDA',
    'GroundTruth'
  ),
  status: statusArb,
  reviewFinalized: fc.constantFrom<boolean | undefined>(
    undefined,
    false,
    true
  ),
  autoLabelEnabled: fc.constantFrom<'absent' | boolean>(
    'absent',
    false,
    true
  ),
  skipVerification: fc.boolean(),
  failedCount: fc.oneof(
    fc.constant<number | undefined>(undefined),
    fc.constant(0),
    fc.constant(1),
    fc.integer({ min: 2, max: 5000 })
  ),
  availableCount: availableCountArb,
  model: modelNoiseArb,
});

/** All gates pass: DDA, InProgress, autolabel-or-skip, unfinalized, >=1. */
const eligiblePartsArb: fc.Arbitrary<EligibilityParts> = fc
  .record({
    reviewFinalized: fc.constantFrom<boolean | undefined>(undefined, false),
    autoLabelEnabled: fc.constantFrom<'absent' | boolean>(
      'absent',
      false,
      true
    ),
    skipVerification: fc.boolean(),
    failedCount: fc.integer({ min: 1, max: 5000 }),
    availableCount: availableCountArb,
    model: modelNoiseArb,
  })
  .map(
    (parts): EligibilityParts => ({
      backend: 'DDA',
      status: 'InProgress',
      ...parts,
      // Force the autolabel-on gate when the free draw missed it.
      skipVerification:
        parts.autoLabelEnabled === true ? parts.skipVerification : true,
    })
  );

/** Weighted mix keeping both directions of the iff well represented. */
const detailPartsArb = fc.oneof(
  { weight: 3, arbitrary: freePartsArb },
  { weight: 2, arbitrary: eligiblePartsArb }
);

/** Assemble the job-detail record, omitting the absent-valued attributes. */
function buildDetailRecord(parts: EligibilityParts): JobDetailRecord {
  const record: JobDetailRecord = {
    ...BASE_JOB,
    labeling_backend: parts.backend,
    status: parts.status,
    skip_verification: parts.skipVerification,
  };
  if (parts.autoLabelEnabled !== 'absent') {
    record.auto_label = {
      enabled: parts.autoLabelEnabled,
      model: parts.model,
      ...(parts.model.startsWith('llm:')
        ? { detection_prompt: 'find each scratch' }
        : {}),
    };
  }
  if (parts.reviewFinalized !== undefined) {
    record.review_finalized = parts.reviewFinalized;
  }
  if (parts.failedCount !== undefined) {
    record.prelabel_failed_count = parts.failedCount;
  }
  if (parts.availableCount !== undefined) {
    record.prelabel_available_count = parts.availableCount;
  }
  return record;
}

/**
 * The Retry_Eligible_Job predicate restated from Requirements 7.1/7.2
 * (deliberately not delegating to the page's exported helper): DDA
 * backend, InProgress status, auto-labeling on (enabled or
 * skip-verification), review not finalized, and at least one Failed
 * pre-label task.
 */
function rerunEligibleOracle(record: JobDetailRecord): boolean {
  return (
    record.labeling_backend === 'DDA' &&
    record.status === 'InProgress' &&
    (record.auto_label?.enabled === true ||
      record.skip_verification === true) &&
    record.review_finalized !== true &&
    (record.prelabel_failed_count ?? 0) >= 1
  );
}

describe('Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property 10: The re-run action renders iff the record satisfies the eligibility predicate', () => {
  it('renders the Re-run pre-labels action with the failed count exactly for Retry_Eligible records', async () => {
    await fc.assert(
      fc.asyncProperty(detailPartsArb, async (parts) => {
        cleanup();
        vi.clearAllMocks();

        const record = buildDetailRecord(parts);
        const expected = rerunEligibleOracle(record);

        // Cross-check: the exported predicate agrees with the restated
        // oracle before the rendering is judged against it.
        expect(canRerunPrelabels(record)).toBe(expected);

        await renderDetail(record);

        const button = screen.queryByTestId('rerun-prelabels-button');
        if (expected) {
          expect(button).not.toBeNull();
          // The action displays the Failed_Prelabel_Task count (7.1).
          expect(button!.textContent ?? '').toContain(
            `Re-run pre-labels (${(
              record.prelabel_failed_count as number
            ).toLocaleString()} failed)`
          );
        } else {
          expect(button).toBeNull();
        }
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});

// ---------------------------------------------------------------------------
// Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property 11:
// The dialog's request carries exactly the pruned overrides, omitted iff
// equal to the persisted map
// ---------------------------------------------------------------------------

/**
 * Label_Set labels: distinct, pre-trimmed, period-free unicode (a
 * period-bearing label with a cleared override would block the dialog by
 * design — outside this property's domain), no CR/LF, no
 * `Object.prototype` member names (see the header's domain notes).
 */
const labelArb = fc
  .string({ unit: 'grapheme', minLength: 1, maxLength: 8 })
  .filter(
    (s) =>
      s.trim() === s &&
      s.length > 0 &&
      !s.includes('.') &&
      !/[\r\n]/.test(s) &&
      !(s in Object.prototype)
  );

/** Period-free prompt text that survives trimming; well under 256 raw. */
const promptTextArb = fc
  .string({ unit: 'grapheme', minLength: 1, maxLength: 10 })
  .filter(
    (s) => !s.includes('.') && !/[\r\n]/.test(s) && s.trim() !== ''
  );

/** Enterable single-line whitespace (space, tab, NBSP — all trimmed). */
const paddingArb = fc
  .array(fc.constantFrom(' ', '\t', '\u00a0'), { maxLength: 3 })
  .map((chars) => chars.join(''));

/** Whitespace-padded period-free value: the raw form must ride through. */
const paddedPromptArb = fc
  .tuple(paddingArb, promptTextArb, paddingArb)
  .map(([lead, body, tail]) => `${lead}${body}${tail}`);

/** Persisted values survive trimming (creation pruned the blanks). */
const persistedValueArb = fc.oneof(
  { weight: 3, arbitrary: promptTextArb },
  { weight: 1, arbitrary: paddedPromptArb }
);

/** One label's dialog edit: keep the pre-fill, clear it, or re-type. */
type EditAction =
  | { kind: 'keep' }
  | { kind: 'clear' }
  | { kind: 'type'; value: string };

const editActionArb: fc.Arbitrary<EditAction> = fc.oneof(
  { weight: 3, arbitrary: fc.constant<EditAction>({ kind: 'keep' }) },
  { weight: 2, arbitrary: fc.constant<EditAction>({ kind: 'clear' }) },
  {
    weight: 3,
    arbitrary: promptTextArb.map(
      (value): EditAction => ({ kind: 'type', value })
    ),
  },
  {
    weight: 2,
    arbitrary: paddedPromptArb.map(
      (value): EditAction => ({ kind: 'type', value })
    ),
  }
);

interface RerunScenario {
  labels: string[];
  persisted: Record<string, string>;
  /** Parallel to `labels`. */
  actions: EditAction[];
  failedCount: number;
}

const rerunScenarioArb: fc.Arbitrary<RerunScenario> = fc
  .uniqueArray(labelArb, { minLength: 1, maxLength: 3 })
  .chain((labels) =>
    fc.record({
      labels: fc.constant(labels),
      persistedValues: fc.array(
        fc.option(persistedValueArb, { nil: null }),
        { minLength: labels.length, maxLength: labels.length }
      ),
      actions: fc.oneof(
        // A dedicated all-keep arm keeps the omitted-body branch (pruned
        // map equals the persisted map) well represented.
        {
          weight: 1,
          arbitrary: fc.constant(
            labels.map((): EditAction => ({ kind: 'keep' }))
          ),
        },
        {
          weight: 4,
          arbitrary: fc.array(editActionArb, {
            minLength: labels.length,
            maxLength: labels.length,
          }),
        }
      ),
      failedCount: fc.integer({ min: 1, max: 500 }),
    })
  )
  .map(({ labels, persistedValues, actions, failedCount }) => {
    const persisted: Record<string, string> = {};
    labels.forEach((label, index) => {
      const value = persistedValues[index];
      if (value !== null) {
        persisted[label] = value;
      }
    });
    return { labels, persisted, actions, failedCount };
  });

/**
 * Own-keys string-record equality restated from Requirement 7.5: same
 * key set, character-identical values.
 */
function ownKeysStringEqual(
  a: Record<string, string>,
  b: Record<string, string>
): boolean {
  const aKeys = Object.keys(a);
  return (
    aKeys.length === Object.keys(b).length &&
    aKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(b, key) && a[key] === b[key]
    )
  );
}

describe('Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property 11: The dialog\'s request carries exactly the pruned overrides, omitted iff equal to the persisted map', () => {
  it('sends exactly the pruned override map, omitting the body when it equals the persisted map, then closes and refetches', async () => {
    await fc.assert(
      fc.asyncProperty(
        rerunScenarioArb,
        async ({ labels, persisted, actions, failedCount }) => {
          cleanup();
          vi.clearAllMocks();

          // A grounded-sam Retry_Eligible record carrying the persisted
          // map (the key absent when empty, the creation invariant).
          const record: JobDetailRecord = {
            ...BASE_JOB,
            labeling_backend: 'DDA',
            status: 'InProgress',
            label_set: labels,
            prelabel_failed_count: failedCount,
            prelabel_available_count: 0,
            auto_label: {
              enabled: true,
              model: 'grounded-sam',
              ...(Object.keys(persisted).length > 0
                ? { prompt_overrides: { ...persisted } }
                : {}),
            },
          };
          rerunPrelabels.mockResolvedValue({
            job_id: 'job-1',
            retried_count: failedCount,
            message: `Re-run started for ${failedCount} failed pre-label task(s)`,
          });

          await renderDetail(record);

          // Open the dialog from the eligibility-gated action.
          fireEvent.click(screen.getByTestId('rerun-prelabels-button'));
          await screen.findByTestId('rerun-prelabels-modal');

          // Apply the edit script through the aria-labeled entries,
          // tracking each entry's final raw value for the oracle.
          const finalValues: Record<string, string | undefined> = {};
          labels.forEach((label, index) => {
            const action = actions[index];
            const input = inputByExactAriaLabel(`Text prompt for ${label}`);
            if (action.kind === 'keep') {
              finalValues[label] = persisted[label];
            } else if (action.kind === 'clear') {
              fireEvent.change(input, { target: { value: '' } });
              finalValues[label] = '';
            } else {
              fireEvent.change(input, { target: { value: action.value } });
              finalValues[label] = action.value;
            }
          });

          // The Requirement 7.5 oracle: entries non-blank after trimming
          // keyed by Label_Set labels, raw values; the body omitted
          // exactly when the pruned map equals the persisted map.
          const pruned: Record<string, string> = {};
          for (const label of labels) {
            const value = finalValues[label];
            if (typeof value === 'string' && value.trim() !== '') {
              pruned[label] = value;
            }
          }
          const expectedBody = ownKeysStringEqual(pruned, persisted)
            ? undefined
            : { prompt_overrides: pruned };

          fireEvent.click(screen.getByTestId('rerun-prelabels-submit'));
          await act(async () => {});

          expect(rerunPrelabels).toHaveBeenCalledTimes(1);
          const [calledJobId, calledBody] = rerunPrelabels.mock.calls[0];
          expect(calledJobId).toBe('job-1');
          if (expectedBody === undefined) {
            expect(calledBody).toBeUndefined();
          } else {
            expect(calledBody).toStrictEqual(expectedBody);
          }

          // The 202 closes the dialog (no inline error) and refetches
          // the job detail (Requirement 7.6 wiring of the 7.5 request).
          expect(
            screen.queryByTestId('rerun-prelabels-modal')
          ).toBeNull();
          expect(
            screen.queryByTestId('rerun-prelabels-error')
          ).toBeNull();
          expect(getLabelingJob).toHaveBeenCalledTimes(2);
        }
      ),
      { numRuns: 100 }
    );
  }, 900_000);
});
