/**
 * Property-based tests for the grounded-sam-autolabel additions to
 * `labelingJobDraft.ts` — the additive-optional
 * `groundedSamPromptOverrides?: Record<string, string>` draft field:
 *
 * - Property 13: Drafts round-trip the overrides and the save gate
 *   discriminates on them (Requirements 6.1, 6.5)
 * - Property 14: Draft reading tolerates the field's absence and rejects
 *   its malformation (Requirements 6.3, 6.4)
 *
 * Lives in its own file so the pinned pre-feature suite
 * (`labelingJobDraft.storage.property.test.ts`) stays byte-identical —
 * the design's non-regression rule. Follows that suite's conventions:
 * fast-check `{ numRuns: 100 }`, jsdom localStorage cleared inside every
 * property run, and generators drawing the literal key `'__proto__'`
 * with weight, since `JSON.parse` surfaces it as an own property and the
 * module's `asStringRecord` normalizer must keep it as one.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import {
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
 * Free-text values: empty, whitespace-only, padded, unicode (graphemes
 * incl. emoji / non-BMP), and plain ASCII — override prompts must carry
 * all of them verbatim (Requirement 6.1).
 */
const anyStringArb: fc.Arbitrary<string> = fc.oneof(
  fc.string(),
  fc.string({ unit: 'grapheme' }),
  fc.constantFrom('', ' ', '\t \n', '  padded  ', 'ünïcode 日本語 🙂', 'prompt/with "quotes" \\ and \n newline')
);

/** The Draft_Key's Use_Case id — any non-empty string, uuid-shaped or not. */
const usecaseIdArb: fc.Arbitrary<string> = fc.oneof(
  fc.uuid(),
  anyStringArb.filter((s) => s.length > 0)
);

/** Read-time clocks across the epoch range the drafts use. */
const nowMsArb: fc.Arbitrary<number> = fc.integer({ min: 0, max: 4_102_444_800_000 });

/** A Preview_Run_Reference, needed only to build complete valid drafts. */
const previewRunArb: fc.Arbitrary<PreviewRunReference> = fc.record({
  runId: fc.oneof(fc.uuid(), anyStringArb),
  sampleCount: fc.integer({ min: 0, max: 500 }),
  startedAtMs: fc.integer({ min: 0, max: 4_102_444_800_000 }),
});

/**
 * Override keys are user-entered DDA label names — any text, explicitly
 * including the literal key `'__proto__'` (drawn with weight so every
 * run set exercises it), per the module's existing `perLabelPrompts`
 * precedent: the value must survive normalization as an own data
 * property instead of routing through `Object.prototype`'s setter.
 */
const overrideKeyArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 9, arbitrary: anyStringArb },
  { weight: 1, arbitrary: fc.constant('__proto__') }
);

/**
 * Arbitrary Prompt_Override maps (Requirement 6.1): the empty map and a
 * guaranteed `__proto__`-keyed entry are drawn explicitly so neither
 * depends on the dictionary generator's whims. The `__proto__` branch is
 * built with `Object.fromEntries` (CreateDataProperty semantics — an own
 * key, exactly what `JSON.parse` of a stored draft produces).
 */
const overridesMapArb: fc.Arbitrary<Record<string, string>> = fc.oneof(
  { weight: 8, arbitrary: fc.dictionary(overrideKeyArb, anyStringArb, { maxKeys: 6 }) },
  { weight: 1, arbitrary: fc.constant<Record<string, string>>({}) },
  {
    weight: 1,
    arbitrary: anyStringArb.map((value) =>
      Object.fromEntries([['__proto__', value] as [string, string]])
    ),
  }
);

/**
 * Complete valid drafts WITHOUT the `groundedSamPromptOverrides` field —
 * the field is attached per scenario, so each property controls its
 * presence exactly. Mirrors the pinned suite's `draftArb`: every field
 * drawn independently, `activeStepIndex` in-range (clamp on read is the
 * identity for 0..5), `savedAtMs`/`usecaseId` garbage on purpose (write
 * stamps them; direct-stored scenarios override them explicitly).
 */
const baseDraftArb: fc.Arbitrary<LabelingJobDraft> = fc.record({
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
  autoLabelModel: fc.oneof(
    fc.constantFrom('', 'sam', 'grounded-sam'),
    anyStringArb.map((s) => `bedrock:${s}`),
    anyStringArb.map((s) => `llm:${s}`),
    anyStringArb
  ),
  detectionPrompt: anyStringArb,
  fewShotEnabled: fc.boolean(),
  downscaleMaxEdge: fc.option(fc.integer({ min: 0, max: 100_000 }), { nil: null }),
  tokenBudget: fc.oneof(fc.integer({ min: 0, max: 10_000_000 }).map(String), anyStringArb),
  skipVerification: fc.boolean(),
  skipVerificationModelId: anyStringArb,
  perLabelPrompts: fc.dictionary(overrideKeyArb, anyStringArb, { maxKeys: 6 }),
  exampleRefs: fc.record({
    good: fc.array(anyStringArb, { maxLength: 6 }),
    bad: fc.array(anyStringArb, { maxLength: 6 }),
  }),
  previewSelectedKeys: fc.array(anyStringArb, { maxLength: 5 }),
  previewRun: fc.option(previewRunArb, { nil: null }),
});

// --------------------------------------------------------------- helpers

/**
 * String-record equality with own-key semantics — the oracle mirror of
 * the module's save-gate comparison. `hasOwnProperty.call` keeps a
 * literal `__proto__` key honest on both sides.
 */
function stringRecordsEqual(a: Record<string, string>, b: Record<string, string>): boolean {
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  return (
    aKeys.length === bKeys.length &&
    aKeys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && a[key] === b[key])
  );
}

// ---------------------------------------------------------- Property 13

/** The field's state on one side of a save-gate comparison: absent or a map. */
type OverridesState = Record<string, string> | undefined;
type OverridesPair = [OverridesState, OverridesState];

const overridesStateArb: fc.Arbitrary<OverridesState> = fc.option(overridesMapArb, {
  nil: undefined,
});

/**
 * Pairs of field states for the save-gate half of Property 13: two
 * independent draws (mostly differing), a structural copy (guaranteed
 * equal, so the discriminates-true branch is always exercised), and the
 * absent-vs-empty pair (equal under the gate's `?? {}` rule — absence ≡
 * zero entries, Requirement 6.5 via 6.3's zero-override restore).
 */
const overridesPairArb: fc.Arbitrary<OverridesPair> = fc.oneof(
  fc.tuple(overridesStateArb, overridesStateArb),
  overridesStateArb.map(
    (state): OverridesPair => [
      state,
      state === undefined ? undefined : Object.fromEntries(Object.entries(state)),
    ]
  ),
  fc.constantFrom<OverridesPair>([undefined, {}], [{}, undefined])
);

/**
 * **Feature: grounded-sam-autolabel, Property 13: Drafts round-trip the
 * overrides and the save gate discriminates on them**
 *
 * For any Setup_Draft carrying any `groundedSamPromptOverrides` map
 * (including entries keyed `__proto__`, unicode values, and the empty
 * map), writing then reading the draft SHALL return the map exactly; and
 * for any two drafts identical except for that field, `draftsEquivalent`
 * SHALL hold exactly when the two maps are equal.
 *
 * **Validates: Requirements 6.1, 6.5**
 */
describe('Feature: grounded-sam-autolabel, Property 13: Drafts round-trip the overrides and the save gate discriminates on them', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('write then read returns the override map exactly, alongside every other field', () => {
    fc.assert(
      fc.property(usecaseIdArb, baseDraftArb, overridesMapArb, (usecaseId, base, overrides) => {
        // Independent storage per run.
        window.localStorage.clear();

        const draft: LabelingJobDraft = { ...base, groundedSamPromptOverrides: overrides };
        writeLabelingJobDraft(usecaseId, draft);
        // Read just after the write, so the staleness rule can never
        // trip on the freshly stamped savedAtMs.
        const nowJustAfterWriteMs = Date.now();
        const readBack = readLabelingJobDraft(usecaseId, nowJustAfterWriteMs);

        expect(readBack).not.toBeNull();
        if (readBack === null) {
          return;
        }

        // The field written present comes back present (own key) and
        // string-record-equal — `__proto__` entries included
        // (Requirement 6.1, 6.5 round trip).
        expect(Object.prototype.hasOwnProperty.call(readBack, 'groundedSamPromptOverrides')).toBe(
          true
        );
        expect(stringRecordsEqual(readBack.groundedSamPromptOverrides ?? {}, overrides)).toBe(true);

        // The whole normalized draft round-trips alongside it
        // (savedAtMs/usecaseId are the write's stamps).
        expect(readBack).toEqual({
          ...draft,
          version: LABELING_JOB_DRAFT_VERSION,
          usecaseId,
          savedAtMs: readBack.savedAtMs,
        });

        // The exported save gate agrees the read-back equals the written
        // state (ignores savedAtMs).
        expect(draftsEquivalent(readBack, { ...draft, usecaseId })).toBe(true);
      }),
      { numRuns: 100 }
    );
  });

  it('draftsEquivalent discriminates exactly on override-map equality for otherwise-identical drafts', () => {
    fc.assert(
      fc.property(baseDraftArb, overridesPairArb, (base, [stateA, stateB]) => {
        // Two drafts identical except the groundedSamPromptOverrides
        // field (absent when the drawn state is undefined).
        const a: LabelingJobDraft = {
          ...base,
          ...(stateA !== undefined ? { groundedSamPromptOverrides: stateA } : {}),
        };
        const b: LabelingJobDraft = {
          ...base,
          ...(stateB !== undefined ? { groundedSamPromptOverrides: stateB } : {}),
        };

        // Oracle: the gate's own rule — absent ≡ empty, else own-key
        // string-record equality (Requirement 6.5).
        const mapsEqual = stringRecordsEqual(stateA ?? {}, stateB ?? {});
        expect(draftsEquivalent(a, b)).toBe(mapsEqual);
        expect(draftsEquivalent(b, a)).toBe(mapsEqual);
      }),
      { numRuns: 100 }
    );
  });
});

// ---------------------------------------------------------- Property 14

/**
 * Non-conforming replacements for the field's value — anything but an
 * object mapping strings to strings: non-objects (strings, numbers,
 * booleans, null), arrays, and records carrying at least one non-string
 * value. Every value survives a JSON round trip as itself (or, for
 * non-finite numbers, as null), so the stored draft is malformed exactly
 * as generated.
 */
const nonStringValueArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.integer(),
  fc.boolean(),
  fc.constant(null),
  fc.array(anyStringArb, { maxLength: 2 }),
  fc.constant({})
);

const malformedOverridesArb: fc.Arbitrary<unknown> = fc.oneof(
  // Non-objects and arrays.
  fc.constantFrom<unknown>(
    'overrides',
    '',
    0,
    7,
    1.5,
    -1,
    true,
    false,
    null,
    [],
    ['prompt'],
    [{ label: 'p' }],
    { label: 42 },
    { label: null },
    { good: 'ok', bad: 2 },
    { nested: {} }
  ),
  // A record with at least one non-string value among conforming
  // entries (the bad entry is inserted last, so its key is guaranteed
  // to carry the non-string value; Object.fromEntries keeps a literal
  // `__proto__` bad key an own property).
  fc
    .tuple(overridesMapArb, overrideKeyArb, nonStringValueArb)
    .map(([goodEntries, badKey, badValue]) =>
      Object.fromEntries([...Object.entries(goodEntries), [badKey, badValue] as [string, unknown]])
    )
);

/**
 * **Feature: grounded-sam-autolabel, Property 14: Draft reading
 * tolerates the field's absence and rejects its malformation**
 *
 * For any otherwise-conforming stored draft, removing the
 * `groundedSamPromptOverrides` key SHALL leave the draft readable
 * (non-null, field absent, restoring as zero overrides), and replacing
 * the key's value with any non-conforming shape SHALL make the read
 * report no draft — never an exception.
 *
 * **Validates: Requirements 6.3, 6.4**
 */
describe("Feature: grounded-sam-autolabel, Property 14: Draft reading tolerates the field's absence and rejects its malformation", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('a stored draft whose overrides key was removed reads back non-null with the field absent', () => {
    fc.assert(
      fc.property(
        usecaseIdArb,
        baseDraftArb,
        overridesMapArb,
        nowMsArb,
        (usecaseId, base, overrides, nowMs) => {
          // Independent storage per run.
          window.localStorage.clear();
          const key = labelingJobDraftKey(usecaseId);

          // An otherwise-conforming, fresh, key-matching stored draft
          // that carried the field — then the key is removed from the
          // stored JSON (the pre-feature draft shape, Requirement 6.3).
          const stored: Record<string, unknown> = {
            ...base,
            version: LABELING_JOB_DRAFT_VERSION,
            usecaseId,
            savedAtMs: nowMs,
            groundedSamPromptOverrides: overrides,
          };
          delete stored.groundedSamPromptOverrides;
          window.localStorage.setItem(key, JSON.stringify(stored));

          let readResult: LabelingJobDraft | null = null;
          expect(() => {
            readResult = readLabelingJobDraft(usecaseId, nowMs);
          }).not.toThrow();
          // Re-widen: TS keeps the closure-assigned variable narrowed to
          // its initializer, so property access below needs the union.
          const readBack = readResult as LabelingJobDraft | null;

          // Readable: the absent field is not malformation.
          expect(readBack).not.toBeNull();
          if (readBack === null) {
            return;
          }

          // Absence preserved as absence — no `{}` injected into the
          // normalized draft (the load-bearing detail keeping
          // pre-feature drafts round-tripping byte-identically).
          expect(
            Object.prototype.hasOwnProperty.call(readBack, 'groundedSamPromptOverrides')
          ).toBe(false);
          expect(readBack.groundedSamPromptOverrides).toBeUndefined();

          // The read-site default restores zero override entries.
          expect(Object.keys(readBack.groundedSamPromptOverrides ?? {})).toHaveLength(0);

          // Every other field restores exactly as stored.
          expect(readBack).toEqual({
            ...base,
            version: LABELING_JOB_DRAFT_VERSION,
            usecaseId,
            savedAtMs: nowMs,
          });
        }
      ),
      { numRuns: 100 }
    );
  });

  it('a stored draft whose overrides value is mangled reads null and never throws', () => {
    fc.assert(
      fc.property(
        usecaseIdArb,
        baseDraftArb,
        malformedOverridesArb,
        nowMsArb,
        (usecaseId, base, malformed, nowMs) => {
          // Independent storage per run.
          window.localStorage.clear();
          const key = labelingJobDraftKey(usecaseId);

          // Otherwise fully valid, fresh, key-matching — the mangled
          // field is the only violation (Requirement 6.4).
          const stored: Record<string, unknown> = {
            ...base,
            version: LABELING_JOB_DRAFT_VERSION,
            usecaseId,
            savedAtMs: nowMs,
            groundedSamPromptOverrides: malformed,
          };
          window.localStorage.setItem(key, JSON.stringify(stored));

          // Present-but-malformed: the whole stored content is treated
          // as no draft, tolerantly (Requirement 6.4).
          let readBack: LabelingJobDraft | null = null;
          expect(() => {
            readBack = readLabelingJobDraft(usecaseId, nowMs);
          }).not.toThrow();
          expect(readBack).toBeNull();
        }
      ),
      { numRuns: 100 }
    );
  });
});
