/**
 * Component tests for the workflow test panel (task 11.6): the test
 * action inside the builder's side drawer (Requirement 12.1; drawer
 * collapse itself is covered by WorkflowBuilder tests), deferred dataset
 * loading via the `active` prop, the dataset picker scoped to the Use_Case
 * and the uploader for new sample inputs (12.2), and stub identification
 * in the report — the "Simulated" badge plus the limitation text that
 * stubbed nodes were simulated rather than actuated (12.8).
 *
 * Task 11.7 deepens the coverage: the full presigned-multipart upload
 * flow (initiate, part PUTs with ETags, finalize) and upload rejection
 * surfacing the backend's reason (12.2), run polling until completion,
 * failed-run display (failure message, failing node, timeout indication),
 * role gating of the test action, dataset-list error handling, and the
 * Run test gating on dataset selection (12.1, 12.2, 12.8).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import TestPanel, {
  ACTIVE_TEST_RUN_STORAGE_KEY,
  canTestWorkflows,
  parseSimulatedConfidence,
  stubActivityNotes,
} from './TestPanel';
import type { UserRole } from '../../types';
import type { WorkflowMeta } from './WorkflowToolbar';
import type { TestRunNodeResult, WorkflowDefinition, WorkflowTestRun } from './types';

const { listTestDatasets, createTestDataset, finalizeTestDataset, startTestRun, getTestRun } =
  vi.hoisted(() => ({
    listTestDatasets: vi.fn(),
    createTestDataset: vi.fn(),
    finalizeTestDataset: vi.fn(),
    startTestRun: vi.fn(),
    getTestRun: vi.fn(),
  }));

vi.mock('../../services/api', () => ({
  apiService: {
    listTestDatasets,
    createTestDataset,
    finalizeTestDataset,
    startTestRun,
    getTestRun,
  },
}));

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const WORKFLOW: WorkflowMeta = {
  workflowId: 'wf-1',
  name: 'Line inspection',
  description: '',
  version: 3,
};

const DATASETS = [
  {
    dataset_id: 'ds-1',
    usecase_id: 'uc-1',
    name: 'Sample defects',
    file_count: 12,
  },
  {
    dataset_id: 'ds-2',
    usecase_id: 'uc-1',
    name: 'Golden images',
    file_count: 4,
  },
];

function testRun(overrides: Partial<WorkflowTestRun> = {}): WorkflowTestRun {
  return {
    test_run_id: 'run-1',
    workflow_id: 'wf-1',
    version: 3,
    usecase_id: 'uc-1',
    dataset_id: 'ds-1',
    status: 'completed',
    failure: null,
    started_at: 1,
    finished_at: 2,
    ...overrides,
  };
}

function nodeResult(overrides: Partial<TestRunNodeResult>): TestRunNodeResult {
  return {
    nodeId: 'node-1',
    status: 'completed',
    outputs: [],
    stubActivity: [],
    error: null,
    ...overrides,
  };
}

function renderPanel(overrides: {
  role?: UserRole;
  usecaseId?: string | null;
  workflow?: WorkflowMeta | null;
  pollIntervalMs?: number;
  getDefinition?: () => WorkflowDefinition;
} = {}) {
  return render(
    <TestPanel
      role={overrides.role ?? 'DataScientist'}
      usecaseId={overrides.usecaseId === undefined ? 'uc-1' : overrides.usecaseId}
      workflow={overrides.workflow === undefined ? WORKFLOW : overrides.workflow}
      getDefinition={overrides.getDefinition}
      pollIntervalMs={overrides.pollIntervalMs ?? 1}
    />
  );
}

/** A Workflow_Definition with (or without) a model_inference node. */
function definitionWith(nodeTypes: string[]): WorkflowDefinition {
  return {
    schemaVersion: 1,
    nodes: nodeTypes.map((type, index) => ({
      id: `${type}_${index}`,
      type,
      position: { x: index, y: 0 },
      parameters: {},
    })),
    connections: [],
  };
}

/** Selects the dataset option matching `name` in the dataset picker. */
async function selectDataset(name: RegExp | string = /Sample defects/) {
  const picker = screen.getByRole('button', { name: /Choose a test dataset/ });
  fireEvent.mouseDown(picker);
  fireEvent.click(picker);
  const option = await screen.findByRole('option', { name });
  fireEvent.mouseDown(option);
  fireEvent.mouseUp(option);
  fireEvent.click(option);
}

/** True when a Cloudscape button is disabled (native or aria). */
function isDisabled(button: HTMLElement): boolean {
  return (
    (button as HTMLButtonElement).disabled === true ||
    button.getAttribute('aria-disabled') === 'true'
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  listTestDatasets.mockResolvedValue({ datasets: DATASETS, count: DATASETS.length });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --------------------------------------------------------------------------
// Test action (Requirement 12.1)
// --------------------------------------------------------------------------

describe('TestPanel test action', () => {
  it('provides the test action panel with a Run test button (12.1)', async () => {
    renderPanel();
    expect(screen.getByRole('region', { name: 'Test workflow panel' })).toBeInTheDocument();
    expect(screen.getByText('Test workflow')).toBeInTheDocument();
    // No large heading is used for the panel title.
    expect(screen.queryByRole('heading', { name: 'Test workflow' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Run test' })).toBeInTheDocument();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
  });

  it('keeps Run test unavailable until the workflow is saved and a dataset is chosen', async () => {
    renderPanel({ workflow: null });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    const runButton = screen.getByRole('button', { name: 'Run test' });
    expect(
      (runButton as HTMLButtonElement).disabled === true ||
        runButton.getAttribute('aria-disabled') === 'true'
    ).toBe(true);
  });
});

// --------------------------------------------------------------------------
// Drawer activation: dataset loading is deferred until the panel is
// active, and re-activation never reloads (state preservation, 12.2)
// --------------------------------------------------------------------------

describe('TestPanel activation', () => {
  const props = {
    role: 'DataScientist' as UserRole,
    usecaseId: 'uc-1',
    workflow: WORKFLOW,
    pollIntervalMs: 1,
  };

  it('defers dataset loading until the panel becomes active', async () => {
    const { rerender } = render(<TestPanel {...props} active={false} />);
    expect(listTestDatasets).not.toHaveBeenCalled();

    rerender(<TestPanel {...props} active />);
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalledWith('uc-1'));
  });

  it('does not reload datasets when re-activated for the same use case', async () => {
    const { rerender } = render(<TestPanel {...props} active />);
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalledTimes(1));

    // Drawer collapsed and reopened (or tab switched away and back):
    // the dataset list is not refetched, preserving the selection.
    rerender(<TestPanel {...props} active={false} />);
    rerender(<TestPanel {...props} active />);
    expect(listTestDatasets).toHaveBeenCalledTimes(1);
  });
});

// --------------------------------------------------------------------------
// Dataset picker and uploader (Requirement 12.2)
// --------------------------------------------------------------------------

describe('TestPanel dataset selection and upload', () => {
  it('prompts with the Test_Datasets scoped to the selected Use_Case (12.2)', async () => {
    renderPanel();

    await waitFor(() => expect(listTestDatasets).toHaveBeenCalledWith('uc-1'));

    // The picker offers exactly the datasets of the Use_Case.
    const picker = screen.getByRole('button', { name: /Choose a test dataset/ });
    fireEvent.mouseDown(picker);
    fireEvent.click(picker);
    await waitFor(() => {
      expect(screen.getByText('Sample defects')).toBeInTheDocument();
      expect(screen.getByText('Golden images')).toBeInTheDocument();
    });
  });

  it('offers an uploader for new sample inputs accepting JPEG/PNG (12.2)', async () => {
    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: /Upload a new dataset/ }));

    const fileInput = screen.getByLabelText('Test dataset files') as HTMLInputElement;
    expect(fileInput.type).toBe('file');
    expect(fileInput.accept).toContain('image/jpeg');
    expect(fileInput.accept).toContain('image/png');
    expect(fileInput.multiple).toBe(true);
    expect(screen.getByLabelText('Test dataset name')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload dataset' })).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Stub identification in the report (Requirement 12.8)
// --------------------------------------------------------------------------

describe('TestPanel report stub identification', () => {
  async function runTestToCompletion(nodeResults: TestRunNodeResult[]) {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'running' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({ status: 'completed' }),
      node_results: nodeResults,
    });

    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    // Select a dataset, then run.
    const picker = screen.getByRole('button', { name: /Choose a test dataset/ });
    fireEvent.mouseDown(picker);
    fireEvent.click(picker);
    const option = await screen.findByRole('option', { name: /Sample defects/ });
    fireEvent.mouseDown(option);
    fireEvent.mouseUp(option);
    fireEvent.click(option);
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await waitFor(() =>
      expect(startTestRun).toHaveBeenCalledWith('wf-1', { dataset_id: 'ds-1', version: 3 })
    );
    await screen.findByText('Test run completed');
  }

  it('marks stubbed nodes with a Simulated badge and shows the limitation text (12.8)', async () => {
    await runTestToCompletion([
      nodeResult({ nodeId: 'camera_source_1', stubActivity: [{ type: 'frames_fed' }] }),
      nodeResult({ nodeId: 'model_inference_1', outputs: [{ label: 'defect' }] }),
      nodeResult({ nodeId: 'digital_output_1', stubActivity: [{ type: 'recorded_actuation' }] }),
    ]);

    const report = screen.getByRole('list', { name: 'Per-node test results' });
    const items = within(report).getAllByRole('listitem');
    expect(items).toHaveLength(3);

    // Exactly the stubbed nodes carry the "Simulated" badge.
    const badged = items.filter((item) => within(item).queryByText('Simulated') !== null);
    expect(badged.map((item) => item.textContent)).toEqual([
      expect.stringContaining('camera_source_1'),
      expect.stringContaining('digital_output_1'),
    ]);
    expect(within(items[1]).queryByText('Simulated')).toBeNull();

    // The limitation is described: simulated rather than actuated (12.8).
    expect(screen.getByText('Some nodes were simulated')).toBeInTheDocument();
    expect(
      screen.getByText(/executed with stubs/)
    ).toBeInTheDocument();
    // Per-node limitation text on each stubbed node.
    expect(within(items[0]).getByText(/hardware was not actuated/)).toBeInTheDocument();
    expect(within(items[2]).getByText(/hardware was not actuated/)).toBeInTheDocument();
  });

  it('shows no Simulated badge or limitation text when no node was stubbed', async () => {
    await runTestToCompletion([
      nodeResult({ nodeId: 'folder_source_1' }),
      nodeResult({ nodeId: 'capture_1', outputs: [{ frame: 's3://x' }] }),
    ]);

    expect(screen.queryByText('Simulated')).toBeNull();
    expect(screen.queryByText('Some nodes were simulated')).toBeNull();
  });

  // ------------------------------------------------------------------
  // Stubbed Custom_Node_Type limitation display (custom-node-designer
  // task 13.3, Requirement 12.3): the harness records the limitation
  // note with the custom_node_stub activity; the report displays it.
  // ------------------------------------------------------------------

  /** The note the sandbox harness records on stubbed Custom_Node_Types
   *  (harness.py CUSTOM_NODE_STUB_NOTE). */
  const CUSTOM_NODE_STUB_NOTE =
    'Simulated: this custom node has no x86_64 build; a pass-through ' +
    'stub recorded the frames the node would have consumed and passed ' +
    'them through unchanged';

  it('describes that a stubbed Custom_Node_Type was simulated because no x86_64 build exists (custom-node-designer 12.3)', async () => {
    await runTestToCompletion([
      nodeResult({ nodeId: 'folder_source_1' }),
      nodeResult({
        nodeId: 'custom_blur_1',
        stubActivity: [
          {
            type: 'custom_node_stub',
            element: 'custom_stub_custom_blur_1',
            frameCount: 3,
            note: CUSTOM_NODE_STUB_NOTE,
          },
        ],
      }),
    ]);

    const report = screen.getByRole('list', { name: 'Per-node test results' });
    const items = within(report).getAllByRole('listitem');
    const stubbedItem = items.find((item) => item.textContent?.includes('custom_blur_1'));
    expect(stubbedItem).toBeDefined();

    // The stubbed custom node carries the Simulated badge and the
    // recorded limitation note: simulated because no x86_64 build exists.
    expect(within(stubbedItem!).getByText('Simulated')).toBeInTheDocument();
    expect(within(stubbedItem!).getByText(/no x86_64 build/)).toBeInTheDocument();
    expect(within(stubbedItem!).getByText(/pass-through/)).toBeInTheDocument();
    // The note replaces the generic hardware-not-actuated fallback text.
    expect(within(stubbedItem!).queryByText(/hardware was not actuated/)).toBeNull();
    // The run-level simulated-nodes limitation alert still appears (12.8).
    expect(screen.getByText('Some nodes were simulated')).toBeInTheDocument();
  });

  it('extracts distinct note strings from stub activity entries', () => {
    expect(
      stubActivityNotes(
        nodeResult({
          stubActivity: [
            { type: 'custom_node_stub', note: CUSTOM_NODE_STUB_NOTE },
            { type: 'custom_node_stub', note: CUSTOM_NODE_STUB_NOTE },
            { type: 'frames_fed' },
            'not-an-object',
            { note: 42 },
          ],
        })
      )
    ).toEqual([CUSTOM_NODE_STUB_NOTE]);
    expect(stubActivityNotes(nodeResult({ stubActivity: [] }))).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// Upload flow: initiate -> part PUTs with ETags -> finalize (12.2)
// --------------------------------------------------------------------------

describe('TestPanel upload flow', () => {
  /** Expands the uploader and fills the name and file inputs. */
  async function fillUploadForm(name: string, files: File[]) {
    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: /Upload a new dataset/ }));
    fireEvent.change(screen.getByLabelText('Test dataset name'), {
      target: { value: name },
    });
    fireEvent.change(screen.getByLabelText('Test dataset files'), {
      target: { files },
    });
  }

  it('runs initiate, PUTs every part collecting ETags, then finalizes (12.2)', async () => {
    // Two parts of 4 bytes for one 8-byte file.
    createTestDataset.mockResolvedValue({
      dataset_id: 'ds-new',
      usecase_id: 'uc-1',
      name: 'My samples',
      s3_prefix: 'test-datasets/uc-1/ds-new/',
      upload: {
        files: [
          {
            name: 'img1.png',
            key: 'test-datasets/uc-1/ds-new/img1.png',
            upload_id: 'mu-1',
            part_size: 4,
            parts: [
              { part_number: 1, url: 'https://s3.example/part-1' },
              { part_number: 2, url: 'https://s3.example/part-2' },
            ],
          },
        ],
        expires_in: 900,
      },
    });
    finalizeTestDataset.mockResolvedValue({
      dataset: { dataset_id: 'ds-new', usecase_id: 'uc-1', name: 'My samples', file_count: 1 },
    });

    let putCount = 0;
    const fetchMock = vi.fn(async () => {
      putCount += 1;
      const etag = `"etag-${putCount}"`;
      return {
        ok: true,
        status: 200,
        headers: { get: (header: string) => (header === 'ETag' ? etag : null) },
      } as unknown as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    await fillUploadForm('My samples', [
      new File(['abcdefgh'], 'img1.png', { type: 'image/png' }),
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Upload dataset' }));

    // 1. Initiate declares the file set scoped to the Use_Case.
    await waitFor(() =>
      expect(createTestDataset).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        name: 'My samples',
        files: [{ name: 'img1.png', size: 8, content_type: 'image/png' }],
      })
    );

    // 2. Every part was PUT to its presigned URL.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://s3.example/part-1',
      expect.objectContaining({ method: 'PUT' })
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://s3.example/part-2',
      expect.objectContaining({ method: 'PUT' })
    );

    // 3. Finalize carries the collected ETags per part.
    await waitFor(() =>
      expect(finalizeTestDataset).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        dataset_id: 'ds-new',
        name: 'My samples',
        files: [
          {
            key: 'test-datasets/uc-1/ds-new/img1.png',
            upload_id: 'mu-1',
            parts: [
              { part_number: 1, etag: '"etag-1"' },
              { part_number: 2, etag: '"etag-2"' },
            ],
          },
        ],
      })
    );

    // The new dataset is reported and becomes the selected option (12.3).
    await screen.findByText("Dataset 'My samples' uploaded");
    expect(screen.getByText('My samples')).toBeInTheDocument();
  });

  it('surfaces the backend rejection reason and persists nothing client-side (12.2)', async () => {
    createTestDataset.mockRejectedValue(
      new Error('Unsupported file format: notes.gif (only JPEG/PNG are accepted)')
    );
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await fillUploadForm('Bad set', [new File(['x'], 'notes.gif', { type: 'image/gif' })]);
    fireEvent.click(screen.getByRole('button', { name: 'Upload dataset' }));

    // The backend's reason is displayed, with the no-persistence note.
    await screen.findByText('Upload rejected');
    expect(
      screen.getByText(/Unsupported file format: notes\.gif \(only JPEG\/PNG are accepted\)/)
    ).toBeInTheDocument();
    expect(screen.getByText(/No dataset was saved\./)).toBeInTheDocument();

    // Nothing was uploaded or finalized, and no dataset joined the picker.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(finalizeTestDataset).not.toHaveBeenCalled();
    expect(screen.queryByText('Bad set')).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Run polling and failed-run display (12.1)
// --------------------------------------------------------------------------

describe('TestPanel run polling and failure display', () => {
  /** Selects a dataset and clicks Run test with the given poll responses. */
  async function startRun(pollResponses: Array<{ run: WorkflowTestRun; results?: TestRunNodeResult[] }>) {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'pending' }) });
    for (const response of pollResponses) {
      getTestRun.mockResolvedValueOnce({
        test_run: response.run,
        node_results: response.results ?? [],
      });
    }

    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));
  }

  it('polls the test run until it completes, then shows the report', async () => {
    await startRun([
      { run: testRun({ status: 'pending' }) },
      { run: testRun({ status: 'running' }) },
      {
        run: testRun({ status: 'completed' }),
        results: [nodeResult({ nodeId: 'folder_source_1' })],
      },
    ]);

    await screen.findByText('Test run completed');
    expect(getTestRun).toHaveBeenCalledTimes(3);
    expect(getTestRun).toHaveBeenCalledWith('run-1');
    expect(screen.getByText('folder_source_1')).toBeInTheDocument();
  });

  it('shows the failure message and failing node, keeping partial results', async () => {
    await startRun([
      {
        run: testRun({
          status: 'failed',
          failure: {
            nodeId: 'model_inference_1',
            message: 'Model artifact could not be loaded',
            timeout: false,
          },
        }),
        results: [nodeResult({ nodeId: 'folder_source_1' })],
      },
    ]);

    await screen.findByText('Test run failed');
    expect(screen.getByText(/Model artifact could not be loaded/)).toBeInTheDocument();
    expect(screen.getByText(/node: model_inference_1/)).toBeInTheDocument();
    // Partial per-node results produced before the failure remain visible.
    expect(screen.getByText('folder_source_1')).toBeInTheDocument();
  });

  it('indicates a timeout when the run failed with a timeout', async () => {
    await startRun([
      {
        run: testRun({
          status: 'failed',
          failure: {
            nodeId: null,
            message: 'Pipeline execution exceeded 10 minutes',
            timeout: true,
          },
        }),
      },
    ]);

    await screen.findByText('Test run failed: timeout');
    expect(screen.getByText(/Pipeline execution exceeded 10 minutes/)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Live progress while a run executes
// --------------------------------------------------------------------------

describe('TestPanel live run progress', () => {
  it('shows "Test in progress" with the progress entries and partial node count', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'pending' }) });
    // The run stays in-flight: a long poll interval keeps this detail
    // rendered so the live-progress view can be asserted.
    getTestRun.mockResolvedValue({
      test_run: testRun({
        status: 'running',
        progress: [
          { at: 1000, message: 'Validating the workflow definition' },
          { at: 2000, message: 'Validation passed' },
          { at: 3000, message: 'Compiling for x86_64 (simulation mode)' },
          { at: 4000, message: 'Compilation succeeded' },
          { at: 5000, message: 'Starting the sandbox container...' },
        ],
      }),
      node_results: [
        nodeResult({ nodeId: 'camera_source_1' }),
        nodeResult({ nodeId: 'model_inference_1' }),
      ],
    });

    renderPanel({ pollIntervalMs: 60_000 });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    // The in-progress greeting is exactly "Test in progress".
    await screen.findByText('Test in progress');
    expect(screen.queryByText(/Test run running/)).toBeNull();

    // The backend's step-level progress entries render as a list (most
    // recent last); with per-node results already flushed, the pipeline
    // execution count is the latest line.
    const log = screen.getByRole('list', { name: 'Test run progress log' });
    const messages = within(log)
      .getAllByRole('listitem')
      .map((item) => item.textContent);
    expect(messages).toEqual([
      'Validating the workflow definition',
      'Validation passed',
      'Compiling for x86_64 (simulation mode)',
      'Compilation succeeded',
      'Starting the sandbox container...',
      'Executing pipeline — 2 node(s) reported',
    ]);
  });

  it('shows the recorded progress entries in the final report', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'pending' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({
        status: 'completed',
        progress: [
          { at: 1000, message: 'Validating the workflow definition' },
          { at: 2000, message: 'Test run completed' },
        ],
      }),
      node_results: [nodeResult({ nodeId: 'folder_source_1' })],
    });

    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    // The report shows the recorded entries under the "Run log" section.
    await screen.findByText('Run log');
    const log = screen.getByRole('list', { name: 'Test run progress log' });
    const messages = within(log)
      .getAllByRole('listitem')
      .map((item) => item.textContent);
    expect(messages).toEqual([
      'Validating the workflow definition',
      'Test run completed',
    ]);
  });

  it('keeps the distinct starting wording before the first poll response', async () => {
    startTestRun.mockReturnValue(new Promise(() => {})); // never resolves

    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await screen.findByText('Starting test...');
    expect(screen.queryByText('Test in progress')).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Run resumption across navigation (localStorage persistence)
// --------------------------------------------------------------------------

describe('TestPanel run resumption across navigation', () => {
  function persistRun(overrides: Partial<{ testRunId: string; workflowId: string; usecaseId: string }> = {}) {
    window.localStorage.setItem(
      ACTIVE_TEST_RUN_STORAGE_KEY,
      JSON.stringify({
        testRunId: 'run-42',
        workflowId: 'wf-1',
        usecaseId: 'uc-1',
        ...overrides,
      })
    );
  }

  it('persists the active run in localStorage when a run starts', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'pending' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({ status: 'running' }),
      node_results: [],
    });

    renderPanel({ pollIntervalMs: 60_000 });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await waitFor(() => {
      const raw = window.localStorage.getItem(ACTIVE_TEST_RUN_STORAGE_KEY);
      expect(raw).not.toBeNull();
      expect(JSON.parse(raw!)).toEqual({
        testRunId: 'run-1',
        workflowId: 'wf-1',
        usecaseId: 'uc-1',
      });
    });
  });

  it('clears the persisted run once the final report has been shown', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'pending' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({ status: 'completed' }),
      node_results: [nodeResult({ nodeId: 'folder_source_1' })],
    });

    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await screen.findByText('Test run completed');
    expect(window.localStorage.getItem(ACTIVE_TEST_RUN_STORAGE_KEY)).toBeNull();
  });

  it('resumes polling a persisted run on mount and restores the in-progress view', async () => {
    persistRun();
    getTestRun.mockResolvedValue({
      test_run: testRun({
        test_run_id: 'run-42',
        status: 'running',
        progress: [{ at: 1000, message: 'Validating the workflow definition' }],
      }),
      node_results: [],
    });

    renderPanel({ pollIntervalMs: 60_000 });

    await screen.findByText('Test in progress');
    expect(getTestRun).toHaveBeenCalledWith('run-42');
    // Resumption polls the existing run; no new run is started.
    expect(startTestRun).not.toHaveBeenCalled();
    expect(screen.getByText('Validating the workflow definition')).toBeInTheDocument();
  });

  it('shows the final report when the run finished while the user was away', async () => {
    persistRun();
    getTestRun.mockResolvedValue({
      test_run: testRun({ test_run_id: 'run-42', status: 'completed' }),
      node_results: [nodeResult({ nodeId: 'folder_source_1' })],
    });

    renderPanel();

    await screen.findByText('Test run completed');
    expect(getTestRun).toHaveBeenCalledWith('run-42');
    expect(startTestRun).not.toHaveBeenCalled();
    expect(screen.getByText('folder_source_1')).toBeInTheDocument();
    // The result has been seen: the marker is cleared.
    expect(window.localStorage.getItem(ACTIVE_TEST_RUN_STORAGE_KEY)).toBeNull();
  });

  it('ignores a persisted run belonging to another use case', async () => {
    persistRun({ usecaseId: 'uc-other' });

    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    expect(getTestRun).not.toHaveBeenCalled();
    // The marker stays for the Use_Case it belongs to.
    expect(window.localStorage.getItem(ACTIVE_TEST_RUN_STORAGE_KEY)).not.toBeNull();
  });
});

// --------------------------------------------------------------------------
// Role gating and Run test gating (12.1)
// --------------------------------------------------------------------------

describe('TestPanel gating', () => {
  it('permits testing only for DataScientist, UseCaseAdmin, and PortalAdmin', () => {
    expect(canTestWorkflows('DataScientist')).toBe(true);
    expect(canTestWorkflows('UseCaseAdmin')).toBe(true);
    expect(canTestWorkflows('PortalAdmin')).toBe(true);
    expect(canTestWorkflows('Operator')).toBe(false);
    expect(canTestWorkflows('Viewer')).toBe(false);
    expect(canTestWorkflows(undefined)).toBe(false);
  });

  it.each(['Viewer', 'Operator'] as const)(
    'keeps Run test unavailable for %s even with a dataset selected',
    async (role) => {
      renderPanel({ role });
      await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
      await selectDataset();

      expect(isDisabled(screen.getByRole('button', { name: 'Run test' }))).toBe(true);
      fireEvent.click(screen.getByRole('button', { name: 'Run test' }));
      expect(startTestRun).not.toHaveBeenCalled();
    }
  );

  it('enables Run test only once a dataset is selected', async () => {
    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    expect(isDisabled(screen.getByRole('button', { name: 'Run test' }))).toBe(true);
    await selectDataset();
    expect(isDisabled(screen.getByRole('button', { name: 'Run test' }))).toBe(false);
  });
});

// --------------------------------------------------------------------------
// Simulated inference outcome (Requirement 12.6): the model is not
// executed in cloud tests; the user configures the injected outcome
// --------------------------------------------------------------------------

describe('TestPanel simulated inference outcome', () => {
  const WITH_INFERENCE = () => definitionWith(['folder_source', 'model_inference', 'capture']);
  const WITH_BEDROCK = () => definitionWith(['folder_source', 'bedrock_inference', 'capture']);
  const WITHOUT_INFERENCE = () => definitionWith(['folder_source', 'capture']);

  it('parses the confidence input strictly to 0..1', () => {
    expect(parseSimulatedConfidence('0.9')).toBe(0.9);
    expect(parseSimulatedConfidence('0')).toBe(0);
    expect(parseSimulatedConfidence('1')).toBe(1);
    expect(parseSimulatedConfidence('')).toBeNull();
    expect(parseSimulatedConfidence('1.5')).toBeNull();
    expect(parseSimulatedConfidence('-0.1')).toBeNull();
    expect(parseSimulatedConfidence('abc')).toBeNull();
  });

  it('hides the section when the workflow has no model_inference node', async () => {
    renderPanel({ getDefinition: WITHOUT_INFERENCE });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    expect(screen.queryByText('Simulated inference outcome')).toBeNull();
  });

  it('hides the section when no definition is provided', async () => {
    renderPanel();
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    expect(screen.queryByText('Simulated inference outcome')).toBeNull();
  });

  it('shows the section with help text explaining the model is not executed', async () => {
    renderPanel({ getDefinition: WITH_INFERENCE });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    expect(screen.getByText('Simulated inference outcome')).toBeInTheDocument();
    expect(screen.getByText(/model is not executed in cloud tests/)).toBeInTheDocument();
    expect(screen.getByText(/drives downstream nodes/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Anomaly detected' })).toBeInTheDocument();
    expect(screen.getByLabelText('Simulated confidence')).toBeInTheDocument();
  });

  it('shows the section for workflows with a bedrock_inference node', async () => {
    // The Bedrock model is stubbed in the sandbox too (no internet in
    // the sandbox VPC), so the configured outcome controls apply.
    renderPanel({ getDefinition: WITH_BEDROCK });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());

    expect(screen.getByText('Simulated inference outcome')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Anomaly detected' })).toBeInTheDocument();
  });

  it('sends the configured outcome for bedrock_inference workflows', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'running' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({ status: 'completed' }),
      node_results: [],
    });

    renderPanel({ getDefinition: WITH_BEDROCK });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Anomaly detected' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await waitFor(() =>
      expect(startTestRun).toHaveBeenCalledWith('wf-1', {
        dataset_id: 'ds-1',
        version: 3,
        simulated_inference: { is_anomalous: true, confidence: 0.9 },
      })
    );
  });

  it('sends the default outcome (not anomalous, 0.9) with startTestRun', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'running' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({ status: 'completed' }),
      node_results: [],
    });

    renderPanel({ getDefinition: WITH_INFERENCE });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await waitFor(() =>
      expect(startTestRun).toHaveBeenCalledWith('wf-1', {
        dataset_id: 'ds-1',
        version: 3,
        simulated_inference: { is_anomalous: false, confidence: 0.9 },
      })
    );
  });

  it('sends the configured outcome with startTestRun', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'running' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({ status: 'completed' }),
      node_results: [],
    });

    renderPanel({ getDefinition: WITH_INFERENCE });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();

    fireEvent.click(screen.getByRole('checkbox', { name: 'Anomaly detected' }));
    fireEvent.change(screen.getByLabelText('Simulated confidence'), {
      target: { value: '0.25' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await waitFor(() =>
      expect(startTestRun).toHaveBeenCalledWith('wf-1', {
        dataset_id: 'ds-1',
        version: 3,
        simulated_inference: { is_anomalous: true, confidence: 0.25 },
      })
    );
  });

  it('omits simulated_inference for workflows without a model_inference node', async () => {
    startTestRun.mockResolvedValue({ test_run: testRun({ status: 'running' }) });
    getTestRun.mockResolvedValue({
      test_run: testRun({ status: 'completed' }),
      node_results: [],
    });

    renderPanel({ getDefinition: WITHOUT_INFERENCE });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));

    await waitFor(() =>
      expect(startTestRun).toHaveBeenCalledWith('wf-1', { dataset_id: 'ds-1', version: 3 })
    );
  });

  it('keeps Run test unavailable while the confidence is invalid', async () => {
    renderPanel({ getDefinition: WITH_INFERENCE });
    await waitFor(() => expect(listTestDatasets).toHaveBeenCalled());
    await selectDataset();

    fireEvent.change(screen.getByLabelText('Simulated confidence'), {
      target: { value: '1.5' },
    });
    expect(screen.getByText('Enter a number between 0 and 1')).toBeInTheDocument();
    expect(isDisabled(screen.getByRole('button', { name: 'Run test' }))).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Run test' }));
    expect(startTestRun).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Simulated confidence'), {
      target: { value: '0.8' },
    });
    expect(isDisabled(screen.getByRole('button', { name: 'Run test' }))).toBe(false);
  });
});

// --------------------------------------------------------------------------
// Dataset list error handling (12.2)
// --------------------------------------------------------------------------

describe('TestPanel dataset list errors', () => {
  it('shows the error when the dataset list cannot be loaded', async () => {
    listTestDatasets.mockRejectedValue(new Error('Access denied for this use case'));

    renderPanel();

    await screen.findByText('Access denied for this use case');
  });
});
