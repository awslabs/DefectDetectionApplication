/**
 * **Feature: quality-prompt-tuning, Property 19: Navigation and entry points
 * appear exactly for the intended roles and workflows** —
 * **Validates: Requirements 1.1, 1.3, 1.4**
 *
 * *For any* role, the "Workflow Tuning" navigation entry SHALL be present iff
 * the role may edit workflows; *for any* loaded Workflow_Definition, the
 * toolbar's "Tune anomaly prompts" action SHALL be present iff the definition
 * has at least one Tunable_Node; and the node panel's "Prompt tuning" link
 * SHALL be present iff the selected node is a Tunable_Node.
 *
 * How the three clauses are asserted
 * ----------------------------------
 *
 * All three are checked on every generated example, against oracles restated
 * locally in this file and never imported from the modules under test:
 *
 *  - `oracleMayEditWorkflows` — Requirement 1.1's role list (DataScientist,
 *    UseCaseAdmin, PortalAdmin), so the test does not take
 *    `canAccessWorkflowTuning` for its notion of correctness;
 *  - `oracleIsTunable` — Requirement 1.5's rule (`bedrock_inference` with
 *    `anomaly_mode` absent/null/true, `llm_inference` with `anomaly_mode` true),
 *    independent of `pages/workflow-tuning/eligibility.ts`. That module's own
 *    agreement with the shared Python case table is Property 1
 *    (`eligibility.property.test.ts`); here the rule is only the oracle for
 *    *which* workflows and nodes must show an entry point.
 *
 * The two designer entry points are additionally gated on the acting role (an
 * entry point must never lead to a `/workflow-tuning/**` route the role's guard
 * rejects) and — for the node panel — on the workflow having been saved (a
 * Tuning_Session is keyed by `(workflowId, nodeId)`). Those conjuncts come from
 * Requirement 1.1 and from the session identity, and they only ever *remove*
 * entry points, so the property is asserted in its conjoined form: present iff
 * the role may edit workflows AND the definition/node is tunable (AND, for the
 * panel, the workflow is saved).
 *
 * Harness: vitest + @testing-library/react + fast-check, 100 examples. No AWS
 * and no network — `apiService` is mocked, and `listTuningWorkflows` (the read
 * `PromptTuningNodeEntry` starts on mount) returns a promise that never settles
 * so a presence check never depends on an async continuation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import * as fc from 'fast-check';
import { buildNavigationItems } from '../../components/Layout';
import WorkflowToolbar, { type WorkflowMeta } from '../workflows/WorkflowToolbar';
import NodeConfigPanel from '../workflows/NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from '../workflows/builderGraph';
import type {
  JsonValue,
  NodeTypeDescriptor,
  WorkflowDefinition,
  WorkflowNode,
} from '../workflows/types';
import type { UserRole } from '../../types';

// ------------------------------------------------------------------- mocks

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));

// Partial mock: only `useNavigate` is replaced, so `Layout`'s other
// react-router-dom imports keep working when its module graph loads.
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => navigateMock };
});

const { listModels, listTuningWorkflows, getTuningSession, useUsecaseMock } = vi.hoisted(() => ({
  listModels: vi.fn(),
  listTuningWorkflows: vi.fn(),
  getTuningSession: vi.fn(),
  useUsecaseMock: vi.fn(),
}));

vi.mock('../../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  return {
    ApiError,
    apiService: {
      listModels,
      listTuningWorkflows,
      getTuningSession,
      // The toolbar's own actions are not exercised here; these exist so a
      // stray call fails loudly instead of throwing "not a function".
      listWorkflows: vi.fn(),
      createWorkflow: vi.fn(),
      updateWorkflow: vi.fn(),
      deleteWorkflow: vi.fn(),
      duplicateWorkflow: vi.fn(),
      validateWorkflow: vi.fn(),
      packageWorkflow: vi.fn(),
      createTuningSession: vi.fn(),
    },
  };
});

vi.mock('../../contexts/UsecaseContext', () => ({ useUsecase: useUsecaseMock }));

// ------------------------------------------------------------------ oracles

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
  'DataLabeler',
];

/** Requirement 1.1's "roles that may edit workflows", restated. */
function oracleMayEditWorkflows(role: UserRole | undefined): boolean {
  return role === 'DataScientist' || role === 'UseCaseAdmin' || role === 'PortalAdmin';
}

const BEDROCK = 'bedrock_inference';
const LLM = 'llm_inference';

/**
 * Requirement 1.5's rule, restated over a node's raw `anomaly_mode`
 * parameter: `bedrock_inference` is tunable unless `anomaly_mode` is present
 * and falsy, `llm_inference` only while it is present and truthy, and no other
 * node type ever is. Only the values this file generates are handled — the full
 * coercion contract is Property 1's business.
 */
function oracleIsTunable(nodeType: string, parameters: Record<string, JsonValue>): boolean {
  const present = Object.prototype.hasOwnProperty.call(parameters, 'anomaly_mode');
  const raw = present ? parameters.anomaly_mode : null;
  let truth: boolean | null;
  if (raw === null || raw === undefined) {
    truth = null;
  } else if (typeof raw === 'boolean') {
    truth = raw;
  } else if (typeof raw === 'number') {
    truth = raw !== 0;
  } else if (typeof raw === 'string') {
    const text = raw.trim().toLowerCase();
    if (text === 'true') truth = true;
    else if (text === 'false') truth = false;
    else if (text === '0' || text === '0.0') truth = false;
    else truth = raw.length > 0;
  } else {
    throw new Error('unexpected generated anomaly_mode value');
  }
  if (nodeType === BEDROCK) {
    return truth === null ? true : truth;
  }
  if (nodeType === LLM) {
    return truth === null ? false : truth;
  }
  return false;
}

// ----------------------------------------------------------------- fixtures

/** Descriptors mirroring the served catalog closely enough to render. */
const DESCRIPTORS: Readonly<Record<string, NodeTypeDescriptor>> = {
  [BEDROCK]: {
    typeId: BEDROCK,
    category: 'inference',
    displayName: 'Bedrock Inference',
    inputs: [
      { name: 'in', portType: 'VideoFrames' },
      { name: 'reference', portType: 'VideoFrames' },
    ],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    parameters: [
      { name: 'prompt', paramType: 'string', required: true, default: 'Inspect', constraints: {} },
      { name: 'anomaly_mode', paramType: 'bool', required: false, default: true, constraints: {} },
    ],
    mappings: [],
    hardwareDependent: false,
  },
  [LLM]: {
    typeId: LLM,
    category: 'inference',
    displayName: 'VLM/LLM Inference',
    inputs: [
      { name: 'in', portType: 'VideoFrames' },
      { name: 'reference', portType: 'VideoFrames' },
    ],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    parameters: [
      {
        name: 'prompt_template',
        paramType: 'string',
        required: true,
        default: null,
        constraints: {},
      },
      { name: 'anomaly_mode', paramType: 'bool', required: false, default: false, constraints: {} },
    ],
    mappings: [],
    hardwareDependent: false,
  },
  model_inference: {
    typeId: 'model_inference',
    category: 'inference',
    displayName: 'Model Inference',
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    parameters: [
      { name: 'anomaly_mode', paramType: 'bool', required: false, default: true, constraints: {} },
    ],
    mappings: [],
    hardwareDependent: false,
  },
  camera_source: {
    typeId: 'camera_source',
    category: 'input',
    displayName: 'Camera source',
    inputs: [],
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
    parameters: [],
    mappings: [],
    hardwareDependent: true,
  },
  bedrock: {
    typeId: 'bedrock',
    category: 'inference',
    displayName: 'Near miss (bedrock)',
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    parameters: [
      { name: 'anomaly_mode', paramType: 'bool', required: false, default: true, constraints: {} },
    ],
    mappings: [],
    hardwareDependent: false,
  },
  LLM_Inference: {
    typeId: 'LLM_Inference',
    category: 'inference',
    displayName: 'Near miss (case)',
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
    parameters: [
      { name: 'anomaly_mode', paramType: 'bool', required: false, default: true, constraints: {} },
    ],
    mappings: [],
    hardwareDependent: false,
  },
};

const NODE_TYPES: readonly string[] = Object.keys(DESCRIPTORS);

const WORKFLOW: WorkflowMeta = {
  workflowId: 'wf-1',
  name: 'Line inspection',
  description: '',
  version: 3,
};

// -------------------------------------------------------------- arbitraries

/** Every role plus the role-less / still-loading state. */
const roleArb: fc.Arbitrary<UserRole | undefined> = fc.constantFrom(...ALL_ROLES, undefined);

/**
 * `anomaly_mode` values, including the absence of the parameter (`undefined`
 * here, dropped from the record when building the node).
 */
const anomalyModeArb: fc.Arbitrary<JsonValue | undefined> = fc.constantFrom<
  Array<JsonValue | undefined>
>(undefined, null, true, false, 'true', 'false', 'True', 'FALSE', ' true ', '0', '1', '', 0, 1, 2.5);

interface GeneratedNode {
  nodeType: string;
  parameters: Record<string, JsonValue>;
}

const nodeArb: fc.Arbitrary<GeneratedNode> = fc
  .record({
    nodeType: fc.constantFrom(...NODE_TYPES),
    anomalyMode: anomalyModeArb,
  })
  .map(({ nodeType, anomalyMode }) => {
    const parameters: Record<string, JsonValue> = {};
    if (anomalyMode !== undefined) {
      parameters.anomaly_mode = anomalyMode;
    }
    return { nodeType, parameters };
  });

/** A canvas definition of 0..4 nodes, any mix of types and modes. */
const definitionArb: fc.Arbitrary<WorkflowDefinition> = fc
  .array(nodeArb, { minLength: 0, maxLength: 4 })
  .map((generated) => ({
    schemaVersion: 1 as WorkflowDefinition['schemaVersion'],
    nodes: generated.map(
      (one, index): WorkflowNode => ({
        id: `${one.nodeType}_${index + 1}`,
        type: one.nodeType,
        position: { x: index * 40, y: 0 },
        parameters: one.parameters,
      })
    ),
    connections: [],
  }));

// -------------------------------------------------------------------- helpers

function builderNode(one: GeneratedNode): BuilderNode {
  return {
    id: `${one.nodeType}_selected`,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor: DESCRIPTORS[one.nodeType], parameters: one.parameters, validationMessages: [] },
  };
}

/** Every href reachable from the navigation list, groups included. */
function navigationHrefs(items: readonly unknown[]): string[] {
  const hrefs: string[] = [];
  const walk = (list: readonly unknown[]) => {
    for (const item of list) {
      const record = item as { href?: string; items?: readonly unknown[] };
      if (typeof record.href === 'string') {
        hrefs.push(record.href);
      }
      if (Array.isArray(record.items)) {
        walk(record.items);
      }
    }
  };
  walk(items);
  return hrefs;
}

/** The "Workflow Tuning" group with its "VLM/LLM Anomaly Tuning" sub-entry. */
function navigationTuningGroup(role: UserRole | undefined) {
  return buildNavigationItems(role).find((item) => {
    const record = item as { type?: string; text?: string };
    return record.type === 'expandable-link-group' && record.text === 'Workflow Tuning';
  }) as { href?: string; items?: Array<{ text?: string; href?: string }> } | undefined;
}

const TUNE_ACTION = 'Tune anomaly prompts';
const PANEL_LINK = 'Prompt tuning';

beforeEach(() => {
  vi.clearAllMocks();
  useUsecaseMock.mockReturnValue({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() });
  // Never settles: mount-time reads must not influence a presence check, and
  // no state update can land after the example's unmount.
  listModels.mockReturnValue(new Promise(() => {}));
  listTuningWorkflows.mockReturnValue(new Promise(() => {}));
  getTuningSession.mockReturnValue(new Promise(() => {}));
});

afterEach(() => {
  cleanup();
});

// ------------------------------------------------------------- Property 19

describe('Property 19: Navigation and entry points appear exactly for the intended roles and workflows', () => {
  it('shows the nav entry, the toolbar action and the node-panel link exactly when they are due', () => {
    fc.assert(
      fc.property(
        roleArb,
        definitionArb,
        nodeArb,
        fc.boolean(),
        (role, definition, selected, workflowSaved) => {
          const mayEdit = oracleMayEditWorkflows(role);

          // ---- Requirement 1.1: the navigation entry, by role alone.
          const group = navigationTuningGroup(role);
          const hrefs = navigationHrefs(buildNavigationItems(role));
          expect(group !== undefined, `role=${role}: "Workflow Tuning" nav entry`).toBe(mayEdit);
          if (mayEdit) {
            expect(group?.href).toBe('/workflow-tuning');
            expect(
              (group?.items ?? []).some(
                (item) =>
                  item.text === 'VLM/LLM Anomaly Tuning' && item.href === '/workflow-tuning/anomaly'
              ),
              `role=${role}: the anomaly tuning sub-entry`
            ).toBe(true);
          } else {
            expect(
              hrefs.filter((href) => href.startsWith('/workflow-tuning')),
              `role=${role}: tuning hrefs leaked into the navigation`
            ).toEqual([]);
          }

          // ---- Requirement 1.3: the toolbar action, by definition (and role).
          const hasTunable = definition.nodes.some((node) =>
            oracleIsTunable(node.type, node.parameters)
          );
          const toolbarDue = mayEdit && hasTunable;
          const toolbar = render(
            <WorkflowToolbar
              role={role}
              usecaseId="uc-1"
              workflow={WORKFLOW}
              dirty={false}
              getDefinition={() => definition}
              onSaved={vi.fn()}
              onOpenWorkflow={vi.fn()}
              onDeleted={vi.fn()}
              onNew={vi.fn()}
            />
          );
          try {
            const action = screen.queryByRole('button', { name: TUNE_ACTION });
            expect(
              action !== null,
              `role=${role} tunableNodes=${hasTunable}: "${TUNE_ACTION}" action`
            ).toBe(toolbarDue);
            if (action !== null) {
              // 1.3's "opens Anomaly_Tuning with that workflow preselected".
              navigateMock.mockClear();
              fireEvent.click(action);
              expect(navigateMock).toHaveBeenCalledWith(
                `/workflow-tuning/anomaly?workflowId=${WORKFLOW.workflowId}`
              );
            } else {
              expect(toolbar.container.textContent ?? '').not.toContain(TUNE_ACTION);
            }
          } finally {
            toolbar.unmount();
          }

          // ---- Requirement 1.4: the node panel link, by selected node (and
          // role, and the workflow being saved).
          const nodeTunable = oracleIsTunable(selected.nodeType, selected.parameters);
          const panelDue = mayEdit && nodeTunable && workflowSaved;
          const panel = render(
            <NodeConfigPanel
              node={builderNode(selected)}
              onParametersChange={vi.fn()}
              role={role}
              workflowId={workflowSaved ? WORKFLOW.workflowId : null}
            />
          );
          try {
            const link = screen.queryByRole('button', { name: PANEL_LINK });
            const where = `role=${role} node=${selected.nodeType} params=${JSON.stringify(
              selected.parameters
            )} saved=${workflowSaved}`;
            expect(link !== null, `${where}: "${PANEL_LINK}" link`).toBe(panelDue);
            expect(screen.queryByTestId('prompt-tuning-entry') !== null, where).toBe(panelDue);
            if (!panelDue) {
              expect(panel.container.textContent ?? '', where).not.toContain(PANEL_LINK);
            }
          } finally {
            panel.unmount();
          }
        }
      ),
      { numRuns: 100 }
    );
  }, 600_000);
});
