/**
 * Property-based tests for the `labelingJobDraft.ts` Draft_Store accessors
 * (spec: labeling-setup-session-recovery) — the storage-behavior half of the
 * module's property coverage:
 *
 * - Property 1: Draft serialization round-trips (this file, first describe)
 * - Property 2: Reading tolerates anything and purges stale drafts
 *   (task 1.3, appended as a separate describe)
 *
 * Runs against jsdom's localStorage (the vitest environment), cleared inside
 * every property run so the 100 iterations stay independent. Shared
 * generators live at the top of the file so subsequent property describe
 * blocks can be appended without duplication.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import * as fc from 'fast-check';
import {
  DRAFT_STALENESS_MS,
  LABELING_JOB_DRAFT_VERSION,
  type LabelingJobDraft,
  type PreviewRunReference,
  draftsEquivalent,
  labelingJobDraftKey,
  readLabelingJobDraft,
  writeLabelingJobDraft,
} from './labelingJobDraft';

// ------------------------------------------------------------- generators

/**
 * Free-text wizard values: empty, whitespace-only, padded, unicode
 * (graphemes incl. emoji / non-BMP), and plain ASCII strings — the draft
 * must carry all of them verbatim (Requirement 2.1).
 */
const anyStringArb: fc.Arbitrary<string> = fc.oneof(
  fc.string(),
  fc.string({ unit: 'grapheme' }),
  fc.constantFrom('', ' ', '\t \n', '  padded  ', 'ünïcode 日本語 🙂', 'label/with "quotes" \\ and \n newline')
);

/** The Draft_Key's Use_Case id — any non-empty string, uuid-shaped or not. */
const usecaseIdArb: fc.Arbitrary<string> = fc.oneof(
  fc.uuid(),
  anyStringArb.filter((s) => s.length > 0)
);

/**
 * Recorded auto-label model selection values: the wizard's `sam` /
 * `bedrock:<id>` / `llm:<id>` shapes plus fully arbitrary strings — ids
 * absent from any catalog must survive verbatim (Requirement 2.2).
 */
const autoLabelModelArb: fc.Arbitrary<string> = fc.oneof(
  fc.constantFrom('', 'sam'),
  anyStringArb.map((s) => `bedrock:${s}`),
  anyStringArb.map((s) => `llm:${s}`),
  anyStringArb
);

/** Example-image refs: realistic `s3://…` URIs and arbitrary strings. */
const exampleRefArb: fc.Arbitrary<string> = fc.oneof(
  fc
    .tuple(fc.stringMatching(/^[a-z0-9][a-z0-9.-]{2,20}$/), anyStringArb)
    .map(([bucket, key]) => `s3://${bucket}/labeling-examples/${key}`),
  anyStringArb
);

/** A Preview_Run_Reference (Requirement 2.4). */
const previewRunArb: fc.Arbitrary<PreviewRunReference> = fc.record({
  runId: fc.oneof(fc.uuid(), anyStringArb),
  sampleCount: fc.integer({ min: 0, max: 500 }),
  startedAtMs: fc.integer({ min: 0, max: 4_102_444_800_000 }),
});

/**
 * Per-label prompt keys are user-entered DDA label names — any text,
 * explicitly including the literal key `'__proto__'` (drawn with weight
 * so every run set exercises it): `JSON.parse` produces it as an own
 * property and the module's record normalizer must preserve it as an own
 * data property rather than routing it through the prototype setter and
 * dropping it.
 */
const perLabelKeyArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 9, arbitrary: anyStringArb },
  { weight: 1, arbitrary: fc.constant('__proto__') }
);

/**
 * Arbitrary drafts across both wizard branches (Requirement 2.1): every
 * field drawn independently, so DDA-only and Ground-Truth-only values
 * coexist in one draft — the schema carries them all regardless of branch.
 *
 * `activeStepIndex` is generated in-range (0..5): the module clamps on
 * READ, so in-range values are exactly the ones the round trip must
 * preserve as an identity. `savedAtMs` and `usecaseId` are generated as
 * garbage on purpose — `writeLabelingJobDraft` must stamp both
 * (Requirement 1.4), and the assertions check the stamps, not these.
 */
const draftArb: fc.Arbitrary<LabelingJobDraft> = fc.record({
  version: fc.constant(LABELING_JOB_DRAFT_VERSION as 1),
  savedAtMs: fc.integer({ min: 0, max: 4_102_444_800_000 }),
  usecaseId: anyStringArb,
  activeStepIndex: fc.integer({ min: 0, max: 5 }),
  labelingBackend: fc.constantFrom('' as const, 'DDA' as const, 'GroundTruth' as const),
  jobName: anyStringArb,
  description: anyStringArb,
  datasetS3Uri: anyStringArb,
  maskPrefix: anyStringArb,
  taskTypeValue: fc.oneof(fc.constantFrom('', 'BoundingBox', 'Segmentation'), anyStringArb),
  workforceTypeValue: anyStringArb,
  labelCategories: anyStringArb,
  gtInstructions: anyStringArb,
  enableAutomatedLabeling: fc.boolean(),
  ddaLabels: fc.array(anyStringArb, { maxLength: 8 }),
  ddaInstructions: anyStringArb,
  selectedTeam: fc.option(
    fc.record({ teamId: anyStringArb, teamName: anyStringArb }),
    { nil: null }
  ),
  autoLabelEnabled: fc.boolean(),
  autoLabelModel: autoLabelModelArb,
  detectionPrompt: anyStringArb,
  fewShotEnabled: fc.boolean(),
  downscaleMaxEdge: fc.option(fc.integer({ min: 0, max: 100_000 }), { nil: null }),
  tokenBudget: fc.oneof(
    fc.integer({ min: 0, max: 10_000_000 }).map(String),
    anyStringArb
  ),
  skipVerification: fc.boolean(),
  skipVerificationModelId: anyStringArb,
  perLabelPrompts: fc.dictionary(perLabelKeyArb, anyStringArb, { maxKeys: 6 }),
  exampleRefs: fc.record({
    good: fc.array(exampleRefArb, { maxLength: 6 }),
    bad: fc.array(exampleRefArb, { maxLength: 6 }),
  }),
  previewSelectedKeys: fc.array(anyStringArb, { maxLength: 5 }),
  previewRun: fc.option(previewRunArb, { nil: null }),
});

// -------------------------------------------------------------- properties

/**
 * **Feature: labeling-setup-session-recovery, Property 1: Draft
 * serialization round-trips**
 *
 * For any `LabelingJobDraft` (arbitrary field values across both branches),
 * `writeLabelingJobDraft(usecaseId, draft)` followed by
 * `readLabelingJobDraft(usecaseId)` SHALL return a draft equivalent to the
 * written one — every Requirement 2.1 field, the verbatim model value, the
 * example refs, the sample selection, and the preview-run reference
 * preserved — carrying `version` 1 and the key's `usecaseId`.
 *
 * **Validates: Requirements 1.4, 2.1, 2.2, 2.4**
 */
describe('Feature: labeling-setup-session-recovery, Property 1: Draft serialization round-trips', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('write then read returns an equivalent draft stamped with version 1 and the key usecaseId', () => {
    fc.assert(
      fc.property(usecaseIdArb, draftArb, (usecaseId, draft) => {
        // Independent storage per run.
        window.localStorage.clear();

        const beforeWriteMs = Date.now();
        writeLabelingJobDraft(usecaseId, draft);
        // Read just after the write, so the 14-day staleness rule can
        // never trip on the freshly stamped savedAtMs.
        const nowJustAfterWriteMs = Date.now();
        const readBack = readLabelingJobDraft(usecaseId, nowJustAfterWriteMs);

        expect(readBack).not.toBeNull();
        if (readBack === null) {
          return;
        }

        // Write stamps the schema version, the save time, and the key's
        // Use_Case id — regardless of what the input draft carried
        // (Requirement 1.4).
        expect(readBack.version).toBe(LABELING_JOB_DRAFT_VERSION);
        expect(readBack.usecaseId).toBe(usecaseId);
        expect(readBack.savedAtMs).toBeGreaterThanOrEqual(beforeWriteMs);
        expect(readBack.savedAtMs).toBeLessThanOrEqual(nowJustAfterWriteMs);

        // Every Requirement 2.1 field, field by field (deep equality on
        // the whole normalized draft; savedAtMs/usecaseId are the stamps
        // asserted above).
        expect(readBack).toEqual({
          ...draft,
          version: LABELING_JOB_DRAFT_VERSION,
          usecaseId,
          savedAtMs: readBack.savedAtMs,
        });

        // The exported save-gate equivalence agrees (ignores savedAtMs).
        expect(draftsEquivalent(readBack, { ...draft, usecaseId })).toBe(true);

        // Explicit spot fields: the verbatim model value (Requirement
        // 2.2), the prompt and budget as entered, the in-range step index
        // (clamp on read is the identity for 0..5), and the
        // Sample_Selection plus Preview_Run_Reference (Requirement 2.4).
        expect(readBack.autoLabelModel).toBe(draft.autoLabelModel);
        expect(readBack.detectionPrompt).toBe(draft.detectionPrompt);
        expect(readBack.tokenBudget).toBe(draft.tokenBudget);
        expect(readBack.activeStepIndex).toBe(draft.activeStepIndex);
        expect(readBack.exampleRefs).toEqual(draft.exampleRefs);
        expect(readBack.previewSelectedKeys).toEqual(draft.previewSelectedKeys);
        expect(readBack.previewRun).toEqual(draft.previewRun);
      }),
      { numRuns: 100 }
    );
  });
});

// ----------------------------------------------- Property 2 generators

/** Read-time clocks across the epoch range the drafts use. */
const nowMsArb: fc.Arbitrary<number> = fc.integer({ min: 0, max: 4_102_444_800_000 });

/** One stored-content scenario: the Draft_Key's id, the read clock, the raw string. */
interface StoredContentScenario {
  keyUsecaseId: string;
  nowMs: number;
  raw: string;
  label: string;
}

/**
 * Arbitrary strings that are not JSON at all — `JSON.parse` throws on
 * them, so the read's tolerance is exercised at the parse step
 * (Requirement 6.2). The filter is deterministic: only strings that
 * actually fail to parse are kept (parsable ones belong to the
 * wrong-shape class below).
 */
const nonJsonScenarioArb: fc.Arbitrary<StoredContentScenario> = fc
  .record({
    keyUsecaseId: usecaseIdArb,
    nowMs: nowMsArb,
    raw: anyStringArb.filter((candidate) => {
      try {
        JSON.parse(candidate);
        return false;
      } catch {
        return true;
      }
    }),
  })
  .map((scenario) => ({ ...scenario, label: 'non-JSON string' }));

/**
 * Parseable JSON of the wrong shape: numbers, booleans, null, strings,
 * arrays, and foreign objects. Anything `conformingDraft` could ever
 * accept must be a non-array object carrying `version === 1`, so those
 * are excluded deterministically (an object that IS version 1 but has
 * broken fields is generated precisely by the corrupted-draft class).
 */
const wrongShapeJsonScenarioArb: fc.Arbitrary<StoredContentScenario> = fc
  .record({
    keyUsecaseId: usecaseIdArb,
    nowMs: nowMsArb,
    raw: fc
      .jsonValue()
      .filter(
        (value) =>
          typeof value !== 'object' ||
          value === null ||
          Array.isArray(value) ||
          (value as { version?: unknown }).version !== LABELING_JOB_DRAFT_VERSION
      )
      .map((value) => JSON.stringify(value)),
  })
  .map((scenario) => ({ ...scenario, label: 'wrong-shape JSON' }));

/**
 * Version stamps the module must reject: the check is strict (`!== 1`),
 * so numeric non-1 values, the string `'1'`, and a missing stamp
 * (`undefined` — dropped by JSON.stringify) are all unknown versions
 * (Requirement 6.2).
 */
const nonV1VersionArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.integer().filter((version) => version !== LABELING_JOB_DRAFT_VERSION),
  fc.constantFrom<unknown>('1', 0, 2, -1, 1.5, null, true, undefined, 'v1', [1], { value: 1 })
);

/**
 * Structurally valid drafts stamped with a version other than 1 — every
 * other field conforming, `usecaseId` matching the key, `savedAtMs`
 * fresh, so the wrong version is the only violation (Requirement 6.2).
 */
const wrongVersionScenarioArb: fc.Arbitrary<StoredContentScenario> = fc
  .record({ keyUsecaseId: usecaseIdArb, nowMs: nowMsArb, draft: draftArb, version: nonV1VersionArb })
  .map(({ keyUsecaseId, nowMs, draft, version }) => ({
    keyUsecaseId,
    nowMs,
    raw: JSON.stringify({ ...draft, usecaseId: keyUsecaseId, savedAtMs: nowMs, version }),
    label: 'version other than 1',
  }));

/**
 * Fully valid, fresh drafts whose `usecaseId` differs from the Draft_Key
 * they are stored under — the cross-use-case guard (Requirement 6.2).
 */
const mismatchedUsecaseScenarioArb: fc.Arbitrary<StoredContentScenario> = fc
  .record({
    ids: fc.tuple(usecaseIdArb, usecaseIdArb).filter(([keyId, draftId]) => keyId !== draftId),
    nowMs: nowMsArb,
    draft: draftArb,
  })
  .map(({ ids: [keyUsecaseId, draftUsecaseId], nowMs, draft }) => ({
    keyUsecaseId,
    nowMs,
    raw: JSON.stringify({
      ...draft,
      version: LABELING_JOB_DRAFT_VERSION,
      usecaseId: draftUsecaseId,
      savedAtMs: nowMs,
    }),
    label: 'usecaseId differing from the key',
  }));

/**
 * Per-field wrong-typed replacements, each guaranteed non-conforming for
 * its field (null stays out of the pools of null-accepting fields, valid
 * enum strings out of `labelingBackend`, numbers out of the raw-string
 * `tokenBudget`, and so on). Together with single-field deletion these
 * are the "objects with wrong fields" class (Requirement 6.2).
 */
const wrongTypedMutations: ReadonlyArray<readonly [keyof LabelingJobDraft, readonly unknown[]]> = [
  ['savedAtMs', ['1700000000000', null, true, [], {}]],
  ['usecaseId', [123, null, false, ['id'], {}]],
  ['activeStepIndex', ['3', null, true, [2], {}]],
  ['labelingBackend', ['dda', 'GROUNDTRUTH', 'DDA ', 7, null, true]],
  ['jobName', [42, null, true, ['name'], {}]],
  ['description', [0, null, false, {}]],
  ['datasetS3Uri', [1.5, null, true, []]],
  ['maskPrefix', [9, null, false, {}]],
  ['taskTypeValue', [3, null, true, []]],
  ['workforceTypeValue', [8, null, false, {}]],
  ['labelCategories', [2, null, true, []]],
  ['gtInstructions', [6, null, false, {}]],
  ['enableAutomatedLabeling', ['true', 1, 0, null, {}, []]],
  ['ddaLabels', ['label', 4, null, {}, [1], ['ok', null], [{}]]],
  ['ddaInstructions', [5, null, true, []]],
  ['selectedTeam', ['team', 7, true, [], {}, { teamId: 'id' }, { teamId: 1, teamName: 'n' }, { teamName: 'n' }]],
  ['autoLabelEnabled', ['false', 0, 1, null, []]],
  ['autoLabelModel', [11, null, true, ['sam']]],
  ['detectionPrompt', [13, null, false, {}]],
  ['fewShotEnabled', ['yes', 2, null, {}]],
  ['downscaleMaxEdge', ['768', true, [], {}]],
  ['tokenBudget', [4096, null, true, []]],
  ['skipVerification', ['no', 0, null, []]],
  ['skipVerificationModelId', [15, null, true, {}]],
  ['perLabelPrompts', ['prompts', 3, null, [], { label: 1 }, { label: null }]],
  ['exampleRefs', [null, 'refs', 6, [], {}, { good: [] }, { bad: [] }, { good: 's3://b/k', bad: [] }, { good: [], bad: [1] }]],
  ['previewSelectedKeys', ['key', 5, null, {}, [3], ['k', false]]],
  ['previewRun', ['run', 9, true, [], {}, { runId: 'r' }, { runId: 1, sampleCount: 2, startedAtMs: 3 }, { runId: 'r', sampleCount: '2', startedAtMs: 3 }]],
];

/** Every schema field is required, so deleting any single one is non-conforming. */
const deletableFieldArb: fc.Arbitrary<keyof LabelingJobDraft> = fc.constantFrom(
  'version',
  ...wrongTypedMutations.map(([field]) => field)
);

type DraftMutation =
  | { kind: 'delete'; field: keyof LabelingJobDraft }
  | { kind: 'replace'; field: keyof LabelingJobDraft; value: unknown };

const draftMutationArb: fc.Arbitrary<DraftMutation> = fc.oneof(
  deletableFieldArb.map((field): DraftMutation => ({ kind: 'delete', field })),
  fc
    .constantFrom(...wrongTypedMutations)
    .chain(([field, pool]) =>
      fc.constantFrom(...pool).map((value): DraftMutation => ({ kind: 'replace', field, value }))
    )
);

/**
 * Otherwise-valid, fresh, key-matching drafts with exactly one field
 * deleted or replaced by a wrong-typed value — the field-by-field shape
 * validation (Requirement 6.2).
 */
const corruptedDraftScenarioArb: fc.Arbitrary<StoredContentScenario> = fc
  .record({ keyUsecaseId: usecaseIdArb, nowMs: nowMsArb, draft: draftArb, mutation: draftMutationArb })
  .map(({ keyUsecaseId, nowMs, draft, mutation }) => {
    const stored: Record<string, unknown> = {
      ...draft,
      version: LABELING_JOB_DRAFT_VERSION,
      usecaseId: keyUsecaseId,
      savedAtMs: nowMs,
    };
    if (mutation.kind === 'delete') {
      delete stored[mutation.field];
    } else {
      stored[mutation.field] = mutation.value;
    }
    return {
      keyUsecaseId,
      nowMs,
      raw: JSON.stringify(stored),
      label: `field ${String(mutation.field)} ${mutation.kind === 'delete' ? 'deleted' : 'wrong-typed'}`,
    };
  });

/** The union of every invalid stored-content class Property 2 quantifies over. */
const invalidContentScenarioArb: fc.Arbitrary<StoredContentScenario> = fc.oneof(
  nonJsonScenarioArb,
  wrongShapeJsonScenarioArb,
  wrongVersionScenarioArb,
  mismatchedUsecaseScenarioArb,
  corruptedDraftScenarioArb
);

/**
 * Save-age offsets (`nowMs − savedAtMs`) straddling the
 * Draft_Staleness_Bound: the exact boundary and its immediate neighbors
 * on both sides, plus broad ranges — including negative offsets (a
 * `savedAtMs` in the future is within the bound). "Older than" is
 * strict, so an offset of exactly DRAFT_STALENESS_MS is still fresh
 * (Requirement 6.3).
 */
const staleOffsetMsArb: fc.Arbitrary<number> = fc.oneof(
  fc.constantFrom(0, 1, -1, DRAFT_STALENESS_MS - 1, DRAFT_STALENESS_MS, DRAFT_STALENESS_MS + 1),
  fc.integer({ min: -DRAFT_STALENESS_MS, max: 3 * DRAFT_STALENESS_MS })
);

/**
 * **Feature: labeling-setup-session-recovery, Property 2: Reading
 * tolerates anything and purges stale drafts**
 *
 * For any stored content under a Draft_Key — an arbitrary string,
 * arbitrary JSON of the wrong shape, a structurally valid draft with a
 * version other than 1, a valid draft whose `usecaseId` differs from the
 * key's, or a valid draft whose `savedAtMs` is older than the
 * Draft_Staleness_Bound — `readLabelingJobDraft` SHALL return null
 * without throwing; and for any valid draft with `savedAtMs` within the
 * bound it SHALL return the draft, while a staler one SHALL additionally
 * be removed from the Draft_Store.
 *
 * **Validates: Requirements 6.2, 6.3, 6.5**
 */
describe('Feature: labeling-setup-session-recovery, Property 2: Reading tolerates anything and purges stale drafts', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('returns null without throwing for any invalid stored content', () => {
    fc.assert(
      fc.property(invalidContentScenarioArb, ({ keyUsecaseId, nowMs, raw }) => {
        // Independent storage per run.
        window.localStorage.clear();
        window.localStorage.setItem(labelingJobDraftKey(keyUsecaseId), raw);

        // Tolerant read: absence, never an exception (Req 6.2, 6.5).
        let readBack: LabelingJobDraft | null = null;
        expect(() => {
          readBack = readLabelingJobDraft(keyUsecaseId, nowMs);
        }).not.toThrow();
        expect(readBack).toBeNull();
      }),
      { numRuns: 100 }
    );
  });

  it('returns a draft saved within the staleness bound and purges a staler one', () => {
    fc.assert(
      fc.property(
        usecaseIdArb,
        draftArb,
        nowMsArb,
        staleOffsetMsArb,
        (usecaseId, draft, nowMs, offsetMs) => {
          // Independent storage per run.
          window.localStorage.clear();
          const key = labelingJobDraftKey(usecaseId);
          const savedAtMs = nowMs - offsetMs;
          // Stored directly (not via writeLabelingJobDraft, which would
          // re-stamp savedAtMs with the real clock): a fully valid v1
          // draft whose only variable is its age relative to nowMs.
          window.localStorage.setItem(
            key,
            JSON.stringify({
              ...draft,
              version: LABELING_JOB_DRAFT_VERSION,
              usecaseId,
              savedAtMs,
            })
          );

          let readBack: LabelingJobDraft | null = null;
          expect(() => {
            readBack = readLabelingJobDraft(usecaseId, nowMs);
          }).not.toThrow();

          if (offsetMs <= DRAFT_STALENESS_MS) {
            // Within the Draft_Staleness_Bound: the draft comes back
            // intact and stays stored.
            expect(readBack).toEqual({
              ...draft,
              version: LABELING_JOB_DRAFT_VERSION,
              usecaseId,
              savedAtMs,
            });
            expect(window.localStorage.getItem(key)).not.toBeNull();
          } else {
            // Older than the bound: treated as absent AND removed from
            // the Draft_Store (Requirement 6.3).
            expect(readBack).toBeNull();
            expect(window.localStorage.getItem(key)).toBeNull();
          }
        }
      ),
      { numRuns: 100 }
    );
  });

  it('reports absence instead of throwing when the Draft_Store itself fails', () => {
    fc.assert(
      fc.property(usecaseIdArb, (usecaseId) => {
        // jsdom's localStorage methods live on Storage.prototype, so the
        // spy makes every getItem call throw (storage disabled/denied).
        const getItemSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
          throw new Error('storage unavailable');
        });
        try {
          let readBack: LabelingJobDraft | null = null;
          expect(() => {
            readBack = readLabelingJobDraft(usecaseId, Date.now());
          }).not.toThrow();
          expect(readBack).toBeNull();
        } finally {
          getItemSpy.mockRestore();
        }
      }),
      { numRuns: 100 }
    );
  });
});
