/**
 * Inspection-format Bedrock nodes are tunable — the frontend half of the
 * shared case table (AC-3) and the pin of the inspection instruction.
 *
 * A `bedrock_inference` node whose `answer_format` is exactly `inspection`
 * is a Tunable_Node whatever its `anomaly_mode`; every other node keeps the
 * anomaly-mode rule of Property 1. The oracle is the fixture
 * `edge-cv-portal/backend/tests/fixtures/inspection_tuning_cases.json`,
 * published by `edge-cv-portal/backend/tests/test_property_tuning_inspection_format.py`
 * from the Python rule: the eligibility case table crossed with
 * `answer_format` values, and the instruction an inspection-format node
 * appends, which the draft projection must reproduce character for
 * character.
 *
 * Names added with this change are loaded inside the tests. At c299a6e1
 * `isTunableNode("bedrock_inference", false, "inspection")` is false and
 * `INSPECTION_INSTRUCTION` is undefined.
 *
 * Harness: pure values only — no rendering, no API, no AWS.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import {
  NODE_TYPE_BEDROCK_INFERENCE,
  definitionHasTunableNode,
  isTunableNode,
  isTunableWorkflowNode,
  tunableNodeIds,
} from './eligibility';
import {
  INSTRUCTION_SEPARATOR,
  VERDICT_INSTRUCTION,
  projectUserMessage,
} from './promptPreview';

interface AnswerFormatEntry {
  id: string;
  /** False means the `answer_format` parameter is absent. */
  present: boolean;
  value: unknown;
}

interface InspectionCase {
  id: string;
  nodeType: string;
  anomalyModeId: string;
  anomalyModePresent: boolean;
  anomalyMode: unknown;
  /** Per `answerFormats[].id`. */
  expectedTunable: Record<string, boolean>;
}

interface InspectionFixture {
  inspectionJsonInstruction: string;
  anomalyInstructionSeparator: string;
  answerFormats: AnswerFormatEntry[];
  cases: InspectionCase[];
}

const FIXTURE_RELATIVE_PATH = path.join(
  'backend',
  'tests',
  'fixtures',
  'inspection_tuning_cases.json'
);

/** Walk up from the working directory to the `edge-cv-portal` directory. */
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
    `cannot find the shared case table (${FIXTURE_RELATIVE_PATH}) above `
      + `${process.cwd()}; it is published by `
      + 'edge-cv-portal/backend/tests/test_property_tuning_inspection_format.py'
  );
}

const FIXTURE: InspectionFixture = JSON.parse(
  readFileSync(resolveFixturePath(), 'utf-8')
);

function modeOf(item: InspectionCase): unknown {
  return item.anomalyModePresent ? item.anomalyMode : undefined;
}

function formatOf(entry: AnswerFormatEntry): unknown {
  return entry.present ? entry.value : undefined;
}

function parametersOf(
  item: InspectionCase,
  entry: AnswerFormatEntry
): Record<string, unknown> {
  const parameters: Record<string, unknown> = {};
  if (item.anomalyModePresent) parameters.anomaly_mode = item.anomalyMode;
  if (entry.present) parameters.answer_format = entry.value;
  return parameters;
}

describe('the published inspection-format case table', () => {
  it('isTunableNode and isTunableWorkflowNode agree with every case', () => {
    expect(FIXTURE.cases.length).toBeGreaterThan(300);
    expect(FIXTURE.answerFormats.map((entry) => entry.id)).toEqual([
      'absent',
      'inspection',
      'legacy',
      'Inspection',
      'space-inspection',
      'empty',
      'null',
    ]);
    const disagreements: string[] = [];
    for (const item of FIXTURE.cases) {
      for (const entry of FIXTURE.answerFormats) {
        const expected = item.expectedTunable[entry.id];
        const rule = isTunableNode(item.nodeType, modeOf(item), formatOf(entry));
        const node = isTunableWorkflowNode({
          id: 'n',
          type: item.nodeType,
          parameters: parametersOf(item, entry),
        });
        if (rule !== expected || node !== expected) {
          disagreements.push(`${item.id} × ${entry.id}: ${rule}/${node} ≠ ${expected}`);
        }
      }
    }
    expect(disagreements).toEqual([]);
  });

  it('an inspection-format Bedrock node is tunable for every anomaly_mode value', () => {
    const bedrock = FIXTURE.cases.filter(
      (item) => item.nodeType === NODE_TYPE_BEDROCK_INFERENCE
    );
    expect(bedrock.length).toBeGreaterThan(20);
    for (const item of bedrock) {
      expect(isTunableNode(item.nodeType, modeOf(item), 'inspection')).toBe(true);
    }
    expect(isTunableNode(NODE_TYPE_BEDROCK_INFERENCE, false, 'inspection')).toBe(true);
  });

  it('isInspectionFormat is the executor’s exact comparison', async () => {
    const { ANSWER_FORMAT_PARAMETER, isInspectionFormat } = await import(
      './eligibility'
    );
    expect(ANSWER_FORMAT_PARAMETER).toBe('answer_format');
    // A Bedrock node with anomaly_mode false is tunable only through the
    // inspection clause, so its published expectation is exactly whether
    // the answer_format value selects the inspection format.
    const offCase = FIXTURE.cases.find(
      (item) => item.id === `${NODE_TYPE_BEDROCK_INFERENCE}#false`
    )!;
    for (const entry of FIXTURE.answerFormats) {
      expect(isInspectionFormat(parametersOf(offCase, entry))).toBe(
        offCase.expectedTunable[entry.id]
      );
    }
    expect(isInspectionFormat(null)).toBe(false);
    expect(isInspectionFormat(undefined)).toBe(false);
  });

  it('a definition whose only tunable node is inspection-format has a tunable node', () => {
    const definition = {
      nodes: [
        { id: 'cam', type: 'camera_source', parameters: {} },
        {
          id: 'check',
          type: NODE_TYPE_BEDROCK_INFERENCE,
          parameters: { anomaly_mode: false, answer_format: 'inspection' },
        },
      ],
    };
    expect(definitionHasTunableNode(definition)).toBe(true);
    expect(tunableNodeIds(definition)).toEqual(['check']);
  });

  it('the inspection clause holds for drawn node types and anomaly_mode values', () => {
    const nodeTypes = Array.from(new Set(FIXTURE.cases.map((item) => item.nodeType)));
    const modes = FIXTURE.cases.map(modeOf);
    fc.assert(
      fc.property(
        fc.constantFrom(...nodeTypes),
        fc.constantFrom(...modes),
        fc.oneof(
          fc.constant('inspection'),
          fc.string(),
          fc.constant(undefined),
          fc.constant(null)
        ),
        (nodeType, mode, answerFormat) => {
          const twoArgument = isTunableNode(nodeType, mode);
          const expected =
            (nodeType === NODE_TYPE_BEDROCK_INFERENCE && answerFormat === 'inspection')
            || twoArgument;
          expect(isTunableNode(nodeType, mode, answerFormat)).toBe(expected);
        }
      ),
      // The decisive pair is checked on every run, whatever is drawn.
      { examples: [[NODE_TYPE_BEDROCK_INFERENCE, false, 'inspection']] }
    );
  });
});

describe('the inspection instruction of the draft projection', () => {
  it('is the shared module’s string, after the same separator', async () => {
    const { INSPECTION_INSTRUCTION } = await import('./promptPreview');
    expect(INSPECTION_INSTRUCTION).toBe(FIXTURE.inspectionJsonInstruction);
    expect(INSTRUCTION_SEPARATOR).toBe(FIXTURE.anomalyInstructionSeparator);
    expect(projectUserMessage('Check the parts.', 'inspection')).toBe(
      `Check the parts.${FIXTURE.anomalyInstructionSeparator}${FIXTURE.inspectionJsonInstruction}`
    );
  });

  it('leaves every other node’s projection unchanged (preservation: passes at c299a6e1 too)', () => {
    for (const answerFormat of [undefined, null, 'legacy', 'Inspection', ' inspection']) {
      expect(projectUserMessage('Check the parts.', answerFormat)).toBe(
        `Check the parts.${INSTRUCTION_SEPARATOR}${VERDICT_INSTRUCTION}`
      );
    }
  });
});
