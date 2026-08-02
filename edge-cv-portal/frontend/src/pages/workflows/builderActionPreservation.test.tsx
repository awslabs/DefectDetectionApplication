/**
 * Task 2.4 — Builder-action preservation (Property 6: Preservation).
 *
 * Bugfix spec: workflow-manager-integration-bugfixes.
 *
 * Bug 5's fix surfaces the open workflow's name at the top of the
 * Workflow_Builder page. Requirement 3.6 requires that change to be
 * DISPLAY-ONLY: every existing toolbar/drawer action — New, Open, Save,
 * Validate, Duplicate, Delete, Package, Generate, Test — SHALL continue to
 * behave exactly as before, and showing the name SHALL NOT change save,
 * load, or validation behavior.
 *
 * These are OBSERVATION-FIRST preservation tests: they drive the real
 * WorkflowBuilder page composition (only apiService and the auth/usecase
 * contexts mocked) and record the CURRENT observable behavior of each
 * action. They are EXPECTED TO PASS on the UNFIXED code, establishing the
 * baseline that the Bug 5 display-only change must preserve. After the fix,
 * task 7.3 re-runs this same file and it must still pass.
 *
 * The builder page (WorkflowBuilder.tsx) is exactly the file Bug 5 edits, so
 * exercising the actions end to end through this page is what catches any
 * accidental behavioral change introduced while relocating/adding the
 * top-of-page workflow-name header.
 *
 * Property 6: Preservation — Behavior unchanged outside every bug condition.
 *
 * Validates: Requirements 3.6
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import WorkflowBuilder from './WorkflowBuilder';
import type {
  NodeTypeDescriptor,
  ValidationFinding,
  WorkflowDefinition,
} from './types';

// --------------------------------------------------------------------------
// apiService and context mocks (mirrors WorkflowBuilder.test.tsx conventions)
// --------------------------------------------------------------------------

const {
  getWorkflowNodeCatalog,
  getWorkflow,
  listWorkflows,
  listModels,
  createWorkflow,
  updateWorkflow,
  deleteWorkflow,
  duplicateWorkflow,
  validateWorkflow,
  packageWorkflow,
  generateWorkflow,
  listTestDatasets,
  startTestRun,
  getTestRun,
  useAuthMock,
  useUsecaseMock,
} = vi.hoisted(() => ({
  getWorkflowNodeCatalog: vi.fn(),
  getWorkflow: vi.fn(),
  listWorkflows: vi.fn(),
  listModels: vi.fn(),
  createWorkflow: vi.fn(),
  updateWorkflow: vi.fn(),
  deleteWorkflow: vi.fn(),
  duplicateWorkflow: vi.fn(),
  validateWorkflow: vi.fn(),
  packageWorkflow: vi.fn(),
  generateWorkflow: vi.fn(),
  listTestDatasets: vi.fn(),
  startTestRun: vi.fn(),
  getTestRun: vi.fn(),
  useAuthMock: vi.fn(),
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
      getWorkflowNodeCatalog,
      getWorkflow,
      listWorkflows,
      listModels,
      createWorkflow,
      updateWorkflow,
      deleteWorkflow,
      duplicateWorkflow,
      validateWorkflow,
      packageWorkflow,
      generateWorkflow,
      listTestDatasets,
      startTestRun,
      getTestRun,
    },
  };
});

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: useUsecaseMock,
}));

// --------------------------------------------------------------------------
// jsdom stubs required by React Flow (ResizeObserver, DOMMatrixReadOnly,
// element offset sizes, and SVG getBBox) — same as WorkflowBuilder.test.tsx.
// --------------------------------------------------------------------------

beforeAll(() => {
  class ResizeObserverStub {
    private readonly callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) {
      this.callback = callback;
    }
    observe(target: Element) {
      const width = (target as HTMLElement).offsetWidth ?? 0;
      const height = (target as HTMLElement).offsetHeight ?? 0;
      const size = [{ inlineSize: width, blockSize: height }];
      const entry = {
        target,
        contentRect: target.getBoundingClientRect(),
        contentBoxSize: size,
        borderBoxSize: size,
        devicePixelContentBoxSize: size,
      } as unknown as ResizeObserverEntry;
      this.callback([entry], this as unknown as ResizeObserver);
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);

  class DOMMatrixReadOnlyStub {
    readonly m22: number;
    constructor(transform?: string) {
      const scale = transform?.match(/scale\(([0-9.]+)\)/)?.[1];
      this.m22 = scale === undefined ? 1 : Number(scale);
    }
  }
  vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixReadOnlyStub);

  Object.defineProperties(HTMLElement.prototype, {
    offsetHeight: {
      configurable: true,
      get(this: HTMLElement) {
        return parseFloat(this.style.height) || 600;
      },
    },
    offsetWidth: {
      configurable: true,
      get(this: HTMLElement) {
        return parseFloat(this.style.width) || 800;
      },
    },
  });

  (SVGElement.prototype as unknown as { getBBox: () => DOMRect }).getBBox = () =>
    ({ x: 0, y: 0, width: 0, height: 0 } as DOMRect);
});

// --------------------------------------------------------------------------
// Fixtures: a catalog covering all five categories, and a saved workflow
// --------------------------------------------------------------------------

function descriptor(
  typeId: string,
  category: string,
  displayName: string,
  overrides: Partial<NodeTypeDescriptor> = {}
): NodeTypeDescriptor {
  return {
    typeId,
    category,
    displayName,
    inputs: [],
    outputs: [],
    parameters: [],
    mappings: [],
    hardwareDependent: false,
    ...overrides,
  };
}

const CATALOG: NodeTypeDescriptor[] = [
  descriptor('camera_source', 'input', 'Camera source', {
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
    parameters: [
      {
        name: 'device',
        paramType: 'string',
        required: true,
        default: '/dev/video0',
        constraints: {},
      },
    ],
  }),
  descriptor('crop', 'preprocessing', 'Crop', {
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
  }),
  descriptor('model_inference', 'inference', 'Model inference', {
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
  }),
  descriptor('inference_filter', 'post_processing', 'Inference filter', {
    inputs: [{ name: 'in', portType: 'InferenceMeta' }],
    outputs: [{ name: 'out', portType: 'InferenceMeta' }],
  }),
  descriptor('mqtt_publish', 'output', 'MQTT publish', {
    inputs: [{ name: 'in', portType: 'InferenceMeta' }],
  }),
];

const WORKFLOW_NAME = 'Line inspection';

const SAVED_DEFINITION: WorkflowDefinition = {
  schemaVersion: 1,
  nodes: [
    {
      id: 'camera_source_1',
      type: 'camera_source',
      position: { x: 0, y: 0 },
      parameters: { device: '/dev/video0' },
    },
    {
      id: 'mqtt_publish_1',
      type: 'mqtt_publish',
      position: { x: 300, y: 0 },
      parameters: {},
    },
  ],
  connections: [],
};

/** The saved workflow returned by getWorkflow for deep-link loads. */
const LOADED_WORKFLOW = {
  workflow: {
    workflow_id: 'wf-1',
    usecase_id: 'uc-1',
    name: WORKFLOW_NAME,
    description: '',
    created_at: 1,
    updated_at: 1,
    latest_version: 3,
  },
  version: 3,
  definition: SAVED_DEFINITION,
};

function renderBuilder(initialPath = '/workflows/builder') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/workflows/builder" element={<WorkflowBuilder />} />
        <Route path="/workflows/builder/:workflowId" element={<WorkflowBuilder />} />
      </Routes>
    </MemoryRouter>
  );
}

/** CloudScape disables buttons natively or via aria-disabled. */
function isDisabled(button: HTMLElement): boolean {
  return (
    (button as HTMLButtonElement).disabled === true ||
    button.getAttribute('aria-disabled') === 'true'
  );
}

/** The toolbar-scoped action button (avoids modal-confirm name clashes). */
function toolbarButton(name: string): HTMLElement {
  const toolbar = screen.getByRole('toolbar', { name: 'Workflow actions' });
  return within(toolbar).getByRole('button', { name });
}

/** Select an option in a CloudScape Select by its placeholder trigger. */
async function selectOption(triggerName: RegExp, optionLabel: RegExp) {
  const trigger = screen.getByRole('button', { name: triggerName });
  fireEvent.mouseDown(trigger);
  fireEvent.click(trigger);
  const option = await screen.findByRole('option', { name: optionLabel });
  fireEvent.mouseDown(option);
  fireEvent.mouseUp(option);
  fireEvent.click(option);
}

beforeEach(() => {
  vi.clearAllMocks();
  getWorkflowNodeCatalog.mockResolvedValue({ nodeTypes: CATALOG });
  listWorkflows.mockResolvedValue({ workflows: [], count: 0 });
  listModels.mockResolvedValue({ models: [], count: 0, usecase_id: 'uc-1' });
  listTestDatasets.mockResolvedValue({ datasets: [], count: 0 });
  getWorkflow.mockResolvedValue(LOADED_WORKFLOW);
  useAuthMock.mockReturnValue({ user: { role: 'DataScientist' } });
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
});

// --------------------------------------------------------------------------
// New — start a fresh workflow (clears the canvas, resets to unsaved)
// --------------------------------------------------------------------------

describe('Builder action preservation: New (Requirement 3.6)', () => {
  it('resets a loaded workflow to an unsaved canvas without any API call', async () => {
    renderBuilder('/workflows/builder/wf-1');

    // Loaded: the toolbar surfaces the name/version as small status text.
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();

    // Loading is not dirty, so New resets immediately (no confirm modal).
    fireEvent.click(toolbarButton('New'));

    expect(await screen.findByText('Unsaved workflow')).toBeInTheDocument();
    // New is a local action: no workflow API is invoked.
    expect(createWorkflow).not.toHaveBeenCalled();
    expect(updateWorkflow).not.toHaveBeenCalled();
    expect(deleteWorkflow).not.toHaveBeenCalled();
  });
});

// --------------------------------------------------------------------------
// Open — pick a saved workflow and load it onto the canvas (5.4)
// --------------------------------------------------------------------------

describe('Builder action preservation: Open (Requirement 3.6)', () => {
  it('lists the use case workflows and loads the chosen one onto the canvas', async () => {
    listWorkflows.mockResolvedValue({
      workflows: [
        { workflow_id: 'wf-1', name: WORKFLOW_NAME, latest_version: 3 },
      ],
      count: 1,
    });
    renderBuilder();
    await screen.findByRole('navigation', { name: 'Node palette' });

    fireEvent.click(toolbarButton('Open'));

    // The picker lists the selected use case's workflows (5.4).
    await waitFor(() => expect(listWorkflows).toHaveBeenCalledWith('uc-1'));
    await selectOption(/Choose a workflow/, new RegExp(WORKFLOW_NAME));

    fireEvent.click(screen.getByRole('button', { name: 'Open workflow' }));

    // The chosen workflow is fetched and rendered; its identity shows.
    await waitFor(() => expect(getWorkflow).toHaveBeenCalledWith('wf-1'));
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Save — first save prompts for a name and creates version 1 (5.1)
// --------------------------------------------------------------------------

describe('Builder action preservation: Save (Requirement 3.6)', () => {
  it('prompts for a name on first save and creates the workflow via the API', async () => {
    createWorkflow.mockResolvedValue({
      workflow: {
        workflow_id: 'wf-new',
        usecase_id: 'uc-1',
        name: 'My workflow',
        description: '',
        created_at: 1,
        updated_at: 1,
        latest_version: 1,
      },
      version: 1,
    });
    renderBuilder();
    await screen.findByRole('navigation', { name: 'Node palette' });

    fireEvent.click(toolbarButton('Save'));

    const nameInput = document.querySelector('input[aria-label="Workflow name"]');
    expect(nameInput).not.toBeNull();
    fireEvent.change(nameInput!, { target: { value: 'My workflow' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save workflow' }));

    await waitFor(() =>
      expect(createWorkflow).toHaveBeenCalledWith(
        expect.objectContaining({
          usecase_id: 'uc-1',
          name: 'My workflow',
        })
      )
    );
    expect(
      await screen.findByText("Saved 'My workflow' as version 1")
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Validate — user-invoked backend validation, findings displayed (4.8, 4.9)
// --------------------------------------------------------------------------

describe('Builder action preservation: Validate (Requirement 3.6)', () => {
  const FINDINGS: ValidationFinding[] = [
    {
      severity: 'error',
      code: 'V4_MISSING_REQUIRED_PARAMETER',
      message: "Required parameter 'modelName' has no value",
      nodeId: 'model_inference_1',
      connectionId: null,
    },
  ];

  it('invokes backend validation for the loaded version and shows the findings', async () => {
    validateWorkflow.mockResolvedValue({
      workflow_id: 'wf-1',
      version: 3,
      passed: false,
      validation_status: { status: 'failed' },
      findings: FINDINGS,
      error_count: 1,
      warning_count: 0,
    });
    renderBuilder('/workflows/builder/wf-1');
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();

    fireEvent.click(toolbarButton('Validate'));

    await waitFor(() => expect(validateWorkflow).toHaveBeenCalledWith('wf-1', 3));
    // No unsaved changes: nothing is re-saved before validating.
    expect(updateWorkflow).not.toHaveBeenCalled();
    expect(
      await screen.findByText('Validation failed: 1 error, 0 warnings')
    ).toBeInTheDocument();
    expect(
      screen.getByText(/V4_MISSING_REQUIRED_PARAMETER/)
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Duplicate — copy the workflow under a new name (5.7)
// --------------------------------------------------------------------------

describe('Builder action preservation: Duplicate (Requirement 3.6)', () => {
  it('duplicates the loaded workflow via the API', async () => {
    duplicateWorkflow.mockResolvedValue({
      workflow: {
        workflow_id: 'wf-copy',
        usecase_id: 'uc-1',
        name: 'Line inspection (copy)',
        description: '',
        created_at: 2,
        updated_at: 2,
        latest_version: 1,
      },
      version: 1,
    });
    renderBuilder('/workflows/builder/wf-1');
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();

    fireEvent.click(toolbarButton('Duplicate'));

    await waitFor(() => expect(duplicateWorkflow).toHaveBeenCalledWith('wf-1'));
    expect(
      await screen.findByText("Duplicated as 'Line inspection (copy)'")
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Delete — confirm, then delete and reset the canvas (5.5)
// --------------------------------------------------------------------------

describe('Builder action preservation: Delete (Requirement 3.6)', () => {
  it('deletes only after confirmation and resets the canvas to unsaved', async () => {
    deleteWorkflow.mockResolvedValue({ workflow_id: 'wf-1', message: 'deleted' });
    renderBuilder('/workflows/builder/wf-1');
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();

    fireEvent.click(toolbarButton('Delete'));
    // Confirmation required: nothing deleted yet.
    expect(deleteWorkflow).not.toHaveBeenCalled();
    expect(
      screen.getByText(
        `Delete '${WORKFLOW_NAME}' and all its versions? This cannot be undone.`
      )
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Delete workflow' }));

    await waitFor(() => expect(deleteWorkflow).toHaveBeenCalledWith('wf-1'));
    // After delete the canvas resets to a fresh, unsaved workflow.
    expect(await screen.findByText('Unsaved workflow')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Package — validate then register the deployable component (7.x)
// --------------------------------------------------------------------------

describe('Builder action preservation: Package (Requirement 3.6)', () => {
  it('validates then packages the selected architecture', async () => {
    validateWorkflow.mockResolvedValue({
      workflow_id: 'wf-1',
      version: 3,
      passed: true,
      validation_status: { status: 'passed' },
      findings: [],
      error_count: 0,
      warning_count: 0,
    });
    packageWorkflow.mockResolvedValue({
      workflow_id: 'wf-1',
      version: 3,
      component_name: 'dda.workflow.wf-1',
      component_version: '3.0.0',
      component_arn: 'arn:...',
      architectures: ['arm64_jp6'],
      artifacts: { arm64_jp6: 's3://bucket/key' },
    });
    renderBuilder('/workflows/builder/wf-1');
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();

    fireEvent.click(toolbarButton('Package'));
    await screen.findByText('Package workflow for deployment');

    await selectOption(/Select target architectures/, /JetPack 6/);
    fireEvent.click(screen.getByRole('button', { name: 'Package workflow' }));

    await waitFor(() =>
      expect(packageWorkflow).toHaveBeenCalledWith('wf-1', {
        architectures: ['arm64_jp6'],
        version: 3,
      })
    );
    expect(validateWorkflow).toHaveBeenCalledWith('wf-1', 3);
    expect(
      await screen.findByText(/Packaged 'Line inspection' v3 as dda\.workflow\.wf-1 3\.0\.0/)
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Generate — prompt-based generation renders onto the canvas (10.1, 10.3)
// --------------------------------------------------------------------------

describe('Builder action preservation: Generate (Requirement 3.6)', () => {
  it('sends the prompt and renders the generated workflow with its findings', async () => {
    generateWorkflow.mockResolvedValue({
      session_id: 'sess-1',
      usecase_id: 'uc-1',
      definition: {
        schemaVersion: 1,
        nodes: [
          {
            id: 'camera_source_1',
            type: 'camera_source',
            position: { x: 0, y: 0 },
            parameters: { device: '/dev/video0' },
          },
          {
            id: 'mqtt_publish_1',
            type: 'mqtt_publish',
            position: { x: 300, y: 0 },
            parameters: {},
          },
        ],
        connections: [],
      } satisfies WorkflowDefinition,
      findings: [],
      error_count: 0,
      warning_count: 0,
      validation_passed: true,
      assistant_text: 'Generated a workflow.',
    });

    renderBuilder();
    await screen.findByRole('navigation', { name: 'Node palette' });

    // Open the Generate drawer and send a prompt.
    fireEvent.click(screen.getByRole('button', { name: 'Open the Generate workflow panel' }));
    const promptInput = document.querySelector(
      'textarea[aria-label="Workflow generation prompt"]'
    ) as HTMLTextAreaElement;
    expect(promptInput).not.toBeNull();
    fireEvent.change(promptInput, { target: { value: 'Read the camera and publish results' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(generateWorkflow).toHaveBeenCalledWith(
        expect.objectContaining({
          usecase_id: 'uc-1',
          prompt: 'Read the camera and publish results',
        })
      )
    );
    // Backend validation summary is displayed for review before saving (10.3).
    expect(
      await screen.findByText('Validation passed (0 warnings)')
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Test — dataset-scoped test run of a saved workflow (12.1, 12.2)
// --------------------------------------------------------------------------

describe('Builder action preservation: Test (Requirement 3.6)', () => {
  it('loads the use case datasets and starts a test run of the saved workflow', async () => {
    listTestDatasets.mockResolvedValue({
      datasets: [
        {
          dataset_id: 'ds-1',
          usecase_id: 'uc-1',
          name: 'Sample images',
          file_count: 5,
        },
      ],
      count: 1,
    });
    startTestRun.mockResolvedValue({
      test_run: {
        test_run_id: 'tr-1',
        workflow_id: 'wf-1',
        version: 3,
        usecase_id: 'uc-1',
        dataset_id: 'ds-1',
        status: 'running',
        progress: [],
        failure: null,
        started_at: 1,
        finished_at: null,
      },
    });
    getTestRun.mockResolvedValue({
      test_run: {
        test_run_id: 'tr-1',
        workflow_id: 'wf-1',
        version: 3,
        usecase_id: 'uc-1',
        dataset_id: 'ds-1',
        status: 'completed',
        progress: [{ at: 1, message: 'Completed' }],
        failure: null,
        started_at: 1,
        finished_at: 2,
      },
      node_results: [
        {
          nodeId: 'camera_source_1',
          status: 'completed',
          outputs: [],
          stubActivity: [],
          error: null,
        },
      ],
    });

    // A saved workflow must be loaded for the test action to be available.
    renderBuilder('/workflows/builder/wf-1');
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();

    // Open the Test drawer: datasets scoped to the use case load (12.2).
    fireEvent.click(screen.getByRole('button', { name: 'Open the Test workflow panel' }));
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalledWith('uc-1'));

    await selectOption(/Choose a test dataset/, /Sample images/);

    const runButton = await screen.findByRole('button', { name: 'Run test' });
    await waitFor(() => expect(isDisabled(runButton)).toBe(false));
    fireEvent.click(runButton);

    await waitFor(() =>
      expect(startTestRun).toHaveBeenCalledWith('wf-1', {
        dataset_id: 'ds-1',
        version: 3,
      })
    );
    await waitFor(() => expect(getTestRun).toHaveBeenCalledWith('tr-1'));
  });
});
