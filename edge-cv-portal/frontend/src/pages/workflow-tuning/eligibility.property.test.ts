/**
 * **Feature: quality-prompt-tuning, Property 1: Tunable classification is one
 * function used everywhere** — **Validates: Requirements 1.5**
 *
 * *For any* node type and any `anomaly_mode` value (absent, null, true, false,
 * truthy/falsy strings), `workflow_core.anomaly_invocation.is_tunable_node`,
 * the Portal frontend's `isTunableNode`, and the executor's anomaly-mode
 * decision SHALL agree, returning true exactly for `bedrock_inference` with
 * `anomaly_mode` absent/null/true and for `llm_inference` with `anomaly_mode`
 * true.
 *
 * Scope of the half asserted here
 * -------------------------------
 *
 * This is the **Portal frontend half** (spec task 8.5). Its oracle is the case
 * table published by the shared-module half (task 1.4,
 * `edge-cv-portal/backend/tests/test_property_anomaly_invocation_eligibility.py`)
 * as `edge-cv-portal/backend/tests/fixtures/anomaly_tuning_eligibility_cases.json`:
 * every `expectedTunable` in that file was computed in **Python** from an
 * independent restatement of Requirement 1.5 and of the executor's decision, so
 * asserting the frontend against it is what makes "one function used
 * everywhere" a real cross-language claim rather than two copies of one bug.
 * The fixture is read from disk (not vendored) so a table change cannot drift
 * apart from this half.
 *
 * Beyond the table's domain the fixture's own `coercionContract` is restated
 * here — locally, never imported from the module under test — so drawn node
 * types and drawn `anomaly_mode` values that the table does not enumerate are
 * still checked. Containers (arrays/objects) are deliberately NOT generated:
 * the fixture's notes record that their truth differs between Python (`[]` is
 * falsy) and JavaScript (`[]` is truthy), so they are covered by the Python
 * half only and no cross-language claim is made about them here.
 *
 * Harness: pure values only — no rendering, no API, no AWS.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import {
  ANOMALY_MODE_PARAMETER,
  NODE_TYPE_BEDROCK_INFERENCE,
  NODE_TYPE_LLM_INFERENCE,
  coerceParameterValue,
  definitionHasTunableNode,
  isAnomalyMode,
  isTunableNode,
  isTunableWorkflowNode,
  tunableNodeIds,
} from './eligibility';

// ------------------------------------------------------------------ fixture

/** One published `(node type, anomaly_mode)` case and its expectation. */
interface EligibilityCase {
  id: string;
  nodeType: string;
  nodeTypeNote: string;
  anomalyModeId: string;
  /** False means the parameter is absent from the node's parameters. */
  anomalyModePresent: boolean;
  /** The stored value; meaningless when `anomalyModePresent` is false. */
  anomalyMode: unknown;
  anomalyModeNote: string;
  expectedTunable: boolean;
}

interface EligibilityFixture {
  feature: string;
  property: number;
  propertyText: string;
  validates: string[];
  generatedBy: string;
  consumedBy: string[];
  inspectionNodeTypes: string[];
  coercionContract: string[];
  notes: string[];
  cases: EligibilityCase[];
}

/**
 * The shared case table, read from the Portal backend's fixtures directory —
 * the single file both halves assert. A missing/moved file fails the suite
 * loudly rather than silently degrading to a frontend-only check.
 */
const FIXTURE_RELATIVE_PATH = path.join(
  'backend',
  'tests',
  'fixtures',
  'anomaly_tuning_eligibility_cases.json'
);

/**
 * The fixture lives in the Portal *backend*'s tests, outside this package, so
 * it is located by walking up from the working directory (vitest runs with
 * `edge-cv-portal/frontend` as its root) until the `edge-cv-portal` directory
 * that holds it is found. `import.meta.url` is not usable here: under vite's
 * transform it resolves to an `http://localhost/@fs/...` URL, not a file path.
 */
function resolveFixturePath(): string {
  let directory = process.cwd();
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = path.join(directory, FIXTURE_RELATIVE_PATH);
    if (existsSync(candidate)) {
      return candidate;
    }
    const parent = path.dirname(directory);
    if (parent === directory) {
      break;
    }
    directory = parent;
  }
  throw new Error(
    `cannot find the shared eligibility case table (${FIXTURE_RELATIVE_PATH}) ` +
      `above ${process.cwd()}; it is published by ` +
      'edge-cv-portal/backend/tests/test_property_anomaly_invocation_eligibility.py'
  );
}

const FIXTURE_PATH = resolveFixturePath();

const FIXTURE: EligibilityFixture = JSON.parse(
  readFileSync(FIXTURE_PATH, 'utf-8')
) as EligibilityFixture;

const CASES: readonly EligibilityCase[] = FIXTURE.cases;

// -------------------------------------------------------------- restatement

/** "the `anomaly_mode` parameter is not present at all". */
const ABSENT = Symbol('anomaly_mode absent');

type RawMode = unknown | typeof ABSENT;

/**
 * The fixture's coercion contract, restated: trim and lowercase to recognise
 * `true`/`false`, else read an integer, else a decimal number, else keep the
 * string unchanged (the ORIGINAL string, not the trimmed one). Non-strings are
 * never coerced. Written table-first here so it is a second statement of the
 * rule and not a paraphrase of `eligibility.ts`.
 */
const LITERALS: Readonly<Record<string, boolean>> = { true: true, false: false };
const INTEGER_TEXT = /^[+-]?[0-9]+$/;
const DECIMAL_TEXT = /^[+-]?([0-9]+\.[0-9]*|\.[0-9]+)$/;

function refCoerce(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  const literal = LITERALS[trimmed.toLowerCase()];
  if (literal !== undefined) return literal;
  if (INTEGER_TEXT.test(trimmed)) return Number.parseInt(trimmed, 10);
  if (trimmed.includes('.') && DECIMAL_TEXT.test(trimmed)) return Number.parseFloat(trimmed);
  return value;
}

/**
 * Truth of a coerced value per the contract: booleans as themselves; `0`, `-0`
 * and `0.0` false and every other number true; the empty string false and every
 * other string — whitespace-only included — true; `null`/absent false.
 * Containers are outside the shared contract and are refused outright.
 */
function refTruthy(value: unknown): boolean {
  switch (typeof value) {
    case 'boolean':
      return value;
    case 'number':
      return value !== 0;
    case 'string':
      return value.length > 0;
    case 'undefined':
      return false;
    case 'object':
      if (value === null) return false;
      throw new Error('containers are out of the shared table\'s domain');
    default:
      throw new Error(`unsupported value type: ${typeof value}`);
  }
}

/** Requirement 1.5, restated over the RAW value (`ABSENT` for no parameter). */
function refIsTunable(nodeType: unknown, raw: RawMode): boolean {
  const coerced = raw === ABSENT ? null : refCoerce(raw);
  if (nodeType === NODE_TYPE_BEDROCK_INFERENCE) {
    // Absent (and a stored null) default to Anomaly_Mode.
    return coerced === null || coerced === undefined ? true : refTruthy(coerced);
  }
  if (nodeType === NODE_TYPE_LLM_INFERENCE) {
    return refTruthy(coerced);
  }
  return false;
}

// ------------------------------------------------------------- arbitraries

/** Node-type strings: the table's, the near misses, degenerate values, text. */
const nodeTypeArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.constantFrom(...new Set(CASES.map((one) => one.nodeType))),
  fc.constantFrom(
    'BEDROCK_INFERENCE',
    'bedrock',
    'llm',
    ' llm_inference',
    'llm_inference\n',
    'bedrock_inference.v2',
    'LLM_Inference',
    'model_inference',
    ''
  ),
  fc.string({ maxLength: 24 }),
  fc.constant(null),
  fc.constant(undefined)
);

/**
 * `anomaly_mode` values: the table's own values plus scalars beyond it
 * (numbers, whitespace, sign/exponent-shaped text, free text). No containers —
 * see the file docstring.
 */
const anomalyModeArb: fc.Arbitrary<RawMode> = fc.oneof(
  fc.constantFrom<RawMode[]>(
    ...(CASES.filter((one) => one.anomalyModePresent).map(
      (one) => one.anomalyMode
    ) as RawMode[])
  ),
  fc.constant(ABSENT),
  fc.constant(null),
  fc.boolean(),
  fc.integer({ min: -5, max: 5 }),
  fc.double({ noDefaultInfinity: false, noNaN: false }),
  fc.constantFrom(
    '1_000',
    '0x0',
    '1e3',
    '0e0',
    'Infinity',
    'nan',
    'inf',
    'true\n',
    '\tFALSE\t',
    '+1',
    '-0.0',
    '.5',
    '00',
    'TRUE FALSE',
    'y',
    'n'
  ),
  fc.string({ maxLength: 16 })
);

/** The published cases, drawn one at a time. */
const caseArb: fc.Arbitrary<EligibilityCase> = fc.constantFrom(...CASES);

/** Values the rule's decisions turn on; every example is checked on these. */
const SENSITIVE_VALUES: readonly RawMode[] = [
  ABSENT,
  null,
  true,
  false,
  'true',
  'false',
  'True',
  'FALSE',
  ' true ',
  '  false  ',
  '0',
  '1',
  '0.0',
  '1.5',
  '',
  ' ',
  'no',
  0,
  1,
  0.0,
];

/** Node types every example is checked against, whatever it drew. */
const SENSITIVE_NODE_TYPES: readonly unknown[] = [
  NODE_TYPE_BEDROCK_INFERENCE,
  NODE_TYPE_LLM_INFERENCE,
  'model_inference',
  '',
  null,
  undefined,
];

// ------------------------------------------------------------------- checks

/** Build the parameters record the Portal surfaces read the value off. */
function parametersFor(raw: RawMode): Record<string, unknown> {
  return raw === ABSENT ? {} : { [ANOMALY_MODE_PARAMETER]: raw };
}

/**
 * Assert the property for one `(node type, anomaly_mode)` pair: the function,
 * its executor-facing name, and both node/definition-level readers agree with
 * the restatement, are total and boolean-valued, and are pure.
 */
function checkOne(nodeType: unknown, raw: RawMode, expected: boolean): void {
  const parameters = parametersFor(raw);
  const frozen = JSON.stringify(Object.entries(parameters).map(([k, v]) => [k, String(v)]));
  const where = `nodeType=${String(nodeType)} anomaly_mode=${
    raw === ABSENT ? '<absent>' : JSON.stringify(raw) ?? String(raw)
  }`;

  const tunable = isTunableNode(nodeType, parameters[ANOMALY_MODE_PARAMETER]);
  expect(tunable, where).toBe(expected);
  expect(typeof tunable, where).toBe('boolean');

  // The executor-facing name is the same decision, not a second rule.
  expect(isAnomalyMode(nodeType, parameters[ANOMALY_MODE_PARAMETER]), where).toBe(tunable);

  // Read off a node and off a definition: the same answer, so a designer
  // canvas node, a stored definition node and a bare pair never disagree.
  const node = { id: 'n1', type: nodeType, parameters };
  expect(isTunableWorkflowNode(node), where).toBe(tunable);
  expect(definitionHasTunableNode({ nodes: [node] }), where).toBe(tunable);
  expect(tunableNodeIds({ nodes: [node] }), where).toEqual(tunable ? ['n1'] : []);

  // Only Inspection_Nodes can ever be tunable (1.5's "every other node").
  if (nodeType !== NODE_TYPE_BEDROCK_INFERENCE && nodeType !== NODE_TYPE_LLM_INFERENCE) {
    expect(tunable, where).toBe(false);
  }

  // Coercion is the executor's: a value's string form classifies exactly like
  // the value it coerces to.
  expect(isTunableNode(nodeType, coerceParameterValue(parameters[ANOMALY_MODE_PARAMETER])), where).toBe(
    tunable
  );

  // Pure and deterministic: no state, no mutation of the caller's parameters.
  expect(isTunableNode(nodeType, parameters[ANOMALY_MODE_PARAMETER]), where).toBe(tunable);
  expect(JSON.stringify(Object.entries(parameters).map(([k, v]) => [k, String(v)])), where).toBe(
    frozen
  );
}

/** The raw value a published case presents to the readers. */
function rawOf(one: EligibilityCase): RawMode {
  return one.anomalyModePresent ? (one.anomalyMode as RawMode) : ABSENT;
}

// -------------------------------------------------------------- Property 1

describe('Property 1: Tunable classification is one function used everywhere (Portal half)', () => {
  it('agrees with the shared Python case table and with the restated rule beyond it', () => {
    fc.assert(
      fc.property(caseArb, nodeTypeArb, anomalyModeArb, (published, nodeType, raw) => {
        // 1. The drawn published case: the frontend answers exactly what the
        //    Python half computed for it.
        checkOne(published.nodeType, rawOf(published), published.expectedTunable);

        // 2. The drawn pair, and the drawn value/type crossed with the
        //    sensitive sets, against the restated rule — so one example covers
        //    the decision surface instead of a single point of it.
        checkOne(nodeType, raw, refIsTunable(nodeType, raw));
        for (const otherType of SENSITIVE_NODE_TYPES) {
          checkOne(otherType, raw, refIsTunable(otherType, raw));
        }
        for (const otherValue of SENSITIVE_VALUES) {
          checkOne(nodeType, otherValue, refIsTunable(nodeType, otherValue));
          checkOne(NODE_TYPE_BEDROCK_INFERENCE, otherValue, refIsTunable(NODE_TYPE_BEDROCK_INFERENCE, otherValue));
          checkOne(NODE_TYPE_LLM_INFERENCE, otherValue, refIsTunable(NODE_TYPE_LLM_INFERENCE, otherValue));
        }

        // 3. The two Inspection_Node types differ only on absent/null/false,
        //    and only in the documented direction (Bedrock defaults on).
        expect(isTunableNode(NODE_TYPE_BEDROCK_INFERENCE, null)).toBe(true);
        expect(isTunableNode(NODE_TYPE_LLM_INFERENCE, null)).toBe(false);
      }),
      { numRuns: 100 }
    );
  });
});

// ------------------------------------------------- the shared table's guards

describe('the shared eligibility case table', () => {
  it('is the table this half is meant to consume', () => {
    expect(FIXTURE.feature).toBe('quality-prompt-tuning');
    expect(FIXTURE.property).toBe(1);
    expect(FIXTURE.validates).toContain('1.5');
    expect(FIXTURE.consumedBy).toContain(
      'edge-cv-portal/frontend/src/pages/workflow-tuning/eligibility.property.test.ts'
    );
    expect(FIXTURE.inspectionNodeTypes).toEqual([
      NODE_TYPE_BEDROCK_INFERENCE,
      NODE_TYPE_LLM_INFERENCE,
    ]);
    expect(CASES.length).toBeGreaterThanOrEqual(100);
    expect(new Set(CASES.map((one) => one.id)).size).toBe(CASES.length);
  });

  it('agrees with the frontend rule on EVERY published case, not just sampled ones', () => {
    const disagreements = CASES.filter(
      (one) => isTunableNode(one.nodeType, one.anomalyModePresent ? one.anomalyMode : null) !== one.expectedTunable
    ).map((one) => one.id);
    expect(disagreements).toEqual([]);
  });

  it('agrees with the locally restated rule on EVERY published case', () => {
    // The restatement is what extends the table to undrawn values, so it must
    // reproduce the Python expectations wherever the table has an opinion.
    const disagreements = CASES.filter(
      (one) => refIsTunable(one.nodeType, rawOf(one)) !== one.expectedTunable
    ).map((one) => one.id);
    expect(disagreements).toEqual([]);
  });

  it('covers the shapes Property 1 names, on both Inspection_Node types', () => {
    const idsOf = (nodeType: string, expected: boolean) =>
      new Set(
        CASES.filter((one) => one.nodeType === nodeType && one.expectedTunable === expected).map(
          (one) => one.anomalyModeId
        )
      );
    const contains = (have: Set<string>, want: readonly string[]) =>
      want.filter((one) => !have.has(one));

    expect(
      contains(idsOf(NODE_TYPE_BEDROCK_INFERENCE, true), [
        'absent',
        'null',
        'true',
        'str-true',
        'str-True',
      ])
    ).toEqual([]);
    expect(
      contains(idsOf(NODE_TYPE_BEDROCK_INFERENCE, false), [
        'false',
        'str-false',
        'str-FALSE',
        'str-0',
        'str-empty',
      ])
    ).toEqual([]);
    expect(contains(idsOf(NODE_TYPE_LLM_INFERENCE, true), ['true', 'str-true', 'str-1'])).toEqual(
      []
    );
    expect(
      contains(idsOf(NODE_TYPE_LLM_INFERENCE, false), ['absent', 'null', 'false', 'str-empty'])
    ).toEqual([]);

    // Node types beyond the two Inspection_Nodes are represented, and the
    // table never expects one of them to be tunable.
    const others = CASES.filter(
      (one) =>
        one.nodeType !== NODE_TYPE_BEDROCK_INFERENCE && one.nodeType !== NODE_TYPE_LLM_INFERENCE
    );
    expect(new Set(others.map((one) => one.nodeType)).size).toBeGreaterThanOrEqual(5);
    expect(others.filter((one) => one.expectedTunable).map((one) => one.id)).toEqual([]);
  });
});
