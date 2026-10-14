/**
 * Bug 5 exploration test — workflow name shown while editing.
 *
 * Bugfix spec: workflow-manager-integration-bugfixes (Bug 5).
 *
 * While viewing or editing a saved workflow, the Workflow_Builder renders
 * a static top-of-page header reading "Workflow Builder" and only surfaces
 * the open workflow's name as small secondary text inside the toolbar
 * (`${workflow.name} (v${workflow.version})`). There is no prominent
 * workflow-name display at the top of the screen, so a user cannot tell
 * from the top of the view which workflow is open.
 *
 * This is an EXPLORATION test written against the UNFIXED code: it asserts
 * the CORRECTED behavior (Property 5 — Bug Condition), so it is EXPECTED TO
 * FAIL on the current page (the top-of-page header is the static
 * "Workflow Builder"; the name lives only in small toolbar text). The
 * failure confirms the bug exists.
 *
 * Property 5: Bug Condition — Workflow name shown while editing
 *   For any builder view where isBugCondition5 holds (a workflow is open
 *   but its name is not displayed at the top), the fixed builder SHALL
 *   display the open workflow's name at the top of the screen, and the
 *   displayed name SHALL equal the open workflow's name.
 *
 * Validates: Requirements 1.5, 2.5
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import WorkflowBuilder from './WorkflowBuilder';
import type { NodeTypeDescriptor, WorkflowDefinition } from './types';

// --------------------------------------------------------------------------
// apiService and context mocks (mirrors WorkflowBuilder.test.tsx)
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
// Fixtures: a minimal catalog and a saved workflow with a recognizable name
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
  }),
  descriptor('mqtt_publish', 'output', 'MQTT publish', {
    inputs: [{ name: 'in', portType: 'InferenceMeta' }],
  }),
];

/** The open workflow's name we expect surfaced at the top of the page. */
const WORKFLOW_NAME = 'Line inspection';

const SAVED_DEFINITION: WorkflowDefinition = {
  schemaVersion: 1,
  nodes: [
    {
      id: 'camera_source_1',
      type: 'camera_source',
      position: { x: 0, y: 0 },
      parameters: {},
    },
  ],
  connections: [],
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
  getWorkflow.mockResolvedValue({
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
  });
});

// --------------------------------------------------------------------------
// Property 5: Bug Condition — Workflow name shown while editing
// --------------------------------------------------------------------------

describe('WorkflowBuilder top-of-page workflow name (Bug 5, Requirements 1.5, 2.5)', () => {
  it('shows the open workflow name in the top-of-page header', async () => {
    renderBuilder('/workflows/builder/wf-1');

    // Wait for the deep-linked workflow to load. The toolbar's small
    // secondary text confirms the load completed (this is NOT the
    // top-of-page header — it is the toolbar detail text the bug leaves
    // as the only place the name appears).
    expect(await screen.findByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();
    await waitFor(() => expect(getWorkflow).toHaveBeenCalledWith('wf-1'));

    // The top-of-page header is the page's h3 heading (the palette section
    // labels are h4; the toolbar detail text is not a heading), so the h3
    // uniquely identifies the top-of-page header.
    const topHeader = screen.getByRole('heading', { level: 3 });

    // Fix-checking assertion (Property 5): the open workflow's name SHALL
    // appear in the top-of-page header.
    // EXPECTED OUTCOME on UNFIXED code: FAILS — the top header reads only
    // "Workflow Builder"; the name is absent from it (it lives only in the
    // small toolbar secondary text).
    expect(topHeader).toHaveTextContent(WORKFLOW_NAME);
  });

  it('the workflow name is present at the top of the page, not only in the toolbar', async () => {
    const { container } = renderBuilder('/workflows/builder/wf-1');

    await screen.findByText(`${WORKFLOW_NAME} (v3)`);

    // Scope to the toolbar to prove the name IS shown there today...
    const toolbar = screen.getByRole('toolbar', { name: 'Workflow actions' });
    expect(within(toolbar).getByText(`${WORKFLOW_NAME} (v3)`)).toBeInTheDocument();

    // ...but the top-of-page header (h3), which sits above the toolbar in
    // the DOM, must ALSO display the name for the user to identify the open
    // workflow from the top of the screen.
    const topHeader = screen.getByRole('heading', { level: 3 });
    expect(toolbar.contains(topHeader)).toBe(false);
    // EXPECTED OUTCOME on UNFIXED code: FAILS — the top header omits the name.
    expect(topHeader).toHaveTextContent(WORKFLOW_NAME);
    // Keep `container` referenced for parity with sibling tests' render shape.
    expect(container).toBeTruthy();
  });

  it('shows the "Untitled workflow" placeholder for a new/unsaved canvas without crashing', async () => {
    // No deep-link id: a fresh canvas with no workflow open.
    renderBuilder('/workflows/builder');

    // Wait for the catalog to load so the builder (and its header) render.
    expect(await screen.findByRole('toolbar', { name: 'Workflow actions' })).toBeInTheDocument();

    // A new/unsaved canvas never fetches a workflow...
    expect(getWorkflow).not.toHaveBeenCalled();

    // ...and the top-of-page header shows the neutral placeholder rather
    // than crashing on an absent name.
    const topHeader = screen.getByRole('heading', { level: 3 });
    expect(topHeader).toHaveTextContent('Untitled workflow');
    // The placeholder is not any real workflow's name.
    expect(topHeader).not.toHaveTextContent(WORKFLOW_NAME);
  });

  it('a deep-link /workflows/builder/{id} shows the name at the top once loaded', async () => {
    renderBuilder('/workflows/builder/wf-1');

    // Before the load resolves the header may show the placeholder; once the
    // deep-linked workflow loads, the top-of-page header displays its name.
    await waitFor(() => expect(getWorkflow).toHaveBeenCalledWith('wf-1'));
    await waitFor(() =>
      expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent(WORKFLOW_NAME)
    );
  });
});
