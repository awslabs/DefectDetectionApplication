/**
 * Component tests for the Source_Editor (custom-node-source-lifecycle
 * task 11.3, Requirements 1.1-1.3, 1.11, 1.13, 5.6, 5.11, 9.2).
 *
 * Rendered against a harness owning the `sourceEditorReducer` the way
 * PluginDetail does. Covers: one tab per text file plus read-only tabs for
 * binary files (1.1, 1.13); edits marking the tab and the unsaved counter
 * (1.2); Add file with path validation and duplicate rejection (1.3, 1.11);
 * Delete file behind a confirmation that only marks the deletion for the
 * next save (1.3); read-only mode hiding every mutating control and the
 * assistant (9.2); the Code_Assistant under each editable tab with the
 * `plugin_source` contract and the other text files as context, and the
 * `frame_hook` contract on the hook file (5.6); an accepted proposal for a
 * different Target_File landing in that file (5.11); and the partial-load
 * warning.
 */
import { useReducer, useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import SourceEditor from './SourceEditor';
import { HOOK_FILE } from './diagnosticsFile';
import {
  fromSourceTree,
  sourceEditorReducer,
  type SourceEditorState,
} from './sourceEditorState';
import type { SourceFileEntry } from './types';

const { codeAssist } = vi.hoisted(() => ({ codeAssist: vi.fn() }));

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
  return { ApiError, apiService: { codeAssist } };
});

// -------------------------------------------------------------- fixtures

const MESON = "project('demo', 'c')\n";
const PLUGIN_C = '#include <gst/gst.h>\n';
const HOOK_PY = 'def process_frame(frame, metadata):\n    return None\n';

function tree(): SourceFileEntry[] {
  return [
    { file: 'meson.build', size: MESON.length, content: MESON },
    { file: 'src/plugin.c', size: PLUGIN_C.length, content: PLUGIN_C },
    { file: HOOK_FILE, size: HOOK_PY.length, content: HOOK_PY },
    { file: 'assets/model.bin', size: 3 * 1024 * 1024, binary: true },
  ];
}

interface HarnessProps {
  initial?: SourceEditorState;
  readOnly?: boolean;
  assist?: boolean;
  truncated?: boolean;
}

const stateSpy = vi.fn<(state: SourceEditorState) => void>();
const activeSpy = vi.fn<(path: string) => void>();

/** PluginDetail stand-in: owns the reducer and the active tab. */
function Harness({
  initial = fromSourceTree(tree(), 3),
  readOnly = false,
  assist = true,
  truncated = false,
}: HarnessProps) {
  const [state, dispatch] = useReducer(sourceEditorReducer, initial);
  const [activeFile, setActiveFile] = useState<string | null>('src/plugin.c');
  stateSpy(state);
  return (
    <SourceEditor
      state={state}
      dispatch={dispatch}
      activeFile={activeFile}
      onActiveFileChange={(path) => {
        activeSpy(path);
        setActiveFile(path);
      }}
      readOnly={readOnly}
      truncated={truncated}
      assist={
        assist
          ? {
              usecaseId: 'uc-1',
              kind: 'scaffold',
              parameters: [{ name: 'threshold', param_type: 'float' }],
            }
          : null
      }
    />
  );
}

// --------------------------------------------------------------- helpers

const latestState = (): SourceEditorState =>
  stateSpy.mock.calls[stateSpy.mock.calls.length - 1][0];

const tab = (name: string | RegExp) => screen.getByRole('tab', { name });

const sourceOf = (path: string) =>
  screen.getByRole('textbox', { name: `Source of ${path}` }) as HTMLTextAreaElement;

const promptInput = () =>
  screen.getByRole('textbox', { name: 'Code assistant' }) as HTMLTextAreaElement;

function lastRequest() {
  return codeAssist.mock.calls[codeAssist.mock.calls.length - 1][0];
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ----------------------------------------------------------------- tests

describe('SourceEditor tabs', () => {
  it('renders one tab per text file plus read-only tabs for binary files (1.1, 1.13)', () => {
    render(<Harness />);

    // Sorted text files first, then the binary entry.
    const tabs = screen.getAllByRole('tab').map((t) => t.textContent);
    expect(tabs).toEqual(['meson.build', HOOK_FILE, 'src/plugin.c', 'assets/model.bin']);

    // The active file is the one the owner asked for.
    expect(tab('src/plugin.c')).toHaveAttribute('aria-selected', 'true');
    expect(sourceOf('src/plugin.c').value).toBe(PLUGIN_C);

    // Binary files show the read-only notice, no textarea.
    fireEvent.click(tab('assets/model.bin'));
    expect(activeSpy).toHaveBeenCalledWith('assets/model.bin');
    expect(screen.getByText('Read-only file')).toBeInTheDocument();
    expect(
      screen.getByText(
        'assets/model.bin (3.0 MiB) is a binary or oversized file and cannot be edited in the portal.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Source of assets/model.bin' })).toBeNull();
  });

  it('marks edited tabs and counts unsaved changes (1.2)', () => {
    render(<Harness />);

    expect(screen.queryByText(/unsaved change/)).toBeNull();

    fireEvent.change(sourceOf('src/plugin.c'), {
      target: { value: PLUGIN_C + 'static int x;\n' },
    });

    expect(tab('src/plugin.c •')).toBeInTheDocument();
    expect(screen.getByText('Modified')).toBeInTheDocument();
    expect(screen.getByText('1 unsaved change')).toBeInTheDocument();
    expect(latestState().files['src/plugin.c']).toBe(PLUGIN_C + 'static int x;\n');

    // Reverting the edit clears the marker again.
    fireEvent.change(sourceOf('src/plugin.c'), { target: { value: PLUGIN_C } });
    expect(screen.queryByText(/unsaved change/)).toBeNull();
    expect(screen.queryByText('Modified')).toBeNull();
  });

  it('shows the partial-load warning when the tree was truncated', () => {
    render(<Harness truncated />);
    expect(screen.getByText('Source partially loaded')).toBeInTheDocument();
  });
});

describe('SourceEditor add and delete', () => {
  async function openAddModal() {
    fireEvent.click(screen.getByRole('button', { name: 'Add file' }));
    const input = (await screen.findByRole('textbox', {
      name: 'New file path',
    })) as HTMLInputElement;
    const dialog = input.closest('[role="dialog"]') as HTMLElement;
    return { input, dialog };
  }

  it('rejects invalid and duplicate paths before adding (1.11)', async () => {
    render(<Harness />);
    const { input, dialog } = await openAddModal();
    const add = () => within(dialog).getByRole('button', { name: 'Add' });
    const isDisabled = (b: HTMLElement) =>
      b.hasAttribute('disabled') || b.getAttribute('aria-disabled') === 'true';

    expect(isDisabled(add())).toBe(true);

    fireEvent.change(input, { target: { value: '../escape.c' } });
    expect(
      screen.getByText('Use a relative path without ".." segments (for example docs/NOTES.md).')
    ).toBeInTheDocument();
    expect(isDisabled(add())).toBe(true);

    fireEvent.change(input, { target: { value: './src/plugin.c' } });
    expect(screen.getByText('A file with this path already exists.')).toBeInTheDocument();
    expect(isDisabled(add())).toBe(true);

    fireEvent.change(input, { target: { value: 'assets/model.bin' } });
    expect(screen.getByText('A file with this path already exists.')).toBeInTheDocument();

    expect(latestState().files).not.toHaveProperty('../escape.c');
  });

  it('adds a new empty file and makes it the active tab (1.3)', async () => {
    render(<Harness />);
    const { input, dialog } = await openAddModal();

    fireEvent.change(input, { target: { value: './docs/NOTES.md' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(tab('docs/NOTES.md •')).toBeInTheDocument());
    expect(activeSpy).toHaveBeenCalledWith('docs/NOTES.md');
    expect(tab('docs/NOTES.md •')).toHaveAttribute('aria-selected', 'true');
    expect(sourceOf('docs/NOTES.md').value).toBe('');
    expect(latestState().files['docs/NOTES.md']).toBe('');
    expect(screen.getByText('1 unsaved change')).toBeInTheDocument();
  });

  it('Enter in the path field submits the add', async () => {
    render(<Harness />);
    const { input } = await openAddModal();

    fireEvent.change(input, { target: { value: 'include/plugin.h' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => expect(latestState().files).toHaveProperty('include/plugin.h'));
  });

  it('deletes a file behind a confirmation and marks it for the next save (1.3)', async () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole('button', { name: 'Delete src/plugin.c' }));

    const message = await screen.findByText(
      'Remove src/plugin.c from the plugin source? The deletion is applied when you save.'
    );
    const dialog = message.closest('[role="dialog"]') as HTMLElement;

    // Cancel first: nothing changes.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(dialog.className).toContain('hidden'));
    expect(latestState().files).toHaveProperty('src/plugin.c');

    fireEvent.click(screen.getByRole('button', { name: 'Delete src/plugin.c' }));
    await screen.findByText(
      'Remove src/plugin.c from the plugin source? The deletion is applied when you save.'
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(screen.queryByRole('tab', { name: 'src/plugin.c' })).toBeNull());
    expect(latestState().files).not.toHaveProperty('src/plugin.c');
    expect(latestState().deleted).toEqual(['src/plugin.c']);
    // The active tab moved to the first remaining text file.
    expect(activeSpy).toHaveBeenLastCalledWith('meson.build');
    expect(screen.getByText('1 unsaved change')).toBeInTheDocument();
  });
});

describe('SourceEditor read-only mode (9.2)', () => {
  it('hides every mutating control and the assistant', () => {
    render(<Harness readOnly />);

    expect(screen.queryByRole('button', { name: 'Add file' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Delete / })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Code assistant' })).toBeNull();
    expect(sourceOf('src/plugin.c')).toHaveAttribute('readonly');
  });
});

describe('SourceEditor code assistant (5.6, 5.11)', () => {
  const RESPONSE = {
    code: '/* fixed */\n',
    notes: 'Fixed.',
    model_id: 'us.anthropic.test-model',
    contract: 'plugin_source',
  };

  it('sends the plugin_source contract with the other text files as context', async () => {
    codeAssist.mockResolvedValue(RESPONSE);
    render(<Harness />);

    fireEvent.change(promptInput(), { target: { value: 'Register a second pad' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));

    await screen.findByLabelText('Generated code for review');
    expect(lastRequest()).toMatchObject({
      usecase_id: 'uc-1',
      surface: 'node-designer',
      contract: 'plugin_source',
      current_code: PLUGIN_C,
      context: {
        kind: 'scaffold',
        active_file: 'src/plugin.c',
        parameters: [{ name: 'threshold', param_type: 'float' }],
        files: { 'meson.build': MESON, [HOOK_FILE]: HOOK_PY },
        file_paths: ['assets/model.bin', 'meson.build', HOOK_FILE, 'src/plugin.c'],
      },
    });
    // The active file is never duplicated into `files`.
    expect(lastRequest().context.files).not.toHaveProperty('src/plugin.c');
  });

  it('uses the frame_hook contract on the hook file', async () => {
    codeAssist.mockResolvedValue({ ...RESPONSE, contract: 'frame_hook' });
    render(<Harness />);

    fireEvent.click(tab(HOOK_FILE));
    await waitFor(() => expect(sourceOf(HOOK_FILE)).toBeInTheDocument());

    fireEvent.change(promptInput(), { target: { value: 'Blur faces' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));

    await screen.findByLabelText('Generated code for review');
    expect(lastRequest().contract).toBe('frame_hook');
    expect(lastRequest().context.active_file).toBe(HOOK_FILE);
  });

  it('applies an accepted proposal to the active file', async () => {
    codeAssist.mockResolvedValue(RESPONSE);
    render(<Harness />);

    fireEvent.change(promptInput(), { target: { value: 'Fix it' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
    await screen.findByLabelText('Generated code for review');

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));

    await waitFor(() => expect(sourceOf('src/plugin.c').value).toBe('/* fixed */\n'));
    expect(latestState().files['src/plugin.c']).toBe('/* fixed */\n');
    expect(tab('src/plugin.c •')).toBeInTheDocument();
  });

  it('lands a proposal for another Target_File in that file and switches to it (5.11)', async () => {
    codeAssist.mockResolvedValue({ ...RESPONSE, target_file: 'meson.build' });
    render(<Harness />);

    fireEvent.change(promptInput(), { target: { value: 'Link gstvideo' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
    await screen.findByText('Applies to meson.build');

    fireEvent.click(screen.getByRole('button', { name: 'Apply to meson.build' }));

    await waitFor(() => expect(activeSpy).toHaveBeenCalledWith('meson.build'));
    expect(latestState().files['meson.build']).toBe('/* fixed */\n');
    // The file being edited was left alone.
    expect(latestState().files['src/plugin.c']).toBe(PLUGIN_C);
  });

  it('creates the Target_File when the proposal names a file not yet in the tree', async () => {
    codeAssist.mockResolvedValue({ ...RESPONSE, target_file: 'src/util.c' });
    render(<Harness />);

    fireEvent.change(promptInput(), { target: { value: 'Split helpers out' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
    await screen.findByText('Applies to src/util.c');

    fireEvent.click(screen.getByRole('button', { name: 'Apply to src/util.c' }));

    await waitFor(() => expect(latestState().files['src/util.c']).toBe('/* fixed */\n'));
    expect(activeSpy).toHaveBeenCalledWith('src/util.c');
  });
});
