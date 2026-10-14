/**
 * Component tests for the prompt-based generation chat panel (task 10.3):
 * panel chrome inside the builder's side drawer (Requirement 10.1;
 * drawer collapse itself is covered by WorkflowBuilder tests), successful
 * generation rendering onto the canvas after client-side parse with the
 * backend validation findings displayed (10.3), session id maintained
 * across turns with the canvas snapshot sent for follow-ups, and failures
 * (API error or client-side parse failure) leaving the canvas untouched
 * with the error displayed and the prompt preserved for retry (10.4, 10.7).
 *
 * Generation is asynchronous (workflow-manager-gaps Requirement 4):
 * `generateWorkflow` resolves with a 202 submission `{job_id,
 * session_id, ...}` and the panel polls `getWorkflowGenerationJob`
 * every POLL_INTERVAL_MS until the Generation_Job stops. Tests run on
 * fake timers: `settle()` flushes the submit round-trip and
 * `advancePoll()` advances one 3-second poll cycle. A failed job
 * surfaces as a rejected poll replaying the Error_Envelope (Req 2.3),
 * so failure fixtures reject from `getWorkflowGenerationJob`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import GenerateChatPanel, { type GenerateChatPanelProps } from './GenerateChatPanel';
import type { UserRole } from '../../types';
import { POLL_INTERVAL_MS } from './generationPollReducer';
import type {
  GenerationStructuralError,
  NodeTypeDescriptor,
  WorkflowDefinition,
  WorkflowGenerationResult,
} from './types';

const { generateWorkflow, getWorkflowGenerationJob } = vi.hoisted(() => ({
  generateWorkflow: vi.fn(),
  getWorkflowGenerationJob: vi.fn(),
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
    apiService: { generateWorkflow, getWorkflowGenerationJob },
  };
});

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

function descriptor(overrides: Partial<NodeTypeDescriptor>): NodeTypeDescriptor {
  return {
    typeId: 'test_type',
    category: 'input',
    displayName: 'Test Type',
    inputs: [],
    outputs: [],
    parameters: [],
    mappings: [],
    hardwareDependent: false,
    ...overrides,
  };
}

const CATALOG: NodeTypeDescriptor[] = [
  descriptor({
    typeId: 'camera_source',
    category: 'input',
    displayName: 'Camera source',
    outputs: [{ name: 'out', portType: 'VideoFrames' }],
  }),
  descriptor({
    typeId: 'capture',
    category: 'output',
    displayName: 'Capture',
    inputs: [{ name: 'in', portType: 'VideoFrames' }],
  }),
];

const EMPTY_DEFINITION: WorkflowDefinition = {
  schemaVersion: 1,
  nodes: [],
  connections: [],
};

/** A generated definition whose node types all exist in the catalog. */
const GENERATED_DEFINITION: WorkflowDefinition = {
  schemaVersion: 1,
  nodes: [
    { id: 'camera_source_1', type: 'camera_source', position: { x: 0, y: 0 }, parameters: {} },
    { id: 'capture_1', type: 'capture', position: { x: 200, y: 0 }, parameters: {} },
  ],
  connections: [
    {
      id: 'camera_source_1.out->capture_1.in',
      from: { node: 'camera_source_1', port: 'out' },
      to: { node: 'capture_1', port: 'in' },
    },
  ],
};

function generationResult(
  overrides: Partial<WorkflowGenerationResult> = {}
): WorkflowGenerationResult {
  return {
    session_id: 'session-1',
    usecase_id: 'uc-1',
    definition: GENERATED_DEFINITION,
    findings: [],
    error_count: 0,
    warning_count: 0,
    validation_passed: true,
    assistant_text: 'Here is your pipeline.',
    ...overrides,
  };
}

/** The 202 submission envelope returned by POST /workflows/generate. */
function submission(overrides: Partial<{ job_id: string; session_id: string }> = {}) {
  return {
    job_id: 'job-1',
    session_id: 'session-1',
    usecase_id: 'uc-1',
    status: 'pending' as const,
    ...overrides,
  };
}

/** A succeeded Generation_Job: the sync payload beside the job identity. */
function succeededJob(overrides: Partial<WorkflowGenerationResult> = {}) {
  return {
    job_id: 'job-1',
    status: 'succeeded' as const,
    ...generationResult(overrides),
  };
}

type ApplyGeneratedMock = ReturnType<typeof vi.fn<GenerateChatPanelProps['onApplyGenerated']>>;

function renderPanel(overrides: {
  role?: UserRole;
  usecaseId?: string | null;
  getDefinition?: () => WorkflowDefinition;
  onApplyGenerated?: ApplyGeneratedMock;
} = {}) {
  const onApplyGenerated: ApplyGeneratedMock =
    overrides.onApplyGenerated ?? vi.fn<GenerateChatPanelProps['onApplyGenerated']>();
  const utils = render(
    <GenerateChatPanel
      role={overrides.role ?? 'DataScientist'}
      usecaseId={overrides.usecaseId === undefined ? 'uc-1' : overrides.usecaseId}
      catalog={CATALOG}
      getDefinition={overrides.getDefinition ?? (() => EMPTY_DEFINITION)}
      onApplyGenerated={onApplyGenerated}
    />
  );
  return { ...utils, onApplyGenerated };
}

function promptInput(): HTMLTextAreaElement {
  const input = document.querySelector(
    'textarea[aria-label="Workflow generation prompt"]'
  );
  expect(input).not.toBeNull();
  return input as HTMLTextAreaElement;
}

function send(prompt: string) {
  fireEvent.change(promptInput(), { target: { value: prompt } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
}

/** Flush the submit round-trip (202 + first poll scheduling). */
async function settle() {
  await act(async () => {});
}

/** Advance one (or more) 3-second poll cycles and flush the outcome. */
async function advancePoll(cycles = 1) {
  for (let i = 0; i < cycles; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
  }
  await act(async () => {});
}

beforeEach(() => {
  vi.clearAllMocks();
  // Fake timers drive the 3-second poll interval deterministically;
  // Date.now() is faked consistently with the timer clock.
  vi.useFakeTimers();
  // Default happy path: one submission whose first poll succeeds.
  generateWorkflow.mockResolvedValue(submission());
  getWorkflowGenerationJob.mockResolvedValue(succeededJob());
});

afterEach(() => {
  vi.useRealTimers();
});

// --------------------------------------------------------------------------
// Panel chrome (Requirement 10.1): the panel lives in the builder's side
// drawer, so it renders a compact title with the prompt input immediately
// available (the drawer, tested with WorkflowBuilder, handles collapsing).
// --------------------------------------------------------------------------

describe('GenerateChatPanel chrome', () => {
  it('renders the compact title, description, and prompt input (10.1)', () => {
    renderPanel();
    expect(screen.getByRole('region', { name: 'Generate workflow panel' })).toBeInTheDocument();
    expect(screen.getByText('Generate workflow')).toBeInTheDocument();
    expect(
      screen.getByText(/Describe the pipeline in natural language/)
    ).toBeInTheDocument();
    // No large heading is used for the panel title.
    expect(screen.queryByRole('heading', { name: 'Generate workflow' })).toBeNull();
    expect(promptInput()).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Successful generation (Requirement 10.3)
// --------------------------------------------------------------------------

describe('GenerateChatPanel successful generation', () => {
  it('renders the generated workflow onto the canvas after parse and validation (10.3)', async () => {
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();

    expect(generateWorkflow).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
    });

    // The job is polled to its terminal success (workflow-manager-gaps 4.1, 4.2).
    await advancePoll();
    expect(getWorkflowGenerationJob).toHaveBeenCalledWith('job-1');
    expect(onApplyGenerated).toHaveBeenCalledTimes(1);

    // The parsed canvas state matches the generated definition.
    const generated = onApplyGenerated.mock.calls[0][0];
    expect(generated.nodes.map((n: { id: string }) => n.id)).toEqual([
      'camera_source_1',
      'capture_1',
    ]);
    expect(generated.edges).toHaveLength(1);

    // Chat history shows both turns and the validation outcome is shown.
    expect(screen.getByText('Camera to capture pipeline')).toBeInTheDocument();
    expect(screen.getByText('Here is your pipeline.')).toBeInTheDocument();
    expect(screen.getByText('Validation passed (0 warnings)')).toBeInTheDocument();

    // The prompt input is cleared on success.
    expect(promptInput().value).toBe('');
  });

  it('keeps polling while the job is pending or running, then renders (4.1)', async () => {
    getWorkflowGenerationJob
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'pending' })
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'running' })
      .mockResolvedValueOnce(succeededJob());
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();

    // Two non-terminal polls: still in progress, canvas untouched.
    await advancePoll(2);
    expect(getWorkflowGenerationJob).toHaveBeenCalledTimes(2);
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(screen.getByText('Generating...')).toBeInTheDocument();

    // Third poll reaches the terminal success.
    await advancePoll();
    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Generating...')).not.toBeInTheDocument();
  });

  it('displays the backend validation findings with the rendered result (10.3)', async () => {
    getWorkflowGenerationJob.mockResolvedValue(
      succeededJob({
        findings: [
          {
            severity: 'error',
            code: 'V4_MISSING_REQUIRED_PARAMETER',
            message: "Required parameter 'modelName' has no value",
            nodeId: 'model_inference_1',
            connectionId: null,
          },
        ],
        error_count: 1,
        warning_count: 0,
        validation_passed: false,
      })
    );
    const { onApplyGenerated } = renderPanel();

    send('Add a model');
    await settle();
    await advancePoll();

    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText('Validation found 1 error and 0 warnings')
    ).toBeInTheDocument();
    const list = screen.getByRole('list', { name: 'Generation validation findings' });
    expect(list.textContent).toContain(
      "[error] V4_MISSING_REQUIRED_PARAMETER: Required parameter 'modelName' has no value (node: model_inference_1)"
    );
  });

  it('maintains the session id across turns and sends the canvas snapshot (10.5 wiring)', async () => {
    generateWorkflow
      .mockResolvedValueOnce(submission({ session_id: 'session-1' }))
      .mockResolvedValueOnce(submission({ session_id: 'session-1' }));
    // After the first turn the canvas carries the generated definition.
    let currentDefinition: WorkflowDefinition = EMPTY_DEFINITION;
    const onApplyGenerated = vi.fn<GenerateChatPanelProps['onApplyGenerated']>(() => {
      currentDefinition = GENERATED_DEFINITION;
    });
    renderPanel({ getDefinition: () => currentDefinition, onApplyGenerated });

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();
    expect(generateWorkflow).toHaveBeenCalledTimes(1);
    // First turn: no session yet and an empty canvas sends no snapshot.
    expect(generateWorkflow.mock.calls[0][0]).toEqual({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
    });

    send('Now add a crop before the capture');
    await settle();
    await advancePoll();
    expect(generateWorkflow).toHaveBeenCalledTimes(2);
    // Follow-up: continues the session (the id adopted from the 202
    // submission, workflow-manager-gaps 4.5) and sends the current canvas.
    expect(generateWorkflow.mock.calls[1][0]).toEqual({
      usecase_id: 'uc-1',
      prompt: 'Now add a crop before the capture',
      session_id: 'session-1',
      current_definition: GENERATED_DEFINITION,
    });
  });
});

// --------------------------------------------------------------------------
// Failures leave the canvas unchanged and preserve the prompt (10.4, 10.7)
// --------------------------------------------------------------------------

describe('GenerateChatPanel failures', () => {
  it('shows a failed job Error_Envelope, keeps the canvas untouched, and preserves the prompt (10.4, 10.7)', async () => {
    const { ApiError } = await import('../../services/api');
    // A failed Generation_Job replays its envelope from the status
    // endpoint (workflow-manager-gaps Requirements 2.3, 4.3).
    getWorkflowGenerationJob.mockRejectedValue(
      new ApiError(
        'Workflow generation timed out after 45 seconds. Your prompt was not lost - please retry.',
        504,
        'GENERATION_TIMEOUT'
      )
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();

    expect(screen.getByText('Generation failed')).toBeInTheDocument();
    expect(
      screen.getByText(/Workflow generation timed out after 45 seconds/)
    ).toBeInTheDocument();
    expect(onApplyGenerated).not.toHaveBeenCalled();
    // The prompt stays in the input for retry.
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('shows a synchronous submit rejection without creating a poll loop (1.3 wiring)', async () => {
    const { ApiError } = await import('../../services/api');
    generateWorkflow.mockRejectedValue(
      new ApiError('Temperature must be a number between 0 and 1', 400, 'INVALID_TEMPERATURE')
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();

    expect(screen.getByText('Generation failed')).toBeInTheDocument();
    expect(screen.getByText(/Temperature must be a number/)).toBeInTheDocument();
    expect(getWorkflowGenerationJob).not.toHaveBeenCalled();
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('shows a client-side parse failure without rendering, preserving the prompt (10.4)', async () => {
    getWorkflowGenerationJob.mockResolvedValue(
      succeededJob({
        definition: {
          schemaVersion: 1,
          nodes: [
            { id: 'mystery_1', type: 'mystery_node', position: { x: 0, y: 0 }, parameters: {} },
          ],
          connections: [],
        },
      })
    );
    const { onApplyGenerated } = renderPanel();

    send('Build something exotic');
    await settle();
    await advancePoll();

    expect(screen.getByText('Generation failed')).toBeInTheDocument();
    expect(
      screen.getByText(/unknown node type 'mystery_node'/)
    ).toBeInTheDocument();
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Build something exotic');
  });

  it('disables Send for roles without workflow edit permission (11.1)', () => {
    renderPanel({ role: 'Viewer' });
    fireEvent.change(promptInput(), { target: { value: 'A pipeline' } });
    const sendButton = screen.getByRole('button', { name: 'Send' });
    expect(
      (sendButton as HTMLButtonElement).disabled === true ||
        sendButton.getAttribute('aria-disabled') === 'true'
    ).toBe(true);
  });
});

// --------------------------------------------------------------------------
// Poll-loop stops (workflow-manager-gaps Requirements 4.6, 4.7)
// --------------------------------------------------------------------------

describe('GenerateChatPanel poll-loop stops (4.6, 4.7)', () => {
  it('stops after 3 consecutive transport failures with the retrieval error, preserving the prompt (4.6)', async () => {
    // Network errors are not Generation_Job failure envelopes.
    getWorkflowGenerationJob.mockRejectedValue(new TypeError('Failed to fetch'));
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();

    // Two failures: still polling.
    await advancePoll(2);
    expect(screen.getByText('Generating...')).toBeInTheDocument();

    // Third consecutive failure: stop with the retrieval message.
    await advancePoll();
    expect(getWorkflowGenerationJob).toHaveBeenCalledTimes(3);
    expect(screen.queryByText('Generating...')).not.toBeInTheDocument();
    expect(screen.getByText('Generation failed')).toBeInTheDocument();
    expect(
      screen.getByText(/The generation status could not be retrieved\./)
    ).toBeInTheDocument();
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('resets the transport-failure counter on a successful poll (4.6)', async () => {
    const { ApiError } = await import('../../services/api');
    getWorkflowGenerationJob
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      // The uniform 404 is a non-failure envelope: transport failure too,
      // but the successful poll before it reset the counter.
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'running' })
      .mockRejectedValueOnce(new ApiError('Job not found', 404, 'JOB_NOT_FOUND'))
      .mockResolvedValueOnce(succeededJob());
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll(5);

    // Never 3 consecutive failures: the loop survived to the success.
    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(promptInput().value).toBe('');
  });

  it('stops at the 300-second deadline with the did-not-complete error, preserving the prompt (4.7)', async () => {
    getWorkflowGenerationJob.mockResolvedValue({ job_id: 'job-1', status: 'running' });
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();

    // 100 polls x 3 s = 300 s: the deadline fires (Req 4.7).
    await advancePoll(100);
    expect(screen.queryByText('Generating...')).not.toBeInTheDocument();
    expect(screen.getByText('Generation failed')).toBeInTheDocument();
    expect(
      screen.getByText(/Generation did not complete in time\./)
    ).toBeInTheDocument();
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });
});

// --------------------------------------------------------------------------
// Task 10.5 additions: render-after-validation evidence, repeated-failure
// canvas safety, retry flow, busy-state gating, and snapshot rules
// (Requirements 10.3, 10.4, 10.5, 10.7)
// --------------------------------------------------------------------------

describe('GenerateChatPanel render-after-validation (10.3)', () => {
  it('renders a validated result with warnings and displays those findings (10.3)', async () => {
    getWorkflowGenerationJob.mockResolvedValue(
      succeededJob({
        findings: [
          {
            severity: 'warning',
            code: 'W1_UNCONNECTED_OPTIONAL_PORT',
            message: "Optional input port 'meta' is not connected",
            nodeId: 'capture_1',
            connectionId: null,
          },
        ],
        error_count: 0,
        warning_count: 1,
        validation_passed: true,
      })
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();

    // The applied result is always accompanied by the backend validation
    // outcome: findings are displayed alongside the render (10.3).
    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Validation passed (1 warning)')).toBeInTheDocument();
    const list = screen.getByRole('list', { name: 'Generation validation findings' });
    expect(list.textContent).toContain(
      "[warning] W1_UNCONNECTED_OPTIONAL_PORT: Optional input port 'meta' is not connected (node: capture_1)"
    );
  });
});

describe('GenerateChatPanel repeated failures and retry (10.4, 10.7)', () => {
  it('leaves the canvas untouched across multiple consecutive failures (10.4, 10.7)', async () => {
    const { ApiError } = await import('../../services/api');
    getWorkflowGenerationJob
      .mockRejectedValueOnce(
        new ApiError('Bedrock invocation failed: throttled', 502, 'GENERATION_FAILED')
      )
      .mockRejectedValueOnce(
        new ApiError(
          'Workflow generation timed out after 45 seconds. Your prompt was not lost - please retry.',
          504,
          'GENERATION_TIMEOUT'
        )
      );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();
    expect(
      screen.getByText(/Bedrock invocation failed: throttled/)
    ).toBeInTheDocument();
    expect(promptInput().value).toBe('Camera to capture pipeline');

    // Retry hits a timeout: the timeout-specific error replaces the
    // previous one, and the canvas has still never been touched.
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await settle();
    await advancePoll();
    expect(
      screen.getByText(/timed out after 45 seconds/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/throttled/)).not.toBeInTheDocument();

    expect(generateWorkflow).toHaveBeenCalledTimes(2);
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('renders on a successful retry after a failure, clearing the error and prompt (10.4, 10.7)', async () => {
    const { ApiError } = await import('../../services/api');
    getWorkflowGenerationJob
      .mockRejectedValueOnce(new ApiError('Bedrock invocation failed', 502, 'GENERATION_FAILED'))
      .mockResolvedValueOnce(succeededJob());
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();
    expect(screen.getByText('Generation failed')).toBeInTheDocument();
    expect(onApplyGenerated).not.toHaveBeenCalled();

    // The preserved prompt is resent unchanged; the retry succeeds.
    // The session id from the first 202 was adopted at submit time
    // (workflow-manager-gaps Req 4.5), so the retry continues it.
    expect(promptInput().value).toBe('Camera to capture pipeline');
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await settle();
    await advancePoll();

    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(generateWorkflow).toHaveBeenLastCalledWith({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
      session_id: 'session-1',
    });
    // The failure alert is cleared and the prompt input is emptied.
    expect(screen.queryByText('Generation failed')).not.toBeInTheDocument();
    expect(promptInput().value).toBe('');
  });
});

describe('GenerateChatPanel busy state (10.7, workflow-manager-gaps 4.1, 4.4)', () => {
  it('disables input and prevents a second send while a request is in flight', async () => {
    let resolveGenerate!: (value: ReturnType<typeof submission>) => void;
    generateWorkflow.mockImplementation(
      () =>
        new Promise<ReturnType<typeof submission>>((resolve) => {
          resolveGenerate = resolve;
        })
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();

    // While generating: spinner shown, prompt input and Send disabled
    // (the indicator shows immediately on submit, Req 4.1).
    expect(screen.getByText('Generating...')).toBeInTheDocument();
    expect(promptInput().disabled).toBe(true);
    const sendButton = screen.getByRole('button', { name: 'Send' });
    expect(
      (sendButton as HTMLButtonElement).disabled === true ||
        sendButton.getAttribute('aria-disabled') === 'true'
    ).toBe(true);

    // A second click issues no second request.
    fireEvent.click(sendButton);
    expect(generateWorkflow).toHaveBeenCalledTimes(1);

    resolveGenerate(submission());
    await settle();

    // Still polling after the 202: submission stays disabled while the
    // job is non-terminal (Req 4.4).
    expect(screen.getByText('Generating...')).toBeInTheDocument();
    expect(promptInput().disabled).toBe(true);
    fireEvent.click(sendButton);
    expect(generateWorkflow).toHaveBeenCalledTimes(1);

    await advancePoll();
    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Generating...')).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Temperature control: optional per-request override of the configured
// model temperature (blank = use the configured value)
// --------------------------------------------------------------------------

function temperatureInput(): HTMLInputElement {
  const input = document.querySelector('input[aria-label="Generation temperature"]');
  expect(input).not.toBeNull();
  return input as HTMLInputElement;
}

describe('GenerateChatPanel temperature control', () => {
  it('renders the control with its help text, defaulting to blank', () => {
    renderPanel();
    expect(temperatureInput().value).toBe('');
    expect(
      screen.getByText(
        /Lower values \(e\.g\. 0-0\.2\) make generation more precise and reduce invented nodes; higher values are more creative\./
      )
    ).toBeInTheDocument();
  });

  it('sends the temperature with the request when set', async () => {
    renderPanel();

    fireEvent.change(temperatureInput(), { target: { value: '0.1' } });
    send('Camera to capture pipeline');
    await settle();

    expect(generateWorkflow).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
      temperature: 0.1,
    });
    await advancePoll();
  });

  it('omits temperature from the payload when left blank', async () => {
    renderPanel();

    send('Camera to capture pipeline');
    await settle();

    expect(generateWorkflow).toHaveBeenCalledTimes(1);
    expect(generateWorkflow.mock.calls[0][0]).toEqual({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
    });
    await advancePoll();
  });

  it('shows an error and blocks Send for out-of-range values', async () => {
    renderPanel();

    fireEvent.change(temperatureInput(), { target: { value: '1.5' } });
    expect(screen.getByText('Enter a number between 0 and 1.')).toBeInTheDocument();

    fireEvent.change(promptInput(), { target: { value: 'A pipeline' } });
    const sendButton = screen.getByRole('button', { name: 'Send' });
    expect(
      (sendButton as HTMLButtonElement).disabled === true ||
        sendButton.getAttribute('aria-disabled') === 'true'
    ).toBe(true);
    fireEvent.click(sendButton);
    expect(generateWorkflow).not.toHaveBeenCalled();
  });

  it('accepts the boundary values 0 and 1', async () => {
    renderPanel();

    fireEvent.change(temperatureInput(), { target: { value: '0' } });
    send('Camera to capture pipeline');
    await settle();

    expect(generateWorkflow).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
      temperature: 0,
    });
    await advancePoll();
  });
});

describe('GenerateChatPanel canvas snapshot rules (10.5)', () => {
  it('sends the snapshot on a first prompt over a non-empty canvas, without a session id (10.5)', async () => {
    renderPanel({ getDefinition: () => GENERATED_DEFINITION });

    send('Add a crop before the capture');
    await settle();

    // A non-empty canvas is sent for modification even on the first
    // turn of a session; no session id exists yet.
    expect(generateWorkflow).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      prompt: 'Add a crop before the capture',
      current_definition: GENERATED_DEFINITION,
    });
    await advancePoll();
  });
});

// --------------------------------------------------------------------------
// Generation_Gate rejection and repaired-notice rendering (task 12.7,
// portal-build-fleet-and-workflow-gates Requirements 8.6, 8.8)
// --------------------------------------------------------------------------

/** Structural errors of a gate rejection envelope (Req 8.8 fixtures). */
const STRUCTURAL_ERRORS: GenerationStructuralError[] = [
  {
    code: 'V2_PORT_TYPE_MISMATCH',
    message: "Port type mismatch: 'VideoFrames' cannot feed 'InferenceResults'",
    affected: [
      // Carries a display name: labeled by it (8.8).
      { id: 'camera_source_1', kind: 'node', displayName: 'Line 3 camera' },
      // No display name: falls back to the id (8.8).
      { id: 'camera_source_1.out->capture_1.in', kind: 'connection' },
    ],
    explanation:
      'The camera output produces video frames but the connection expects inference results, so data cannot flow.',
  },
  {
    code: 'V5_MISSING_OUTPUT_NODE',
    message: 'Workflow has no output node',
    // Graph-level error: no affected elements, explanation only (8.8).
    affected: [],
    explanation: 'The workflow produces no output, so running it would have no effect.',
  },
];

describe('GenerateChatPanel gate rejection (8.8)', () => {
  it('renders the rejection alert with display-name fallback labels and explanations, preserving the prompt (8.8)', async () => {
    const { ApiError } = await import('../../services/api');
    // The gate rejection is recorded on the failed job and replayed by
    // the status endpoint (workflow-manager-gaps Requirements 2.3, 4.3).
    getWorkflowGenerationJob.mockRejectedValue(
      new ApiError(
        'The generated workflow has structural errors and was rejected.',
        422,
        'GENERATION_REJECTED',
        { structural_errors: STRUCTURAL_ERRORS }
      )
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();

    // The rejection is rendered as an error Alert with its own header,
    // distinct from the generic failure alert.
    expect(screen.getByText('Generation rejected')).toBeInTheDocument();
    expect(screen.queryByText('Generation failed')).not.toBeInTheDocument();
    expect(
      screen.getByText(/The generated workflow has structural errors and was rejected\./)
    ).toBeInTheDocument();

    const list = screen.getByRole('list', { name: 'Structural errors' });
    const items = Array.from(list.querySelectorAll('li')).map((li) => li.textContent);
    expect(items).toHaveLength(2);
    // Affected elements labeled by display name, falling back to the id,
    // followed by the plain-language explanation (8.8).
    expect(items[0]).toContain('Line 3 camera, camera_source_1.out->capture_1.in');
    expect(items[0]).toContain(
      'The camera output produces video frames but the connection expects inference results, so data cannot flow.'
    );
    // Graph-level error: explanation alone, no affected-element label.
    expect(items[1]).toBe(
      'The workflow produces no output, so running it would have no effect.'
    );

    // The canvas is untouched and the prompt stays for retry (8.8).
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('renders the rejection alert for the fail-closed GENERATION_VALIDATION_INCOMPLETE code (8.8)', async () => {
    const { ApiError } = await import('../../services/api');
    getWorkflowGenerationJob.mockRejectedValue(
      new ApiError(
        'Validation could not be completed for the generated workflow.',
        422,
        'GENERATION_VALIDATION_INCOMPLETE',
        {}
      )
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();

    expect(screen.getByText('Generation rejected')).toBeInTheDocument();
    expect(
      screen.getByText(/Validation could not be completed for the generated workflow\./)
    ).toBeInTheDocument();
    // No structural errors in the envelope: no error list is rendered.
    expect(screen.queryByRole('list', { name: 'Structural errors' })).toBeNull();
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('clears the rejection and the prompt only on a subsequent 200 (8.8)', async () => {
    const { ApiError } = await import('../../services/api');
    getWorkflowGenerationJob
      .mockRejectedValueOnce(
        new ApiError('Rejected.', 422, 'GENERATION_REJECTED', {
          structural_errors: STRUCTURAL_ERRORS,
        })
      )
      .mockResolvedValueOnce(succeededJob());
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();
    expect(screen.getByText('Generation rejected')).toBeInTheDocument();
    expect(promptInput().value).toBe('Camera to capture pipeline');

    // Retry with the preserved prompt succeeds: the rejection alert is
    // cleared and the prompt input empties (cleared only on 200).
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await settle();
    await advancePoll();
    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Generation rejected')).not.toBeInTheDocument();
    expect(promptInput().value).toBe('');
  });
});

describe('GenerateChatPanel repaired notice (8.6)', () => {
  it('renders the automatic-correction info alert listing the corrected errors on a repaired acceptance (8.6)', async () => {
    getWorkflowGenerationJob.mockResolvedValue(
      succeededJob({
        gate: {
          passed: true,
          repaired: true,
          corrected_errors: [
            {
              severity: 'error',
              code: 'V3_BACKWARDS_EDGE',
              message: 'Connection goes backwards against the pipeline flow',
              nodeId: null,
              connectionId: 'capture_1.out->camera_source_1.in',
            },
            {
              severity: 'error',
              code: 'V6_UNREACHABLE_NODE',
              message: 'Node is not reachable from any input node',
              nodeId: 'capture_1',
              connectionId: null,
            },
          ],
          structural_error_codes: ['V3_BACKWARDS_EDGE', 'V6_UNREACHABLE_NODE'],
        },
      })
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();

    // The repaired result is still applied to the canvas (accept path).
    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Automatic correction applied')).toBeInTheDocument();

    // The corrected original Structural_Errors are listed (8.6).
    const list = screen.getByRole('list', { name: 'Corrected structural errors' });
    const items = Array.from(list.querySelectorAll('li')).map((li) => li.textContent);
    expect(items).toHaveLength(2);
    expect(items[0]).toContain(
      'V3_BACKWARDS_EDGE: Connection goes backwards against the pipeline flow'
    );
    expect(items[0]).toContain('(connection: capture_1.out->camera_source_1.in)');
    expect(items[1]).toContain('V6_UNREACHABLE_NODE: Node is not reachable from any input node');
    expect(items[1]).toContain('(node: capture_1)');

    // The prompt clears on the 200 as usual.
    expect(promptInput().value).toBe('');
  });

  it('shows no correction notice when the gate accepted without repair (8.6)', async () => {
    getWorkflowGenerationJob.mockResolvedValue(
      succeededJob({
        gate: { passed: true, repaired: false, corrected_errors: [], structural_error_codes: [] },
      })
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    await settle();
    await advancePoll();

    expect(onApplyGenerated).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Automatic correction applied')).not.toBeInTheDocument();
  });
});
