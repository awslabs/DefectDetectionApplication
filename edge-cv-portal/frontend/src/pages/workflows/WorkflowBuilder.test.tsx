/**
 * Builder-level component tests for the Workflow_Builder page (task 8.9):
 * the Node_Palette renders all five sections with catalog-sourced items
 * when the page loads (Requirement 1.1), and the validate button invokes
 * the backend Workflow_Validator and displays the complete findings list
 * through the real page composition (Requirements 4.8, 4.9).
 *
 * These complement the unit-level NodePalette.test.tsx (palette grouping
 * and drag payloads in isolation) and WorkflowToolbar.test.tsx (toolbar
 * behavior in isolation) by exercising WorkflowBuilder end to end with
 * only apiService and the auth/usecase contexts mocked.
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
// apiService and context mocks
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
  generateWorkflow,
  listTestDatasets,
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
  generateWorkflow: vi.fn(),
  listTestDatasets: vi.fn(),
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
      generateWorkflow,
      listTestDatasets,
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
// jsdom stubs required by React Flow (it measures the canvas via
// ResizeObserver and reads zoom via DOMMatrixReadOnly, neither of which
// jsdom implements).
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

const FINDINGS: ValidationFinding[] = [
  {
    severity: 'error',
    code: 'V3_CYCLE',
    message: 'Workflow contains a cycle',
    nodeId: null,
    connectionId: null,
  },
  {
    severity: 'error',
    code: 'V4_MISSING_REQUIRED_PARAMETER',
    message: "Required parameter 'modelName' has no value",
    nodeId: 'model_inference_1',
    connectionId: null,
  },
  {
    severity: 'warning',
    code: 'W2_TYPE_COERCION',
    message: 'Connection relies on implicit format conversion',
    nodeId: null,
    connectionId: 'camera_source_1.out->crop_1.in',
  },
];

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

beforeEach(() => {
  vi.clearAllMocks();
  getWorkflowNodeCatalog.mockResolvedValue({ nodeTypes: CATALOG });
  listWorkflows.mockResolvedValue({ workflows: [], count: 0 });
  listModels.mockResolvedValue({ models: [], count: 0, usecase_id: 'uc-1' });
  listTestDatasets.mockResolvedValue({ datasets: [], count: 0 });
  useAuthMock.mockReturnValue({ user: { role: 'DataScientist' } });
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
});

// --------------------------------------------------------------------------
// Requirement 1.1: palette with five sections sourced from the catalog
// --------------------------------------------------------------------------

describe('WorkflowBuilder palette rendering (Requirement 1.1)', () => {
  it('fetches the node catalog and renders the palette with all five sections', async () => {
    renderBuilder();

    const palette = await screen.findByRole('navigation', { name: 'Node palette' });
    expect(getWorkflowNodeCatalog).toHaveBeenCalledTimes(1);

    const sections = ['Input', 'Preprocessing', 'Model inference', 'Post-processing', 'Output'];
    for (const label of sections) {
      expect(within(palette).getByRole('region', { name: label })).toBeInTheDocument();
    }
  });

  it('lists each catalog-sourced node type under its section', async () => {
    renderBuilder();
    const palette = await screen.findByRole('navigation', { name: 'Node palette' });

    const expected: Array<[string, string]> = [
      ['Input', 'Camera source'],
      ['Preprocessing', 'Crop'],
      ['Model inference', 'Model inference'],
      ['Post-processing', 'Inference filter'],
      ['Output', 'MQTT publish'],
    ];
    for (const [section, item] of expected) {
      const region = within(palette).getByRole('region', { name: section });
      expect(
        within(region).getByRole('listitem', { name: `${item} node type` })
      ).toBeInTheDocument();
    }
  });

  it('shows an error instead of the palette when the catalog fails to load', async () => {
    getWorkflowNodeCatalog.mockRejectedValue(new Error('catalog unavailable'));
    renderBuilder();

    expect(await screen.findByText('catalog unavailable')).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Node palette' })).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Requirements 4.8, 4.9: validate action through the page composition
// --------------------------------------------------------------------------

describe('WorkflowBuilder validate action (Requirements 4.8, 4.9)', () => {
  beforeEach(() => {
    getWorkflow.mockResolvedValue({
      workflow: {
        workflow_id: 'wf-1',
        usecase_id: 'uc-1',
        name: 'Line inspection',
        description: '',
        created_at: 1,
        updated_at: 1,
        latest_version: 3,
      },
      version: 3,
      definition: SAVED_DEFINITION,
    });
  });

  it('invokes backend validation for the loaded workflow and displays the complete findings list', async () => {
    validateWorkflow.mockResolvedValue({
      workflow_id: 'wf-1',
      version: 3,
      passed: false,
      validation_status: { status: 'failed' },
      findings: FINDINGS,
      error_count: 2,
      warning_count: 1,
    });
    renderBuilder('/workflows/builder/wf-1');

    // The deep-linked workflow is loaded onto the canvas first.
    expect(await screen.findByText('Line inspection (v3)')).toBeInTheDocument();
    await waitFor(() => expect(getWorkflow).toHaveBeenCalledWith('wf-1'));

    fireEvent.click(screen.getByRole('button', { name: 'Validate' }));

    await waitFor(() => expect(validateWorkflow).toHaveBeenCalledWith('wf-1', 3));
    // The canvas carries no unsaved changes, so nothing is re-saved.
    expect(updateWorkflow).not.toHaveBeenCalled();

    expect(
      await screen.findByText('Validation failed: 2 errors, 1 warning')
    ).toBeInTheDocument();
    const list = screen.getByRole('list', { name: 'Validation findings' });
    const items = list.querySelectorAll('li');
    expect(items).toHaveLength(FINDINGS.length);
    expect(items[0].textContent).toContain('[error] V3_CYCLE: Workflow contains a cycle');
    expect(items[1].textContent).toContain(
      "[error] V4_MISSING_REQUIRED_PARAMETER: Required parameter 'modelName' has no value (node: model_inference_1)"
    );
    expect(items[2].textContent).toContain(
      '[warning] W2_TYPE_COERCION: Connection relies on implicit format conversion' +
        ' (connection: camera_source_1.out->crop_1.in)'
    );
  });

  it('displays a passing result with zero findings', async () => {
    validateWorkflow.mockResolvedValue({
      workflow_id: 'wf-1',
      version: 3,
      passed: true,
      validation_status: { status: 'passed' },
      findings: [],
      error_count: 0,
      warning_count: 0,
    });
    renderBuilder('/workflows/builder/wf-1');

    expect(await screen.findByText('Line inspection (v3)')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }));

    await waitFor(() => expect(validateWorkflow).toHaveBeenCalledWith('wf-1', 3));
    expect(await screen.findByText('Validation passed (0 warnings)')).toBeInTheDocument();
    expect(screen.getByText('No findings.')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Requirement 1.5: the per-node trash button deletes the node and its
// attached connections through the canvas deletion path.
// --------------------------------------------------------------------------

describe('WorkflowBuilder node delete button (Requirement 1.5)', () => {
  beforeEach(() => {
    getWorkflow.mockResolvedValue({
      workflow: {
        workflow_id: 'wf-2',
        usecase_id: 'uc-1',
        name: 'Deletable',
        description: '',
        created_at: 1,
        updated_at: 1,
        latest_version: 1,
      },
      version: 1,
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
            id: 'crop_1',
            type: 'crop',
            position: { x: 300, y: 0 },
            parameters: {},
          },
        ],
        connections: [
          {
            id: 'c1',
            from: { node: 'camera_source_1', port: 'out' },
            to: { node: 'crop_1', port: 'in' },
          },
        ],
      } satisfies WorkflowDefinition,
    });
  });

  it('removes the node and its attached connections when its trash button is clicked', async () => {
    const { container } = renderBuilder('/workflows/builder/wf-2');
    expect(await screen.findByText('Deletable (v1)')).toBeInTheDocument();

    // Both nodes and the connecting edge are on the canvas.
    await waitFor(() => {
      expect(container.querySelector('.react-flow__node[data-id="camera_source_1"]')).not.toBeNull();
      expect(container.querySelector('.react-flow__node[data-id="crop_1"]')).not.toBeNull();
      expect(container.querySelectorAll('.react-flow__edge')).toHaveLength(1);
    });

    const cameraNode = container.querySelector(
      '.react-flow__node[data-id="camera_source_1"]'
    ) as HTMLElement;
    fireEvent.click(within(cameraNode).getByRole('button', { name: 'Delete node' }));

    // The node is removed along with its attached connection; the other
    // node survives (Requirement 1.5).
    await waitFor(() => {
      expect(container.querySelector('.react-flow__node[data-id="camera_source_1"]')).toBeNull();
      expect(container.querySelectorAll('.react-flow__edge')).toHaveLength(0);
    });
    expect(container.querySelector('.react-flow__node[data-id="crop_1"]')).not.toBeNull();
  });
});

// --------------------------------------------------------------------------
// Side drawer: the Generate and Test panels live in a collapsible drawer
// beside the canvas (collapsed by default), reachable without scrolling
// above the canvas (Requirements 10.1, 12.1 placement).
// --------------------------------------------------------------------------

describe('WorkflowBuilder side drawer', () => {
  it('keeps the Generate and Test panels in a collapsed drawer beside the canvas', async () => {
    renderBuilder();
    await screen.findByRole('navigation', { name: 'Node palette' });

    // Collapsed by default: only the slim toggle strip, no panel content.
    const drawer = screen.getByRole('complementary', { name: 'Workflow side panels' });
    expect(within(drawer).getByRole('button', { name: 'Open the Generate workflow panel' }))
      .toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Run test' })).toBeNull();

    // Opening via the strip shows the Generate panel beside the canvas.
    fireEvent.click(screen.getByRole('button', { name: 'Open the Generate workflow panel' }));
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();

    // Switching the segmented control shows the Test panel and loads the
    // datasets of the selected use case.
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    expect(await screen.findByRole('button', { name: 'Run test' })).toBeInTheDocument();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalledWith('uc-1'));

    // Collapsing returns to the slim strip; panel content is hidden again.
    fireEvent.click(screen.getByRole('button', { name: 'Collapse the side panel' }));
    expect(screen.queryByRole('button', { name: 'Run test' })).toBeNull();
    expect(
      screen.getByRole('button', { name: 'Open the Test workflow panel' })
    ).toBeInTheDocument();
  });

  it('opens directly on the Test tab from its strip toggle', async () => {
    renderBuilder();
    await screen.findByRole('navigation', { name: 'Node palette' });

    fireEvent.click(screen.getByRole('button', { name: 'Open the Test workflow panel' }));
    expect(await screen.findByRole('button', { name: 'Run test' })).toBeInTheDocument();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalledWith('uc-1'));
  });
});
