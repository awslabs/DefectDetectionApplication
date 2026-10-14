/**
 * Unit tests for the two designer entry points (quality-prompt-tuning,
 * task 8.6 — Requirements 1.3, 1.4).
 *
 * Property 19 asserts *presence*: an entry point appears exactly for the
 * intended roles and workflows. These are the example-based counterparts for
 * everything else about them:
 *
 *  - the toolbar's "Tune anomaly prompts" action — where it navigates
 *    (`/workflow-tuning/anomaly?workflowId=`, URL-encoded), that it is
 *    disabled with a reason while the canvas is unsaved, and that the eight
 *    pre-existing toolbar actions are untouched (Requirement 1.3);
 *  - the node panel's "Prompt tuning" link — that it navigates straight to
 *    an existing Tuning_Session WITHOUT creating anything, create-or-gets one
 *    only when the node has none, falls back to the section overview when
 *    that create is refused, and renders the latest applied Tuning_Result
 *    (version, date, actor, candidate, and the applied vs baseline score)
 *    or the never-applied line (Requirement 1.4).
 *
 * The two pure line formatters are unit-tested directly as well.
 *
 * No AWS and no network: `apiService`, `UsecaseContext` and `useNavigate`
 * are mocked.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import WorkflowToolbar, { type WorkflowMeta } from '../workflows/WorkflowToolbar';
import NodeConfigPanel from '../workflows/NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from '../workflows/builderGraph';
import PromptTuningNodeEntry, {
  NEVER_APPLIED_MESSAGE,
  anomalyTuningHref,
  formatAppliedLine,
  formatScoreLine,
  tuningSessionHref,
} from './PromptTuningNodeEntry';
import type { JsonValue, NodeTypeDescriptor, WorkflowDefinition } from '../workflows/types';
import type { ScoreSummary, TuningResult } from './types';

const {
  navigateMock,
  listModels,
  listTuningWorkflows,
  getTuningSession,
  createTuningSession,
  useUsecaseMock,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listModels: vi.fn(),
  listTuningWorkflows: vi.fn(),
  getTuningSession: vi.fn(),
  createTuningSession: vi.fn(),
  useUsecaseMock: vi.fn(),
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock('../../services/api', () => ({
  ApiError: class ApiError extends Error {},
  apiService: {
    listModels,
    listTuningWorkflows,
    getTuningSession,
    createTuningSession,
    listWorkflows: vi.fn(),
    createWorkflow: vi.fn(),
    updateWorkflow: vi.fn(),
    deleteWorkflow: vi.fn(),
    duplicateWorkflow: vi.fn(),
    validateWorkflow: vi.fn(),
    packageWorkflow: vi.fn(),
  },
}));

vi.mock('../../contexts/UsecaseContext', () => ({ useUsecase: useUsecaseMock }));

// ------------------------------------------------------------------ fixtures

const BEDROCK_DESCRIPTOR: NodeTypeDescriptor = {
  typeId: 'bedrock_inference',
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
};

const CAMERA_DESCRIPTOR: NodeTypeDescriptor = {
  typeId: 'camera_source',
  category: 'input',
  displayName: 'Camera source',
  inputs: [],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [],
  mappings: [],
  hardwareDependent: true,
};

const WORKFLOW: WorkflowMeta = {
  workflowId: 'wf 1/a',
  name: 'Line inspection',
  description: '',
  version: 3,
};

function definition(
  nodes: Array<{ type: string; parameters?: Record<string, JsonValue> }>
): WorkflowDefinition {
  return {
    schemaVersion: 1 as WorkflowDefinition['schemaVersion'],
    nodes: nodes.map((node, index) => ({
      id: `${node.type}_${index + 1}`,
      type: node.type,
      position: { x: index * 40, y: 0 },
      parameters: node.parameters ?? {},
    })),
    connections: [],
  };
}

function builderNode(
  descriptor: NodeTypeDescriptor,
  parameters: Record<string, JsonValue> = {}
): BuilderNode {
  return {
    id: `${descriptor.typeId}_selected`,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor, parameters, validationMessages: [] },
  };
}

function summary(patch: Partial<ScoreSummary> = {}): ScoreSummary {
  return {
    samples: 10,
    invocations: 10,
    correct: 9,
    falsePass: 1,
    falseFail: 0,
    parseFailure: 0,
    invocationError: 0,
    accuracy: 0.9,
    unstable: 0,
    meanOutputTokens: 40,
    maxOutputTokens: 60,
    meanLatencyMs: 800,
    ...patch,
  };
}

function result(patch: Partial<TuningResult> = {}): TuningResult {
  return {
    appliedAt: 1_700_000_000,
    appliedBy: 'alice',
    newVersion: 5,
    previousVersion: 4,
    candidateId: 'cand-1',
    candidateName: 'Describe · compare · decide',
    scoreRunId: 'run-1',
    summary: summary(),
    baselineSummary: summary({ correct: 5, accuracy: 0.5, falsePass: 4 }),
    ...patch,
  };
}

function renderToolbar(
  def: WorkflowDefinition,
  options: { workflow?: WorkflowMeta | null } = {}
) {
  render(
    <WorkflowToolbar
      role="DataScientist"
      usecaseId="uc-1"
      workflow={options.workflow === undefined ? WORKFLOW : options.workflow}
      dirty={false}
      getDefinition={() => def}
      onSaved={vi.fn()}
      onOpenWorkflow={vi.fn()}
      onDeleted={vi.fn()}
      onNew={vi.fn()}
    />
  );
}

function overview(sessionId: string | null) {
  return {
    usecaseId: 'uc-1',
    sampleExportEnabled: true,
    sampleRetentionDays: 30,
    count: 1,
    sampleStoreError: null,
    workflows: [
      {
        workflowId: 'wf-1',
        name: 'Line inspection',
        latestVersion: 5,
        updatedAt: null,
        nodes: [
          {
            nodeId: 'bedrock_1',
            nodeType: 'bedrock_inference',
            model: null,
            sampleCount: 12,
            sessionId,
          },
        ],
      },
    ],
  };
}

async function renderEntry(options: {
  sessionId?: string | null;
  tuningResult?: TuningResult | null;
  usecaseId?: string | null;
} = {}) {
  const sessionId = options.sessionId === undefined ? 'ts-1' : options.sessionId;
  listTuningWorkflows.mockResolvedValue(overview(sessionId));
  getTuningSession.mockResolvedValue({
    session: { latestTuningResult: options.tuningResult ?? null },
  });
  render(
    <PromptTuningNodeEntry
      workflowId="wf-1"
      nodeId="bedrock_1"
      usecaseId={options.usecaseId === undefined ? 'uc-1' : options.usecaseId}
    />
  );
  await waitFor(() => expect(screen.getByTestId('prompt-tuning-entry')).toBeTruthy());
}

beforeEach(() => {
  vi.clearAllMocks();
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
  listModels.mockResolvedValue({ models: [] });
  createTuningSession.mockResolvedValue({ session: { sessionId: 'ts-created' } });
});

afterEach(() => {
  cleanup();
});

// ------------------------------------------------------------- Requirement 1.3

describe('Requirement 1.3: the toolbar action', () => {
  it('opens the section with the workflow preselected and URL-encoded', () => {
    renderToolbar(definition([{ type: 'bedrock_inference' }]));
    fireEvent.click(screen.getByRole('button', { name: 'Tune anomaly prompts' }));
    expect(navigateMock).toHaveBeenCalledWith(
      '/workflow-tuning/anomaly?workflowId=wf%201%2Fa'
    );
    expect(anomalyTuningHref('wf 1/a')).toBe(
      '/workflow-tuning/anomaly?workflowId=wf%201%2Fa'
    );
  });

  it('is offered for a tunable definition and withheld for one without', () => {
    renderToolbar(definition([{ type: 'camera_source' }, { type: 'bedrock_inference' }]));
    expect(screen.getByRole('button', { name: 'Tune anomaly prompts' })).toBeTruthy();
    cleanup();
    renderToolbar(
      definition([
        { type: 'camera_source' },
        { type: 'bedrock_inference', parameters: { anomaly_mode: false } },
        { type: 'llm_inference' },
      ])
    );
    expect(screen.queryByRole('button', { name: 'Tune anomaly prompts' })).toBeNull();
  });

  it('is disabled with a reason while the canvas is unsaved', () => {
    renderToolbar(definition([{ type: 'bedrock_inference' }]), { workflow: null });
    const action = screen.getByRole('button', { name: 'Tune anomaly prompts' });
    // Cloudscape renders a `disabledReason` button as aria-disabled so the
    // reason tooltip stays reachable.
    expect(action.getAttribute('aria-disabled')).toBe('true');
    fireEvent.click(action);
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it('leaves the pre-existing toolbar actions in place', () => {
    renderToolbar(definition([{ type: 'bedrock_inference' }]));
    for (const name of [
      'New',
      'Open',
      'Save',
      'Validate',
      'Package',
      'Duplicate',
      'Rename',
      'Delete',
    ]) {
      expect(screen.getByRole('button', { name })).toBeTruthy();
    }
  });
});

// ------------------------------------------------------------- Requirement 1.4

describe('Requirement 1.4: the node panel link', () => {
  it('opens an existing session without creating anything', async () => {
    await renderEntry({ sessionId: 'ts-7' });
    await waitFor(() => expect(getTuningSession).toHaveBeenCalledWith('ts-7'));
    fireEvent.click(screen.getByRole('button', { name: 'Prompt tuning' }));
    expect(navigateMock).toHaveBeenCalledWith(
      '/workflow-tuning/anomaly/sessions/ts-7'
    );
    expect(createTuningSession).not.toHaveBeenCalled();
    expect(tuningSessionHref('ts 7/a')).toBe(
      '/workflow-tuning/anomaly/sessions/ts%207%2Fa'
    );
  });

  it('reads the tuning surface without creating a session on mount', async () => {
    await renderEntry({ sessionId: null });
    expect(listTuningWorkflows).toHaveBeenCalledWith('uc-1', 'wf-1');
    expect(createTuningSession).not.toHaveBeenCalled();
    expect(getTuningSession).not.toHaveBeenCalled();
  });

  it('create-or-gets the session only when the link is followed', async () => {
    await renderEntry({ sessionId: null });
    fireEvent.click(screen.getByRole('button', { name: 'Prompt tuning' }));
    await waitFor(() =>
      expect(createTuningSession).toHaveBeenCalledWith({
        workflow_id: 'wf-1',
        node_id: 'bedrock_1',
      })
    );
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith(
        '/workflow-tuning/anomaly/sessions/ts-created'
      )
    );
  });

  it('falls back to the overview when the create is refused', async () => {
    await renderEntry({ sessionId: null });
    createTuningSession.mockRejectedValue(new Error('NODE_NOT_TUNABLE'));
    fireEvent.click(screen.getByRole('button', { name: 'Prompt tuning' }));
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith(anomalyTuningHref('wf-1'))
    );
    expect(screen.getByText(/NODE_NOT_TUNABLE/)).toBeTruthy();
  });

  it('shows the latest applied Tuning_Result and its score against the baseline', async () => {
    await renderEntry({ tuningResult: result() });
    await waitFor(() =>
      expect(screen.getByTestId('prompt-tuning-applied').textContent).toBe(
        "Applied as version 5 on 2023-11-14 by alice from candidate 'Describe · compare · decide'."
      )
    );
    expect(screen.getByTestId('prompt-tuning-score').textContent).toBe(
      'Scored accuracy 90%, 1 false pass, 10 invocations — baseline: accuracy 50%, 4 false passes.'
    );
  });

  it('says so when no tuned prompt has been applied yet', async () => {
    await renderEntry({ tuningResult: null });
    await waitFor(() => expect(screen.getByText(NEVER_APPLIED_MESSAGE)).toBeTruthy());
    expect(screen.queryByTestId('prompt-tuning-applied')).toBeNull();
  });

  it('degrades to the bare link when the tuning read fails', async () => {
    listTuningWorkflows.mockRejectedValue(new Error('404'));
    render(
      <PromptTuningNodeEntry workflowId="wf-1" nodeId="bedrock_1" usecaseId="uc-1" />
    );
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Prompt tuning' })).toBeTruthy()
    );
    expect(screen.queryByTestId('prompt-tuning-applied')).toBeNull();
  });

  it('reads nothing without a selected use case', async () => {
    await renderEntry({ usecaseId: null });
    expect(listTuningWorkflows).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Prompt tuning' })).toBeTruthy();
  });

  it('is mounted by the config panel for a tunable node of a saved workflow', async () => {
    listTuningWorkflows.mockResolvedValue(overview('ts-1'));
    getTuningSession.mockResolvedValue({ session: { latestTuningResult: null } });
    render(
      <NodeConfigPanel
        node={builderNode(BEDROCK_DESCRIPTOR)}
        onParametersChange={vi.fn()}
        role="DataScientist"
        workflowId="wf-1"
      />
    );
    await waitFor(() => expect(screen.getByTestId('prompt-tuning-entry')).toBeTruthy());
  });

  it('is not mounted for a non-tunable node nor for an unsaved workflow', () => {
    render(
      <NodeConfigPanel
        node={builderNode(CAMERA_DESCRIPTOR)}
        onParametersChange={vi.fn()}
        role="DataScientist"
        workflowId="wf-1"
      />
    );
    expect(screen.queryByTestId('prompt-tuning-entry')).toBeNull();
    cleanup();
    render(
      <NodeConfigPanel
        node={builderNode(BEDROCK_DESCRIPTOR)}
        onParametersChange={vi.fn()}
        role="DataScientist"
        workflowId={null}
      />
    );
    expect(screen.queryByTestId('prompt-tuning-entry')).toBeNull();
  });
});

// -------------------------------------------------- the two line formatters

describe('the Tuning_Result lines', () => {
  it('names the version, date, actor and candidate', () => {
    expect(formatAppliedLine(result())).toBe(
      "Applied as version 5 on 2023-11-14 by alice from candidate 'Describe · compare · decide'."
    );
  });

  it('falls back to the candidate id and an unnamed date/actor', () => {
    expect(
      formatAppliedLine(
        result({
          candidateName: null,
          appliedAt: Number.NaN,
          appliedBy: '',
        })
      )
    ).toBe("Applied as version 5 on an earlier date from candidate 'cand-1'.");
  });

  it('singularises one false pass and one invocation', () => {
    expect(
      formatScoreLine(
        result({
          summary: summary({ falsePass: 1, invocations: 1 }),
          baselineSummary: summary({ falsePass: 1, accuracy: null }),
        })
      )
    ).toBe(
      'Scored accuracy 90%, 1 false pass, 1 invocation — baseline: accuracy —, 1 false pass.'
    );
  });

  it('omits the baseline comparison when the baseline was never scored', () => {
    expect(formatScoreLine(result({ baselineSummary: null }))).toBe(
      'Scored accuracy 90%, 1 false pass, 10 invocations.'
    );
  });

  it('says so when the applied candidate carries no summary', () => {
    expect(formatScoreLine(result({ summary: null }))).toBe(
      'The applied candidate carries no score summary.'
    );
  });
});
