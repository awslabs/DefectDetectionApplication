/**
 * Component tests for the GitSyncPanel (custom-node-source-lifecycle task
 * 12.3; Requirements 2.6, 2.8, 3.1, 3.2, 3.7, 3.8, 3.12, 3.13, 4.1, 4.2,
 * 4.10, 4.11, 9.2).
 *
 * Covers: the unlinked state offering only verified Git_Connections with
 * Repository_Path validation and the link request (2.8, 3.1); the linked
 * summary with the last sync's commit link (3.10); Push with a message,
 * blocked behind a modal while the editor is dirty (3.2, 3.13); the
 * overwrite checkbox revealed only after a `diverged` failure and sent as
 * `force` (3.7, 3.8); Pull mode defaults per lifecycle with `in_place`
 * disabled outside dev (4.1, 4.2); polling an in-flight Sync_Operation
 * until it settles and notifying the owner (4.10); plain-language failure
 * explanations in the history (3.12, 4.11); the disconnected warning when
 * the linked connection is gone (2.6); Unlink behind a confirmation; and
 * the read-only view (9.2).
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GitSyncPanel, { SYNC_POLL_MS, commitUrl } from './GitSyncPanel';
import type { GitConnection, PluginVersionDetail, SyncOperation } from './types';

const {
  listGitConnections,
  listSyncOperations,
  getSyncOperation,
  setGitLink,
  removeGitLink,
  pushGit,
  pullGit,
} = vi.hoisted(() => ({
  listGitConnections: vi.fn(),
  listSyncOperations: vi.fn(),
  getSyncOperation: vi.fn(),
  setGitLink: vi.fn(),
  removeGitLink: vi.fn(),
  pushGit: vi.fn(),
  pullGit: vi.fn(),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    listGitConnections,
    listSyncOperations,
    getSyncOperation,
    setGitLink,
    removeGitLink,
    pushGit,
    pullGit,
  },
}));

// -------------------------------------------------------------- fixtures

function connection(overrides: Partial<GitConnection> = {}): GitConnection {
  return {
    connection_id: 'c-1',
    usecase_id: 'uc-1',
    name: 'plugins-repo',
    provider: 'github',
    repo_url: 'https://github.com/acme/plugins.git',
    default_branch: 'main',
    status: 'verified',
    verification: { at: 1 },
    created_by: 'user-1',
    created_at: 1,
    updated_at: 1,
    ...overrides,
  };
}

function plugin(overrides: Partial<PluginVersionDetail> = {}): PluginVersionDetail {
  return {
    plugin_id: 'p-1',
    version: 1,
    usecase_id: 'uc-1',
    name: 'my-scaffold',
    description: '',
    kind: 'scaffold',
    deepstream: false,
    provenance: {},
    lifecycle_state: 'dev',
    review: { decision: 'pending' },
    artifacts: {},
    component: {},
    source_s3_prefix: 'plugin-sources/uc-1/p-1/1/',
    source_revision: 3,
    stale_architectures: [],
    created_by: 'user-1',
    created_at: 1,
    updated_at: 1,
    ...overrides,
  };
}

const LINKED = plugin({
  git: {
    connection_id: 'c-1',
    branch: 'main',
    path: 'plugins/my-scaffold',
    linked_by: 'user-1',
    linked_at: 1,
    last_sync: {
      kind: 'push',
      commit: 'abcdef1234567890',
      branch: 'main',
      path: 'plugins/my-scaffold',
      source_revision: 3,
      by: 'user-1',
      at: 1_700_000_000_000,
    },
  },
});

function operation(overrides: Partial<SyncOperation> = {}): SyncOperation {
  return {
    operation_id: 'op-1',
    usecase_id: 'uc-1',
    connection_id: 'c-1',
    plugin_id: 'p-1',
    version: 1,
    kind: 'push',
    target: { branch: 'main', path: 'plugins/my-scaffold' },
    status: 'succeeded',
    started_by: 'user-1',
    started_at: 1_700_000_000_000,
    finished_at: 1_700_000_010_000,
    result: { commit: 'abcdef1234567890', files: 3 },
    ...overrides,
  };
}

// --------------------------------------------------------------- helpers

const onSettled = vi.fn();

function renderPanel(record: PluginVersionDetail, props: { editorDirty?: boolean; readOnly?: boolean } = {}) {
  return render(
    <GitSyncPanel
      plugin={record}
      editorDirty={props.editorDirty ?? false}
      readOnly={props.readOnly ?? false}
      onSettled={onSettled}
    />
  );
}

/** Flush the mount-time loads (connections + history). */
async function settle() {
  await waitFor(() => expect(listGitConnections).toHaveBeenCalled());
  await waitFor(() => expect(listSyncOperations).toHaveBeenCalled());
  await act(async () => {});
}

const isDisabled = (button: HTMLElement) =>
  button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';

async function selectOption(triggerName: string | RegExp, optionLabel: string | RegExp) {
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
  listGitConnections.mockResolvedValue({ connections: [connection()], count: 1 });
  listSyncOperations.mockResolvedValue({ operations: [], count: 0 });
});

afterEach(() => {
  vi.useRealTimers();
});

// ----------------------------------------------------------------- tests

describe('commitUrl', () => {
  it('builds provider commit URLs and returns null for unknown hosts', () => {
    expect(commitUrl('https://github.com/acme/plugins.git', 'abc')).toBe(
      'https://github.com/acme/plugins/commit/abc'
    );
    expect(commitUrl('https://gitlab.example.com/acme/plugins/', 'abc')).toBe(
      'https://gitlab.example.com/acme/plugins/-/commit/abc'
    );
    expect(commitUrl('https://git.example.org/acme/plugins.git', 'abc')).toBeNull();
    expect(commitUrl(undefined, 'abc')).toBeNull();
    expect(commitUrl('https://github.com/acme/plugins', undefined)).toBeNull();
  });
});

describe('GitSyncPanel unlinked', () => {
  it('offers only verified connections of the use case and links with branch and path (2.8, 3.1)', async () => {
    listGitConnections.mockResolvedValue({
      connections: [
        connection(),
        connection({ connection_id: 'c-2', name: 'pending-repo', status: 'verifying' }),
        connection({ connection_id: 'c-3', name: 'broken-repo', status: 'failed' }),
      ],
      count: 3,
    });
    setGitLink.mockResolvedValue({ git: { connection_id: 'c-1', branch: 'dev', path: 'nodes/demo' } });
    renderPanel(plugin());
    await settle();

    expect(listGitConnections).toHaveBeenCalledWith('uc-1');
    expect(isDisabled(screen.getByRole('button', { name: 'Link repository' }))).toBe(true);

    const trigger = screen.getByRole('button', { name: /Select a verified connection/ });
    fireEvent.mouseDown(trigger);
    fireEvent.click(trigger);
    const options = await screen.findAllByRole('option');
    expect(options).toHaveLength(1);
    expect(screen.getByRole('option', { name: /plugins-repo/ })).toBeInTheDocument();
    const option = screen.getByRole('option', { name: /plugins-repo/ });
    fireEvent.mouseDown(option);
    fireEvent.mouseUp(option);
    fireEvent.click(option);

    fireEvent.change(screen.getByRole('textbox', { name: 'Branch' }), { target: { value: 'dev' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Repository path' }), {
      target: { value: 'nodes/demo' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Link repository' }));

    await waitFor(() =>
      expect(setGitLink).toHaveBeenCalledWith('p-1', 1, {
        connection_id: 'c-1',
        branch: 'dev',
        path: 'nodes/demo',
      })
    );
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
  });

  it('rejects a Repository_Path with ".." segments (2.8)', async () => {
    renderPanel(plugin());
    await settle();
    await selectOption(/Select a verified connection/, /plugins-repo/);

    fireEvent.change(screen.getByRole('textbox', { name: 'Repository path' }), {
      target: { value: '../outside' },
    });

    expect(
      screen.getByText('Use a relative repository path without ".." segments.')
    ).toBeInTheDocument();
    expect(isDisabled(screen.getByRole('button', { name: 'Link repository' }))).toBe(true);
    expect(setGitLink).not.toHaveBeenCalled();
  });

  it('shows a link error and stays unlinked when the request fails', async () => {
    setGitLink.mockRejectedValue(new Error('Connection is not verified'));
    renderPanel(plugin());
    await settle();
    await selectOption(/Select a verified connection/, /plugins-repo/);

    fireEvent.click(screen.getByRole('button', { name: 'Link repository' }));

    await screen.findByText('Connection is not verified');
    expect(onSettled).not.toHaveBeenCalled();
  });

  it('read-only roles see the hint but no link form (9.2)', async () => {
    renderPanel(plugin(), { readOnly: true });
    await settle();

    expect(
      screen.getByText('Link this version to a repository to push its source or pull changes back.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Link repository' })).toBeNull();
  });
});

describe('GitSyncPanel linked', () => {
  it('summarizes the link and the last sync with a commit link (3.10)', async () => {
    renderPanel(LINKED);
    await settle();

    expect(screen.getByText('plugins-repo')).toBeInTheDocument();
    expect(screen.getByText('main')).toBeInTheDocument();
    expect(screen.getByText('plugins/my-scaffold')).toBeInTheDocument();
    const sha = screen.getByRole('link', { name: /abcdef1234/ });
    expect(sha).toHaveAttribute('href', 'https://github.com/acme/plugins/commit/abcdef1234567890');
    expect(screen.getByRole('button', { name: 'Push' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pull' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unlink repository' })).toBeInTheDocument();
    expect(screen.getByText('No sync operations yet.')).toBeInTheDocument();
  });

  it('pushes with the commit message and prepends the operation (3.2)', async () => {
    pushGit.mockResolvedValue({
      operation: operation({ operation_id: 'op-9', status: 'queued', result: null }),
    });
    renderPanel(LINKED);
    await settle();

    fireEvent.change(screen.getByRole('textbox', { name: 'Commit message' }), {
      target: { value: 'Tune threshold' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Push' }));

    await waitFor(() => expect(pushGit).toHaveBeenCalledWith('p-1', 1, { message: 'Tune threshold' }));
    await screen.findByText('queued');
    // A queued operation blocks further pushes and pulls.
    expect(isDisabled(screen.getByRole('button', { name: 'Push' }))).toBe(true);
    expect(isDisabled(screen.getByRole('button', { name: 'Pull' }))).toBe(true);
    // Nothing settled yet.
    expect(onSettled).not.toHaveBeenCalled();
  });

  it('blocks Push behind a modal while the editor holds unsaved edits (3.13)', async () => {
    renderPanel(LINKED, { editorDirty: true });
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'Push' }));

    await screen.findByText(
      'Save or discard your source edits before pushing; a push sends the saved source tree, not the editor contents.'
    );
    expect(pushGit).not.toHaveBeenCalled();
  });

  it('reveals the overwrite option only after a diverged failure and sends force (3.7, 3.8)', async () => {
    listSyncOperations.mockResolvedValue({
      operations: [
        operation({
          operation_id: 'op-2',
          status: 'failed',
          result: null,
          failure: {
            category: 'diverged',
            message: 'remote changed',
            changed_files: ['src/plugin.c'],
          },
        }),
      ],
      count: 1,
    });
    pushGit.mockResolvedValue({
      operation: operation({ operation_id: 'op-3', status: 'queued', result: null, target: { force: true } }),
    });
    renderPanel(LINKED);
    await settle();

    const overwrite = await screen.findByRole('checkbox', {
      name: 'Overwrite repository changes under plugins/my-scaffold',
    });
    fireEvent.click(overwrite);
    fireEvent.click(screen.getByRole('button', { name: 'Push' }));

    await waitFor(() => expect(pushGit).toHaveBeenCalledWith('p-1', 1, { force: true }));
    // The history explains the divergence in plain language.
    expect(screen.getByText('diverged')).toBeInTheDocument();
    await screen.findByText('overwrite');
  });

  it('has no overwrite option without a diverged failure (3.7)', async () => {
    renderPanel(LINKED);
    await settle();

    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('pulls in place by default on dev versions (4.1)', async () => {
    pullGit.mockResolvedValue({
      operation: operation({ operation_id: 'op-4', kind: 'pull', status: 'queued', result: null }),
    });
    renderPanel(LINKED);
    await settle();

    const inPlace = screen.getByRole('radio', { name: /Replace this version’s source/ });
    expect(inPlace).toBeChecked();
    expect(inPlace).not.toBeDisabled();

    fireEvent.change(screen.getByRole('textbox', { name: 'Ref' }), { target: { value: 'v1.2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));

    await waitFor(() => expect(pullGit).toHaveBeenCalledWith('p-1', 1, { mode: 'in_place', ref: 'v1.2' }));
  });

  it('forces new_version outside dev with in_place disabled (4.2)', async () => {
    pullGit.mockResolvedValue({
      operation: operation({ operation_id: 'op-5', kind: 'pull', status: 'queued', result: null }),
    });
    renderPanel({ ...LINKED, lifecycle_state: 'test' });
    await settle();

    expect(screen.getByRole('radio', { name: /Replace this version’s source/ })).toBeDisabled();
    expect(screen.getByRole('radio', { name: /Create a new version/ })).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));

    await waitFor(() => expect(pullGit).toHaveBeenCalledWith('p-1', 1, { mode: 'new_version' }));
  });

  it('polls an in-flight operation until it settles and notifies the owner (4.10)', async () => {
    vi.useFakeTimers();
    listSyncOperations.mockResolvedValue({
      operations: [operation({ operation_id: 'op-6', status: 'running', result: null })],
      count: 1,
    });
    getSyncOperation
      .mockResolvedValueOnce({ operation: operation({ operation_id: 'op-6', status: 'running', result: null }) })
      .mockResolvedValueOnce({
        operation: operation({ operation_id: 'op-6', status: 'succeeded', result: { commit: 'feedfacecafebeef', files: 2 } }),
      });
    renderPanel(LINKED);
    await act(async () => {});

    expect(screen.getByText('running')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SYNC_POLL_MS);
    });
    expect(getSyncOperation).toHaveBeenCalledWith('op-6');
    expect(onSettled).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SYNC_POLL_MS);
    });
    expect(getSyncOperation).toHaveBeenCalledTimes(2);
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onSettled.mock.calls[0][0]).toMatchObject({ operation_id: 'op-6', status: 'succeeded' });
    expect(screen.getByText('succeeded')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /feedfacec/ })).toBeInTheDocument();

    // Settled: no further polls.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SYNC_POLL_MS * 2);
    });
    expect(getSyncOperation).toHaveBeenCalledTimes(2);
  });

  it('explains failures in plain language and shows pull results (3.12, 4.11)', async () => {
    listSyncOperations.mockResolvedValue({
      operations: [
        operation({
          operation_id: 'op-7',
          kind: 'pull',
          status: 'failed',
          result: null,
          failure: {
            category: 'invalid_source',
            message: 'tree rejected',
            defects: ['missing meson.build'],
          },
        }),
        operation({
          operation_id: 'op-8',
          kind: 'pull',
          status: 'succeeded',
          result: { commit: '0123456789abcdef', version: 2, mode: 'new_version' },
        }),
        operation({ operation_id: 'op-9', status: 'succeeded', result: { no_changes: true } }),
      ],
      count: 3,
    });
    renderPanel(LINKED);
    await settle();

    expect(screen.getByText('invalid_source')).toBeInTheDocument();
    fireEvent.click(screen.getByText('invalid_source'));
    await screen.findByText('Pulled source rejected');
    expect(
      screen.getByText('The pulled tree cannot be installed (tree rejected). Defects: missing meson.build.')
    ).toBeInTheDocument();
    expect(screen.getByText('v2 (new version)')).toBeInTheDocument();
    expect(screen.getByText('no changes')).toBeInTheDocument();
  });

  it('warns when the linked connection no longer exists and hides push/pull (2.6)', async () => {
    listGitConnections.mockResolvedValue({ connections: [], count: 0 });
    renderPanel(LINKED);
    await settle();

    expect(screen.getByText('Disconnected')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Push' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Pull' })).toBeNull();
    // Re-linking elsewhere goes through Unlink first.
    expect(screen.getByRole('button', { name: 'Unlink repository' })).toBeInTheDocument();
  });

  it('disables push and pull while the connection is not verified', async () => {
    listGitConnections.mockResolvedValue({
      connections: [connection({ status: 'failed' })],
      count: 1,
    });
    renderPanel(LINKED);
    await settle();

    expect(screen.getByText('Connection not verified')).toBeInTheDocument();
    expect(isDisabled(screen.getByRole('button', { name: 'Push' }))).toBe(true);
    expect(isDisabled(screen.getByRole('button', { name: 'Pull' }))).toBe(true);
  });

  it('unlinks behind a confirmation and notifies the owner', async () => {
    removeGitLink.mockResolvedValue({ git: null });
    renderPanel(LINKED);
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'Unlink repository' }));
    const message = await screen.findByText(/Remove the Git link from this version\?/);
    const dialog = message.closest('[role="dialog"]') as HTMLElement;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Unlink' }));

    await waitFor(() => expect(removeGitLink).toHaveBeenCalledWith('p-1', 1));
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
  });

  it('read-only roles see the summary and history but no actions (9.2)', async () => {
    renderPanel(LINKED, { readOnly: true });
    await settle();

    expect(screen.getByText('plugins-repo')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Push' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Pull' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Unlink repository' })).toBeNull();
  });
});
