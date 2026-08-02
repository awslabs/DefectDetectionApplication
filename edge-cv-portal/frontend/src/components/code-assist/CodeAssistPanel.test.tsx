/**
 * Component tests for the CodeAssistPanel flows (custom-node-code-assist
 * task 6.6, Requirements 1.5, 1.6, 2.4, 2.5, 2.7, 2.9, 5.4, 5.5).
 *
 * Rendered against a harness holding a sibling editor textarea, the way
 * the real Code_Editing_Surfaces own the editor: review-before-apply
 * (returned code shown, editor untouched until Accept, 2.4), Accept
 * firing `onAccept` with the code and clearing the prompt (2.5), Reject
 * preserving editor and prompt (2.9), the in-progress spinner with
 * resubmission blocked while the editor sibling stays editable (1.5,
 * 1.6), the per-category error alerts with the prompt retained and
 * resubmission accepted afterwards (5.1-5.3, 5.5), and — in every flow —
 * no persist/save API call: only `codeAssist`, once per submission
 * (2.7, 5.4).
 */
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CodeAssistPanel from './CodeAssistPanel';
import { ApiError } from '../../services/api';

const { codeAssist, otherApiCalls } = vi.hoisted(() => ({
  codeAssist: vi.fn(),
  otherApiCalls: [] as string[],
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
  // Any apiService method other than codeAssist (a persist/save call,
  // for instance) is recorded so every test can assert none happened
  // (Requirements 2.7, 5.4).
  const apiService = new Proxy(
    { codeAssist },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => {
          otherApiCalls.push(String(prop));
          return Promise.resolve({});
        };
      },
    }
  );
  return { ApiError, apiService };
});

// -------------------------------------------------------------- fixtures

const GENERATED_CODE = 'def process_frame(frame, metadata):\n    return None\n';
const NOTES = 'Blurs every face with a Gaussian kernel.';

const SUCCESS = {
  code: GENERATED_CODE,
  notes: NOTES,
  model_id: 'us.anthropic.test-model',
  contract: 'process_frame_or_handle',
};

/** onAccept spy shared with the harness; reset per test. */
const acceptSpy = vi.fn();

/** Minimal stand-in for a Code_Editing_Surface: a sibling editor
 *  textarea whose value only `onAccept` (or the user) may change. */
function Harness({ initialEditorCode = '' }: { initialEditorCode?: string }) {
  const [editorCode, setEditorCode] = useState(initialEditorCode);
  return (
    <>
      <textarea
        data-testid="editor"
        value={editorCode}
        onChange={(event) => setEditorCode(event.target.value)}
      />
      <CodeAssistPanel
        usecaseId="uc-1"
        surface="workflow-builder"
        contract="process_frame_or_handle"
        editorCode={editorCode}
        onAccept={(code) => {
          acceptSpy(code);
          setEditorCode(code);
        }}
      />
    </>
  );
}

// --------------------------------------------------------------- helpers

const editor = () => screen.getByTestId('editor') as HTMLTextAreaElement;

// The FormField label wins the accessible-name computation via
// aria-labelledby, so the prompt textarea's name is "Code assistant".
const promptInput = () =>
  screen.getByRole('textbox', { name: 'Code assistant' }) as HTMLTextAreaElement;

const generateButton = () => screen.getByRole('button', { name: 'Generate' });

const isDisabled = (button: HTMLElement) =>
  button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';

function submitPrompt(prompt: string) {
  fireEvent.change(promptInput(), { target: { value: prompt } });
  fireEvent.click(generateButton());
}

/** Submit and wait for the review block to appear. */
async function reachReview(prompt: string) {
  codeAssist.mockResolvedValue(SUCCESS);
  submitPrompt(prompt);
  return await screen.findByLabelText('Generated code for review');
}

beforeEach(() => {
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

afterEach(() => {
  // No flow ever issues a persist/save API call (Requirements 2.7, 5.4).
  expect(otherApiCalls).toEqual([]);
});

// ----------------------------------------------------------------- tests

describe('CodeAssistPanel', () => {
  it('shows returned code for review without touching the editor until Accept (2.4)', async () => {
    render(<Harness initialEditorCode="# original code" />);

    const review = await reachReview('Blur all faces');

    // The returned code and notes are shown for review…
    expect(review.textContent).toBe(GENERATED_CODE);
    expect(screen.getByText(NOTES)).toBeInTheDocument();
    // …while the editor is untouched and the prompt still visible.
    expect(editor().value).toBe('# original code');
    expect(acceptSpy).not.toHaveBeenCalled();
    expect(promptInput().value).toBe('Blur all faces');

    // Exactly one codeAssist request, carrying the editor's code.
    expect(codeAssist).toHaveBeenCalledTimes(1);
    expect(codeAssist).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      surface: 'workflow-builder',
      contract: 'process_frame_or_handle',
      prompt: 'Blur all faces',
      current_code: '# original code',
    });
  });

  it('Accept fires onAccept with the code and clears the prompt (2.5)', async () => {
    render(<Harness initialEditorCode="# original code" />);
    await reachReview('Blur all faces');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));

    expect(acceptSpy).toHaveBeenCalledTimes(1);
    expect(acceptSpy).toHaveBeenCalledWith(GENERATED_CODE);
    expect(editor().value).toBe(GENERATED_CODE);
    expect(promptInput().value).toBe('');
    expect(screen.queryByLabelText('Generated code for review')).toBeNull();
  });

  it('Reject preserves the editor and the prompt (2.9)', async () => {
    render(<Harness initialEditorCode="# original code" />);
    await reachReview('Blur all faces');

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));

    expect(acceptSpy).not.toHaveBeenCalled();
    expect(editor().value).toBe('# original code');
    expect(promptInput().value).toBe('Blur all faces');
    expect(screen.queryByLabelText('Generated code for review')).toBeNull();
  });

  it('shows the spinner, blocks resubmission, and keeps the editor sibling editable while pending (1.5, 1.6)', async () => {
    let resolveRequest: (value: typeof SUCCESS) => void = () => {};
    codeAssist.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRequest = resolve;
        })
    );
    render(<Harness />);

    submitPrompt('Blur all faces');

    // In-progress indication shown, submission control disabled (1.6).
    expect(screen.getByText(/Generating code/)).toBeInTheDocument();
    expect(isDisabled(generateButton())).toBe(true);

    // A second click issues no concurrent request (1.6).
    fireEvent.click(generateButton());
    expect(codeAssist).toHaveBeenCalledTimes(1);
    // The blank editor was sent as a fresh generation: no current_code.
    expect(codeAssist.mock.calls[0][0]).not.toHaveProperty('current_code');

    // The sibling editor still accepts manual edits mid-flight (1.5).
    fireEvent.change(editor(), { target: { value: '# my manual edit' } });
    expect(editor().value).toBe('# my manual edit');

    resolveRequest(SUCCESS);
    await screen.findByLabelText('Generated code for review');
    expect(screen.queryByText(/Generating code/)).toBeNull();
    // The mid-flight manual edit survived (1.5).
    expect(editor().value).toBe('# my manual edit');
  });

  it.each([
    [
      'throttling',
      new ApiError('Too many requests', 502, 'BEDROCK_INVOCATION_FAILED', {
        category: 'throttling',
      }),
      'Throttled',
    ],
    [
      'authorization',
      new ApiError('Access denied to the model', 502, 'BEDROCK_INVOCATION_FAILED', {
        category: 'authorization',
      }),
      'Not authorized to invoke the model',
    ],
    [
      'model-access',
      new ApiError('Model not enabled in region', 502, 'BEDROCK_UNREACHABLE', {
        category: 'model-access',
      }),
      'Model not available',
    ],
    [
      'model-error',
      new ApiError('The model returned an error', 502, 'BEDROCK_INVOCATION_FAILED', {
        category: 'model-error',
      }),
      'Model error',
    ],
    [
      'timeout',
      new ApiError('Generation did not complete', 504, 'GENERATION_TIMEOUT', {
        timeout_seconds: 45,
      }),
      'Timed out after 45 seconds',
    ],
    [
      'no code produced',
      new ApiError('The model returned no code', 422, 'NO_CODE_RETURNED'),
      'No code produced',
    ],
  ])(
    'shows the %s error alert with the prompt retained and the editor untouched (5.4, 5.5)',
    async (_category, error, expectedHeader) => {
      codeAssist.mockRejectedValue(error);
      render(<Harness initialEditorCode="# original code" />);

      submitPrompt('Blur all faces');

      const alert = await screen.findByText(expectedHeader);
      expect(alert).toBeInTheDocument();
      expect(screen.getByText(error.message)).toBeInTheDocument();

      // Prompt retained unmodified and editable again; editor untouched;
      // no in-progress indication remains (5.4, 5.5).
      expect(promptInput().value).toBe('Blur all faces');
      expect(promptInput()).toBeEnabled();
      expect(editor().value).toBe('# original code');
      expect(acceptSpy).not.toHaveBeenCalled();
      expect(screen.queryByText(/Generating code/)).toBeNull();
      expect(codeAssist).toHaveBeenCalledTimes(1);
    }
  );

  it('accepts a resubmission of the preserved prompt after a failure (5.5)', async () => {
    codeAssist.mockRejectedValueOnce(
      new ApiError('Too many requests', 502, 'BEDROCK_INVOCATION_FAILED', {
        category: 'throttling',
      })
    );
    render(<Harness />);

    submitPrompt('Blur all faces');
    await screen.findByText('Throttled');

    // The preserved prompt is resent unchanged; the retry succeeds and
    // the error alert clears.
    codeAssist.mockResolvedValue(SUCCESS);
    fireEvent.click(generateButton());

    await screen.findByLabelText('Generated code for review');
    expect(codeAssist).toHaveBeenCalledTimes(2);
    expect(codeAssist.mock.calls[1][0].prompt).toBe('Blur all faces');
    expect(screen.queryByText('Throttled')).toBeNull();
  });

  it('never persists across a full submit-review-accept flow (2.7)', async () => {
    render(<Harness />);
    await reachReview('Blur all faces');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));

    await waitFor(() => expect(acceptSpy).toHaveBeenCalledTimes(1));
    // Only the one codeAssist call happened; the afterEach hook asserts
    // no other apiService method was ever invoked (2.7).
    expect(codeAssist).toHaveBeenCalledTimes(1);
  });
});
