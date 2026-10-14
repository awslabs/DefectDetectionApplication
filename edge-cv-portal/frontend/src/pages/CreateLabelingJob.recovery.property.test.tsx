/**
 * Property-based test for Setup_Draft restore fidelity
 * (labeling-setup-session-recovery task 3.3).
 *
 * **Feature: labeling-setup-session-recovery, Property 5: Restoring a
 * draft is faithful end-to-end**
 *
 * *For any* submittable DDA draft (generator constrained to values the
 * wizard can reach and submit: backend DDA, a job name within bounds, a
 * well-formed dataset S3 URI, a task type, a team present in the mocked
 * team list, a valid label set for the modality, an auto-label
 * configuration drawn across disabled / `sam` / `bedrock:<id>` /
 * `llm:<id>` with a non-empty prompt and an in-range token budget
 * differing from the mocked catalog's `token_limit` for the `llm:` case,
 * arbitrary restored example refs, and step index 5), mounting the wizard
 * with that draft stored, choosing Restore, and submitting SHALL issue a
 * creation request whose fields equal the draft's values: the job name,
 * dataset prefix, task type, team id, label set, instructions, the
 * auto-label model **verbatim**, the detection prompt
 * character-for-character, the draft's token budget (not the catalog
 * pre-fill), the draft's downscale setting, and
 * `example_images`/`few_shot.examples` equal to the restored-first merge
 * with per-designation positions.
 *
 * **Validates: Requirements 3.2, 3.3, 3.5, 2.2, 4.3, 7.1**
 *
 * Approach — the render-per-run budget follows the repo precedent
 * `CreateLabelingJob.modelpicker.property.test.tsx` (full wizard mount per
 * fast-check run at `{ numRuns: 100 }`, mock scaffolding reused). Each run
 * seeds the Draft_Store through the shipped `writeLabelingJobDraft`,
 * mounts the wizard on clean state, clicks the Restore_Offer's Restore
 * action (the restored step index 5 lands the wizard on the review step),
 * waits for the Requirement 3.4 deferred team re-selection to surface in
 * the review step (the last asynchronous piece of the apply — by then the
 * mocked catalog has also resolved, so the token-budget pre-fill hazard of
 * Requirement 3.3 is armed), clicks Create Job, and compares the
 * `createLabelingJob` call's fields against the generated draft.
 *
 * Oracle independence: the expected few-shot example set is restated
 * inline (every good ref before every bad ref, per-designation positions
 * 0..n−1 in draft order) instead of importing the production helper, and
 * the expected label set / dataset prefix ride along from the generator
 * (`labelCores`, `expectedDatasetPrefix`) rather than re-running the
 * wizard's trim/parse logic.
 *
 * Decisive-budget note: generated `llm:` token budgets exclude every
 * mocked catalog `token_limit` *and* `MODEL_TOKEN_LIMIT_DEFAULT` (the
 * pre-fill fallback for models absent from the catalog), so a broken
 * pre-fill defusal can never coincide with the drafted value. Model ids
 * include one absent from the catalog (`ghost-model-x`), asserting
 * verbatim restoration even when the capability-filtered picker offers no
 * matching entry (Requirements 2.2, 3.5). Only modality-compatible model
 * values are generated: the wizard's compatibility effect clears
 * incompatible ones by design, which would be a false failure here; the
 * same effect resets few-shot/downscale/budget for non-`llm:` selections,
 * so those drafts carry the steady-state values a real save would hold.
 * `previewRun` stays null so no resume polling rides the runs.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import * as fc from 'fast-check';

import CreateLabelingJob, {
  BEDROCK_MODALITIES,
  LLM_MODALITIES,
  MODEL_TOKEN_LIMIT_DEFAULT,
  SAM_MODALITIES,
} from './CreateLabelingJob';
import { writeLabelingJobDraft } from './labelingJobDraft';
import type { LabelingJobDraft } from './labelingJobDraft';
import {
  MAX_IMAGE_EDGE_OPTIONS,
  TOKEN_BUDGET_CEILING,
  TOKEN_BUDGET_MIN,
} from '../components/labeling/PromptTuningPreview';

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

// A non-admin Job_Creator; generated drafts carry skip verification
// disabled, so the Requirement 3.6 drop is the identity and the DDA
// submission always carries `team_id`.
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

const USECASE_ID = 'uc-1';

/** Labeling teams the mocked `listLabelingTeams` returns; drafts draw from
 * these so the parked team id is always present in the loaded list
 * (Requirement 3.4's found case — the absent case is task 3.2's suite). */
const MOCK_TEAMS = [
  { team_id: 't-alpha', team_name: 'Restore Team Alpha', members: ['a'] },
  { team_id: 't-beta', team_name: 'Restore Team Beta', members: ['a', 'b'] },
  { team_id: 't-gamma', team_name: 'Restore Team Gamma', members: ['c'] },
];

/**
 * Model_Catalog behind the mocked `getBedrockModels`: every `token_limit`
 * differs from every generated budget, so the Requirement 3.3 assertion
 * (the draft's budget is presented, not the model-change pre-fill) is
 * decisive. `model-b` leaves `image_input` absent (still offered);
 * `ghost-model-x` is generated but deliberately NOT listed here.
 */
const CATALOG = [
  { id: 'model-a', label: 'Model A', image_limit: 8, token_limit: 4096, image_input: true },
  { id: 'model-b', label: 'Model B', token_limit: 8192 },
];

/** Budgets a (correct or broken) pre-fill could produce: the catalog
 * `token_limit`s plus the default used for catalog-absent models. */
const PREFILL_BUDGET_VALUES = new Set<number>([
  ...CATALOG.map((m) => m.token_limit).filter((v): v is number => typeof v === 'number'),
  MODEL_TOKEN_LIMIT_DEFAULT,
]);

/** Reset every mock to the benign defaults each run mounts against. */
function primeMocks() {
  vi.clearAllMocks();
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'UC1', s3_bucket: 'out-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingTeams.mockResolvedValue({
    teams: MOCK_TEAMS,
    count: MOCK_TEAMS.length,
  });
  apiMocks.getBedrockModels.mockResolvedValue({ models: CATALOG, region: 'us-east-1' });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({ job_id: 'job-1' });
}

beforeEach(() => {
  window.localStorage.clear();
  primeMocks();
});

// ---------------------------------------------------------------------------
// Generators — submittable DDA drafts (values the wizard can reach and the
// review step can submit without a validation rejection)
// ---------------------------------------------------------------------------

const modalityArb = fc.constantFrom('Classification', 'Segmentation', 'ObjectDetection');

/** Whitespace padding around persisted-verbatim, trimmed-on-use values. */
const paddingArb = fc.constantFrom('', ' ', '  ', '\t');

/** Job name: trims to 1..60 characters (DDA cap is 63), padded to exercise
 * the submission's trim. */
const jobNameArb = fc
  .tuple(
    paddingArb,
    fc.string({ unit: 'grapheme', minLength: 1, maxLength: 16 }).filter((s) => {
      const trimmed = s.trim();
      return trimmed.length >= 1 && trimmed.length <= 60;
    }),
    paddingArb
  )
  .map(([pre, core, post]) => `${pre}${core}${post}`);

const lowerAlnumDashArb = fc.constantFrom(
  ...'abcdefghijklmnopqrstuvwxyz0123456789-'.split('')
);
const keyCharArb = fc.constantFrom(
  ...'abcdefghijklmnopqrstuvwxyz0123456789-_./ é'.split('')
);

/** Well-formed `s3://bucket/prefix` dataset URI; the prefix rides along as
 * the oracle for the submitted `dataset_prefix`. */
const datasetArb = fc
  .tuple(
    fc.string({ unit: lowerAlnumDashArb, minLength: 3, maxLength: 12 }),
    fc.string({ unit: keyCharArb, minLength: 1, maxLength: 24 })
  )
  .map(([bucket, prefix]) => ({
    uri: `s3://${bucket}/${prefix}`,
    prefix,
  }));

/** One label core: trim-stable and within the 64-character label cap, so
 * padded rows trim back to exactly the distinct cores. */
const labelCoreArb = fc
  .string({ unit: 'grapheme', minLength: 1, maxLength: 8 })
  .filter((s) => s.trim() === s && s.length <= 64);

/**
 * DDA label rows as the pre-trim editor state: 1..5 distinct cores, each
 * padded with whitespace, sometimes followed by an empty row — the wizard
 * trims and drops empties, so the effective set is exactly `cores`.
 */
const ddaLabelsArb: fc.Arbitrary<{ cores: string[]; rows: string[] }> = fc
  .uniqueArray(labelCoreArb, { minLength: 1, maxLength: 5 })
  .chain((cores) =>
    fc
      .tuple(
        fc.array(paddingArb, { minLength: cores.length, maxLength: cores.length }),
        fc.array(paddingArb, { minLength: cores.length, maxLength: cores.length }),
        fc.boolean()
      )
      .map(([pres, posts, addEmptyRow]) => ({
        cores,
        rows: [
          ...cores.map((core, i) => `${pres[i]}${core}${posts[i]}`),
          ...(addEmptyRow ? [''] : []),
        ],
      }))
  );

/** Restored example refs: `s3://bucket/key`, 0..3 per designation. */
const exampleRefArb = fc
  .tuple(
    fc.string({ unit: lowerAlnumDashArb, minLength: 3, maxLength: 10 }),
    fc.string({ unit: keyCharArb, minLength: 1, maxLength: 20 })
  )
  .map(([bucket, key]) => `s3://${bucket}/labeling-examples/${key}`);

/** Detection prompt: non-empty after trim, well under the 2000 cap. */
const promptArb = fc
  .string({ unit: 'grapheme', minLength: 1, maxLength: 40 })
  .filter((s) => s.trim().length > 0 && s.length <= 2000);

/** In-range whole-number budget that no pre-fill could produce. */
const tokenBudgetArb = fc
  .integer({ min: TOKEN_BUDGET_MIN, max: TOKEN_BUDGET_CEILING })
  .filter((v) => !PREFILL_BUDGET_VALUES.has(v))
  .map((v) => String(v));

/** Downscale_Setting values the preview's control offers: off or one of
 * the six Max_Image_Edge options. */
const downscaleArb = fc.constantFrom<number | null>(null, ...MAX_IMAGE_EDGE_OPTIONS);

/** Catalog ids plus one id absent from the catalog (verbatim survival —
 * Requirements 2.2, 3.5 — and the default-pre-fill hazard arm). */
const modelIdArb = fc.constantFrom('model-a', 'model-b', 'ghost-model-x');

interface AutoLabelDraw {
  enabled: boolean;
  model: string;
  prompt: string;
  fewShotWanted: boolean;
  downscale: number | null;
  budget: string;
}

/**
 * Auto-label configuration compatible with the drawn modality: disabled and
 * `llm:` are always reachable; `sam` and `bedrock:` only where the shipped
 * compatibility matrix allows them (the effect clears incompatible values,
 * which would be a false failure). Non-`llm:` draws carry the steady-state
 * few-shot/downscale/budget values the compatibility effect enforces.
 */
function autoLabelConfigArb(modality: string): fc.Arbitrary<AutoLabelDraw> {
  const disabledDraw: AutoLabelDraw = {
    enabled: false,
    model: '',
    prompt: '',
    fewShotWanted: false,
    downscale: null,
    budget: '',
  };
  const kinds: Array<'disabled' | 'sam' | 'bedrock' | 'llm'> = ['disabled', 'llm'];
  if (SAM_MODALITIES.includes(modality)) kinds.push('sam');
  if (BEDROCK_MODALITIES.includes(modality)) kinds.push('bedrock');
  return fc.constantFrom(...kinds).chain((kind): fc.Arbitrary<AutoLabelDraw> => {
    switch (kind) {
      case 'disabled':
        return fc.constant(disabledDraw);
      case 'sam':
        return fc.constant({ ...disabledDraw, enabled: true, model: 'sam' });
      case 'bedrock':
        return modelIdArb.map((id) => ({
          ...disabledDraw,
          enabled: true,
          model: `bedrock:${id}`,
        }));
      case 'llm':
        return fc
          .record({
            id: modelIdArb,
            prompt: promptArb,
            fewShotWanted: fc.boolean(),
            downscale: downscaleArb,
            budget: tokenBudgetArb,
          })
          .map(({ id, prompt, fewShotWanted, downscale, budget }) => ({
            enabled: true,
            model: `llm:${id}`,
            prompt,
            fewShotWanted,
            downscale,
            budget,
          }));
    }
  });
}

interface RestoreCase {
  draft: LabelingJobDraft;
  /** The distinct trimmed labels the rows reduce to (oracle for label_set). */
  labelCores: string[];
  /** The generated prefix behind the dataset URI (oracle for dataset_prefix). */
  expectedDatasetPrefix: string;
}

const restoreCaseArb: fc.Arbitrary<RestoreCase> = fc
  .record({
    modality: modalityArb,
    jobName: jobNameArb,
    description: fc.string({ unit: 'grapheme', maxLength: 16 }),
    dataset: datasetArb,
    ddaLabels: ddaLabelsArb,
    ddaInstructions: fc.string({ unit: 'grapheme', maxLength: 24 }),
    team: fc.constantFrom(...MOCK_TEAMS),
    goodRefs: fc.array(exampleRefArb, { maxLength: 3 }),
    badRefs: fc.array(exampleRefArb, { maxLength: 3 }),
    previewSelectedKeys: fc.oneof(
      fc.constant<string[]>([]),
      fc.array(fc.string({ unit: keyCharArb, minLength: 1, maxLength: 12 }), {
        minLength: 1,
        maxLength: 3,
      })
    ),
  })
  .chain((base) =>
    autoLabelConfigArb(base.modality).map((auto): RestoreCase => {
      const draft: LabelingJobDraft = {
        version: 1,
        savedAtMs: 0, // re-stamped by writeLabelingJobDraft at seed time
        usecaseId: USECASE_ID,
        activeStepIndex: 5, // review step — Create Job is directly reachable
        labelingBackend: 'DDA',
        jobName: base.jobName,
        description: base.description,
        datasetS3Uri: base.dataset.uri,
        maskPrefix: '',
        taskTypeValue: base.modality,
        workforceTypeValue: 'private',
        labelCategories: '',
        gtInstructions: '',
        enableAutomatedLabeling: false,
        ddaLabels: base.ddaLabels.rows,
        ddaInstructions: base.ddaInstructions,
        selectedTeam: {
          teamId: base.team.team_id,
          teamName: base.team.team_name,
        },
        autoLabelEnabled: auto.enabled,
        autoLabelModel: auto.model,
        detectionPrompt: auto.prompt,
        // The few-shot rule needs at least one example; with no staged
        // files the restored refs are the only examples.
        fewShotEnabled:
          auto.fewShotWanted && base.goodRefs.length + base.badRefs.length > 0,
        downscaleMaxEdge: auto.downscale,
        tokenBudget: auto.budget,
        skipVerification: false,
        skipVerificationModelId: '',
        perLabelPrompts: {},
        exampleRefs: { good: base.goodRefs, bad: base.badRefs },
        previewSelectedKeys: base.previewSelectedKeys,
        previewRun: null, // no resume polling rides these runs (Req 5.5 aside)
      };
      return {
        draft,
        labelCores: base.ddaLabels.cores,
        expectedDatasetPrefix: base.dataset.prefix,
      };
    })
  );

// ---------------------------------------------------------------------------
// Property 5
// ---------------------------------------------------------------------------

describe('Feature: labeling-setup-session-recovery, Property 5: Restoring a draft is faithful end-to-end', () => {
  it('mount → Restore → Create Job issues a creation request equal to the draft', async () => {
    // The generator assumes `llm:` is reachable for every modality and the
    // sam/bedrock gates come from the shipped matrix; pin that agreement
    // before spending 100 rendered runs on it.
    expect(LLM_MODALITIES).toEqual([
      'Classification',
      'Segmentation',
      'ObjectDetection',
    ]);

    await fc.assert(
      fc.asyncProperty(restoreCaseArb, async ({ draft, labelCores, expectedDatasetPrefix }) => {
        cleanup();
        window.localStorage.clear();
        primeMocks();

        // Seed the Draft_Store exactly as the wizard's save effect would
        // (version/savedAt/usecase stamped by the shipped writer).
        writeLabelingJobDraft(USECASE_ID, draft);

        render(<CreateLabelingJob />);

        // The Restore_Offer appears once the use case resolves and the
        // draft is read (Req 3.1); choose Restore (Req 3.2).
        fireEvent.click(
          await screen.findByTestId('draft-restore-button', {}, { timeout: 10_000 })
        );

        // The wizard lands on the restored review step. The parked team
        // re-selection (Req 3.4) settles once the mocked team list
        // resolves; its name showing in the review step is the signal
        // that the restore has fully applied. (findAll: a generated
        // string colliding with the fixed name must not throw.)
        await screen.findAllByText(
          draft.selectedTeam!.teamName,
          {},
          { timeout: 10_000 }
        );

        // By now the catalog has resolved too (same commit kicked off both
        // fetches), so the model-change pre-fill hazard has had every
        // chance to clobber the restored budget (Req 3.3).
        expect(apiMocks.getBedrockModels).toHaveBeenCalled();

        // Submit from the review step: restored values validate exactly as
        // manually entered ones and the builder consumes them unchanged
        // (Req 7.1).
        fireEvent.click(screen.getByRole('button', { name: 'Create Job' }));
        await waitFor(
          () => expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1),
          { timeout: 10_000 }
        );

        const payload = apiMocks.createLabelingJob.mock.calls[0][0];

        // --- Fidelity: every asserted field equals the draft's value ----
        expect(payload.usecase_id).toBe(USECASE_ID);
        expect(payload.labeling_backend).toBe('DDA');
        expect(payload.job_name).toBe(draft.jobName.trim());
        expect(payload.dataset_prefix).toBe(expectedDatasetPrefix);
        expect(payload.task_type).toBe(draft.taskTypeValue);
        // No skip verification in any generated draft → team id rides.
        expect(payload.skip_verification).toBeUndefined();
        expect(payload.team_id).toBe(draft.selectedTeam!.teamId);
        expect(payload.label_set).toEqual(
          draft.taskTypeValue === 'Classification'
            ? ['normal', 'anomaly']
            : labelCores
        );
        expect(payload.instructions).toBe(
          draft.ddaInstructions === '' ? undefined : draft.ddaInstructions
        );

        // Restored-first merge with no staged Files: exactly the restored
        // refs, per designation, in draft order (Req 4.3).
        expect(payload.example_images).toEqual({
          good: draft.exampleRefs.good,
          bad: draft.exampleRefs.bad,
        });

        // Few-shot examples: every good ref before every bad ref with
        // per-designation positions 0..n−1 in merge order — restated
        // inline as the oracle (Req 4.3).
        const isLlm = draft.autoLabelModel.startsWith('llm:');
        expect(payload.few_shot).toEqual(
          draft.autoLabelEnabled && isLlm && draft.fewShotEnabled
            ? {
                enabled: true,
                examples: [
                  ...draft.exampleRefs.good.map((ref, position) => ({
                    ref,
                    designation: 'good',
                    position,
                  })),
                  ...draft.exampleRefs.bad.map((ref, position) => ({
                    ref,
                    designation: 'bad',
                    position,
                  })),
                ],
              }
            : { enabled: false, examples: [] }
        );

        // Auto-label block: model verbatim (Req 2.2, 3.5 — including ids
        // the picker does not offer), prompt character-for-character, the
        // draft's downscale setting, and the draft's budget — a pre-fill
        // would have produced a value the generator excluded (Req 3.3).
        if (!draft.autoLabelEnabled) {
          expect(payload.auto_label).toBeUndefined();
        } else if (isLlm) {
          expect(payload.auto_label).toEqual({
            enabled: true,
            model: draft.autoLabelModel,
            detection_prompt: draft.detectionPrompt,
            downscale_max_edge: draft.downscaleMaxEdge,
            token_budget: Number(draft.tokenBudget),
          });
        } else {
          expect(payload.auto_label).toEqual({
            enabled: true,
            model: draft.autoLabelModel,
          });
        }
      }),
      { numRuns: 100 }
    );
  }, 900_000);
});
