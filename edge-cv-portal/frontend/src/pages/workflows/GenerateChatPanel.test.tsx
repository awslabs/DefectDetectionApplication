/**
 * Component tests for the prompt-based generation chat panel (task 10.3):
 * panel chrome inside the builder's side drawer (Requirement 10.1;
 * drawer collapse itself is covered by WorkflowBuilder tests), successful
 * generation rendering onto the canvas after client-side parse with the
 * backend validation findings displayed (10.3), session id maintained
 * across turns with the canvas snapshot sent for follow-ups, and failures
 * (API error or client-side parse failure) leaving the canvas untouched
 * with the error displayed and the prompt preserved for retry (10.4, 10.7).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import GenerateChatPanel, { type GenerateChatPanelProps } from './GenerateChatPanel';
import type { UserRole } from '../../types';
import type {
  NodeTypeDescriptor,
  WorkflowDefinition,
  WorkflowGenerationResult,
} from './types';

const { generateWorkflow } = vi.hoisted(() => ({
  generateWorkflow: vi.fn(),
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
    apiService: { generateWorkflow },
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

beforeEach(() => {
  vi.clearAllMocks();
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
    generateWorkflow.mockResolvedValue(generationResult());
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');

    await waitFor(() =>
      expect(generateWorkflow).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        prompt: 'Camera to capture pipeline',
      })
    );
    await waitFor(() => expect(onApplyGenerated).toHaveBeenCalledTimes(1));

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

  it('displays the backend validation findings with the rendered result (10.3)', async () => {
    generateWorkflow.mockResolvedValue(
      generationResult({
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

    await waitFor(() => expect(onApplyGenerated).toHaveBeenCalledTimes(1));
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
      .mockResolvedValueOnce(generationResult({ session_id: 'session-1' }))
      .mockResolvedValueOnce(generationResult({ session_id: 'session-1' }));
    // After the first turn the canvas carries the generated definition.
    let currentDefinition: WorkflowDefinition = EMPTY_DEFINITION;
    const onApplyGenerated = vi.fn<GenerateChatPanelProps['onApplyGenerated']>(() => {
      currentDefinition = GENERATED_DEFINITION;
    });
    renderPanel({ getDefinition: () => currentDefinition, onApplyGenerated });

    send('Camera to capture pipeline');
    await waitFor(() => expect(generateWorkflow).toHaveBeenCalledTimes(1));
    // First turn: no session yet and an empty canvas sends no snapshot.
    expect(generateWorkflow.mock.calls[0][0]).toEqual({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
    });

    send('Now add a crop before the capture');
    await waitFor(() => expect(generateWorkflow).toHaveBeenCalledTimes(2));
    // Follow-up: continues the session and sends the current canvas.
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
  it('shows the API error, keeps the canvas untouched, and preserves the prompt (10.4, 10.7)', async () => {
    const { ApiError } = await import('../../services/api');
    generateWorkflow.mockRejectedValue(
      new ApiError(
        'Workflow generation timed out after 45 seconds. Your prompt was not lost - please retry.',
        504,
        'GENERATION_TIMEOUT'
      )
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');

    expect(await screen.findByText('Generation failed')).toBeInTheDocument();
    expect(
      screen.getByText(/Workflow generation timed out after 45 seconds/)
    ).toBeInTheDocument();
    expect(onApplyGenerated).not.toHaveBeenCalled();
    // The prompt stays in the input for retry.
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('shows a client-side parse failure without rendering, preserving the prompt (10.4)', async () => {
    generateWorkflow.mockResolvedValue(
      generationResult({
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

    expect(await screen.findByText('Generation failed')).toBeInTheDocument();
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
// Task 10.5 additions: render-after-validation evidence, repeated-failure
// canvas safety, retry flow, busy-state gating, and snapshot rules
// (Requirements 10.3, 10.4, 10.5, 10.7)
// --------------------------------------------------------------------------

describe('GenerateChatPanel render-after-validation (10.3)', () => {
  it('renders a validated result with warnings and displays those findings (10.3)', async () => {
    generateWorkflow.mockResolvedValue(
      generationResult({
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

    // The applied result is always accompanied by the backend validation
    // outcome: findings are displayed alongside the render (10.3).
    await waitFor(() => expect(onApplyGenerated).toHaveBeenCalledTimes(1));
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
    generateWorkflow
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
    expect(
      await screen.findByText(/Bedrock invocation failed: throttled/)
    ).toBeInTheDocument();
    expect(promptInput().value).toBe('Camera to capture pipeline');

    // Retry hits a timeout: the timeout-specific error replaces the
    // previous one, and the canvas has still never been touched.
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(
      await screen.findByText(/timed out after 45 seconds/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/throttled/)).not.toBeInTheDocument();

    expect(generateWorkflow).toHaveBeenCalledTimes(2);
    expect(onApplyGenerated).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Camera to capture pipeline');
  });

  it('renders on a successful retry after a failure, clearing the error and prompt (10.4, 10.7)', async () => {
    const { ApiError } = await import('../../services/api');
    generateWorkflow
      .mockRejectedValueOnce(new ApiError('Bedrock invocation failed', 502, 'GENERATION_FAILED'))
      .mockResolvedValueOnce(generationResult());
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');
    expect(await screen.findByText('Generation failed')).toBeInTheDocument();
    expect(onApplyGenerated).not.toHaveBeenCalled();

    // The preserved prompt is resent unchanged; the retry succeeds.
    expect(promptInput().value).toBe('Camera to capture pipeline');
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => expect(onApplyGenerated).toHaveBeenCalledTimes(1));
    expect(generateWorkflow).toHaveBeenLastCalledWith({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
    });
    // The failure alert is cleared and the prompt input is emptied.
    expect(screen.queryByText('Generation failed')).not.toBeInTheDocument();
    expect(promptInput().value).toBe('');
  });
});

describe('GenerateChatPanel busy state (10.7)', () => {
  it('disables input and prevents a second send while a request is in flight', async () => {
    let resolveGenerate!: (result: WorkflowGenerationResult) => void;
    generateWorkflow.mockImplementation(
      () =>
        new Promise<WorkflowGenerationResult>((resolve) => {
          resolveGenerate = resolve;
        })
    );
    const { onApplyGenerated } = renderPanel();

    send('Camera to capture pipeline');

    // While generating: spinner shown, prompt input and Send disabled.
    expect(await screen.findByText('Generating...')).toBeInTheDocument();
    expect(promptInput().disabled).toBe(true);
    const sendButton = screen.getByRole('button', { name: 'Send' });
    expect(
      (sendButton as HTMLButtonElement).disabled === true ||
        sendButton.getAttribute('aria-disabled') === 'true'
    ).toBe(true);

    // A second click issues no second request.
    fireEvent.click(sendButton);
    expect(generateWorkflow).toHaveBeenCalledTimes(1);

    resolveGenerate(generationResult());
    await waitFor(() => expect(onApplyGenerated).toHaveBeenCalledTimes(1));
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
    generateWorkflow.mockResolvedValue(generationResult());
    renderPanel();

    fireEvent.change(temperatureInput(), { target: { value: '0.1' } });
    send('Camera to capture pipeline');

    await waitFor(() =>
      expect(generateWorkflow).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        prompt: 'Camera to capture pipeline',
        temperature: 0.1,
      })
    );
  });

  it('omits temperature from the payload when left blank', async () => {
    generateWorkflow.mockResolvedValue(generationResult());
    renderPanel();

    send('Camera to capture pipeline');

    await waitFor(() => expect(generateWorkflow).toHaveBeenCalledTimes(1));
    expect(generateWorkflow.mock.calls[0][0]).toEqual({
      usecase_id: 'uc-1',
      prompt: 'Camera to capture pipeline',
    });
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
    generateWorkflow.mockResolvedValue(generationResult());
    renderPanel();

    fireEvent.change(temperatureInput(), { target: { value: '0' } });
    send('Camera to capture pipeline');

    await waitFor(() =>
      expect(generateWorkflow).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        prompt: 'Camera to capture pipeline',
        temperature: 0,
      })
    );
  });
});

describe('GenerateChatPanel canvas snapshot rules (10.5)', () => {
  it('sends the snapshot on a first prompt over a non-empty canvas, without a session id (10.5)', async () => {
    generateWorkflow.mockResolvedValue(generationResult());
    renderPanel({ getDefinition: () => GENERATED_DEFINITION });

    send('Add a crop before the capture');

    // A non-empty canvas is sent for modification even on the first
    // turn of a session; no session id exists yet.
    await waitFor(() =>
      expect(generateWorkflow).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        prompt: 'Add a crop before the capture',
        current_definition: GENERATED_DEFINITION,
      })
    );
  });
});
