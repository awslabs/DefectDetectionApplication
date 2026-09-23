/**
 * Component tests for the CodeAssistPanel diagnostic mode
 * (custom-node-source-lifecycle task 13.3, Requirements 5.1-5.4, 5.7,
 * 5.8).
 *
 * Covers: pasted error output attached as `diagnostics` with the submit
 * label switching to "Diagnose and fix" (5.3); a seeded Diagnostic_Context
 * ("Fix with AI") pre-filling the attachment with its kind and
 * architecture (5.1, 5.2); truncation to the last 16 KiB with the flag set
 * (5.4); a response naming a different Target_File shown as "Applies to"
 * and handed to `onAccept(code, targetFile)` (5.7); a same-file target
 * treated as the active file; the INVALID_TARGET_FILE error alert with
 * prompt and attachment preserved (5.8); Clear detaching the diagnostics;
 * and a failed invocation keeping the attachment for the retry.
 */
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CodeAssistPanel from './CodeAssistPanel';
import { DIAGNOSTICS_MAX_LENGTH, type CodeAssistDiagnosticsState } from './codeAssistState';
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

const FIXED_CODE = '#include <gst/gst.h>\nstatic void fixed(void) {}\n';
const NOTES = 'Added the missing semicolon after the element registration.';

const SUCCESS = {
  code: FIXED_CODE,
  notes: NOTES,
  model_id: 'us.anthropic.test-model',
  contract: 'plugin_source',
};

const BUILD_LOG = 'src/plugin.c:42:3: error: expected ";" before "}" token';

const acceptSpy = vi.fn();

interface HarnessProps {
  seeded?: CodeAssistDiagnosticsState | null;
  activeFile?: string | null;
}

/** Source_Editor stand-in: a sibling editor plus the panel with context. */
function Harness({ seeded, activeFile = 'src/plugin.c' }: HarnessProps) {
  const [editorCode, setEditorCode] = useState('/* original */');
  return (
    <>
      <textarea
        data-testid="editor"
        value={editorCode}
        onChange={(event) => setEditorCode(event.target.value)}
      />
      <CodeAssistPanel
        usecaseId="uc-1"
        surface="node-designer"
        contract="plugin_source"
        context={{
          active_file: activeFile ?? undefined,
          files: { 'meson.build': "project('demo', 'c')" },
          file_paths: ['meson.build', 'src/plugin.c'],
          kind: 'scaffold',
        }}
        editorCode={editorCode}
        activeFile={activeFile}
        diagnostics={seeded}
        onAccept={(code, targetFile) => {
          acceptSpy(code, targetFile);
          if (!targetFile) setEditorCode(code);
        }}
      />
    </>
  );
}

// --------------------------------------------------------------- helpers

const editor = () => screen.getByTestId('editor') as HTMLTextAreaElement;
const promptInput = () =>
  screen.getByRole('textbox', { name: 'Code assistant' }) as HTMLTextAreaElement;
const errorOutput = () =>
  screen.getByRole('textbox', { name: 'Error output' }) as HTMLTextAreaElement;

function typePrompt(prompt: string) {
  fireEvent.change(promptInput(), { target: { value: prompt } });
}

function lastRequest() {
  return codeAssist.mock.calls[codeAssist.mock.calls.length - 1][0];
}

beforeEach(() => {
  vi.clearAllMocks();
  otherApiCalls.length = 0;
});

afterEach(() => {
  // Diagnostic mode persists nothing either (Requirement 2.7 carried over).
  expect(otherApiCalls).toEqual([]);
});

// ----------------------------------------------------------------- tests

describe('CodeAssistPanel diagnostic mode', () => {
  it('sends pasted error output as user diagnostics and relabels the submit (5.3)', async () => {
    codeAssist.mockResolvedValue(SUCCESS);
    render(<Harness />);

    // Without an attachment the plain "Generate" action is offered.
    expect(screen.getByRole('button', { name: 'Generate' })).toBeInTheDocument();

    fireEvent.change(errorOutput(), { target: { value: BUILD_LOG } });
    typePrompt('Fix the compile error');

    const submit = screen.getByRole('button', { name: 'Diagnose and fix' });
    expect(screen.queryByRole('button', { name: 'Generate' })).toBeNull();
    fireEvent.click(submit);

    await screen.findByLabelText('Generated code for review');
    expect(codeAssist).toHaveBeenCalledTimes(1);
    expect(lastRequest()).toMatchObject({
      usecase_id: 'uc-1',
      surface: 'node-designer',
      contract: 'plugin_source',
      prompt: 'Fix the compile error',
      current_code: '/* original */',
      diagnostics: { kind: 'user', text: BUILD_LOG },
      context: { active_file: 'src/plugin.c', file_paths: ['meson.build', 'src/plugin.c'] },
    });
    // No truncation flag when the text fits.
    expect(lastRequest().diagnostics).not.toHaveProperty('truncated');
    expect(lastRequest().diagnostics).not.toHaveProperty('architecture');
  });

  it('pre-fills a seeded Diagnostic_Context with its kind and architecture (5.1, 5.2)', async () => {
    codeAssist.mockResolvedValue(SUCCESS);
    render(
      <Harness seeded={{ kind: 'build', architecture: 'arm64_jp6', text: BUILD_LOG }} />
    );

    expect(screen.getByText('Attached error output (build, arm64_jp6)')).toBeInTheDocument();
    expect(errorOutput().value).toBe(BUILD_LOG);

    typePrompt('Fix the build');
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose and fix' }));

    await screen.findByLabelText('Generated code for review');
    expect(lastRequest().diagnostics).toEqual({
      kind: 'build',
      architecture: 'arm64_jp6',
      text: BUILD_LOG,
    });
  });

  it('sends only the last 16 KiB of a long log and flags the truncation (5.4)', async () => {
    codeAssist.mockResolvedValue(SUCCESS);
    const head = 'HEAD-'.repeat(200);
    const tail = 'x'.repeat(DIAGNOSTICS_MAX_LENGTH);
    render(<Harness seeded={{ kind: 'simulation', text: head + tail }} />);

    expect(
      screen.getByText(`Only the last ${DIAGNOSTICS_MAX_LENGTH.toLocaleString()} characters are sent.`)
    ).toBeInTheDocument();

    typePrompt('Why does the simulation crash?');
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose and fix' }));

    await screen.findByLabelText('Generated code for review');
    const sent = lastRequest().diagnostics;
    expect(sent.kind).toBe('simulation');
    expect(sent.truncated).toBe(true);
    expect(sent.text).toBe(tail);
    expect(sent.text.length).toBe(DIAGNOSTICS_MAX_LENGTH);
  });

  it('shows the Target_File and hands it to onAccept when the fix lands elsewhere (5.7)', async () => {
    codeAssist.mockResolvedValue({ ...SUCCESS, target_file: 'meson.build' });
    render(<Harness />);

    typePrompt('Link against gstvideo');
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));

    await screen.findByLabelText('Generated code for review');
    expect(screen.getByText('Applies to meson.build')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Apply to meson.build' }));

    expect(acceptSpy).toHaveBeenCalledTimes(1);
    expect(acceptSpy).toHaveBeenCalledWith(FIXED_CODE, 'meson.build');
    // The active editor is not the target, so the harness left it alone.
    expect(editor().value).toBe('/* original */');
    // Accept clears prompt and attachment alike.
    expect(promptInput().value).toBe('');
    expect(screen.getByText('Error output (optional)')).toBeInTheDocument();
  });

  it('treats a target equal to the active file as the active file', async () => {
    codeAssist.mockResolvedValue({ ...SUCCESS, target_file: 'src/plugin.c' });
    render(<Harness />);

    typePrompt('Fix the registration');
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));

    await screen.findByLabelText('Generated code for review');
    expect(screen.queryByText(/Applies to/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));

    expect(acceptSpy).toHaveBeenCalledWith(FIXED_CODE, undefined);
    expect(editor().value).toBe(FIXED_CODE);
  });

  it('reports a Target_File outside the Source_Tree and keeps prompt and attachment (5.8)', async () => {
    codeAssist.mockRejectedValue(
      new ApiError('Target file is not part of the plugin source', 422, 'INVALID_TARGET_FILE', {
        target_file: '../../etc/passwd',
      })
    );
    render(<Harness seeded={{ kind: 'build', architecture: 'x86_64', text: BUILD_LOG }} />);

    typePrompt('Fix it');
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose and fix' }));

    await screen.findByText('Proposed file is not part of this plugin');
    expect(
      screen.getByText(
        'The assistant named "../../etc/passwd", which is not a file of this plugin. ' +
          'Retry, or ask it to change the file you are editing.'
      )
    ).toBeInTheDocument();

    // Prompt, attachment, and editor untouched; retry still offered.
    expect(promptInput().value).toBe('Fix it');
    expect(errorOutput().value).toBe(BUILD_LOG);
    expect(editor().value).toBe('/* original */');
    expect(acceptSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Diagnose and fix' })).toBeEnabled();
  });

  it('Clear detaches the diagnostics so the next request carries none', async () => {
    codeAssist.mockResolvedValue(SUCCESS);
    render(<Harness seeded={{ kind: 'build', architecture: 'arm64_jp5', text: BUILD_LOG }} />);

    fireEvent.click(screen.getByRole('button', { name: 'Clear attached error output' }));

    expect(screen.getByText('Error output (optional)')).toBeInTheDocument();
    expect(errorOutput().value).toBe('');

    typePrompt('Add a property');
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));

    await screen.findByLabelText('Generated code for review');
    expect(lastRequest()).not.toHaveProperty('diagnostics');
  });

  it('a failed invocation keeps the attachment for the retry (5.5 carried over)', async () => {
    codeAssist.mockRejectedValueOnce(
      new ApiError('Too many requests', 502, 'BEDROCK_INVOCATION_FAILED', {
        category: 'throttling',
      })
    );
    render(<Harness />);

    fireEvent.change(errorOutput(), { target: { value: BUILD_LOG } });
    typePrompt('Fix the compile error');
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose and fix' }));

    await screen.findByText('Throttled');
    expect(errorOutput().value).toBe(BUILD_LOG);

    codeAssist.mockResolvedValue(SUCCESS);
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose and fix' }));

    await screen.findByLabelText('Generated code for review');
    expect(codeAssist).toHaveBeenCalledTimes(2);
    expect(lastRequest().diagnostics).toEqual({ kind: 'user', text: BUILD_LOG });
  });
});
