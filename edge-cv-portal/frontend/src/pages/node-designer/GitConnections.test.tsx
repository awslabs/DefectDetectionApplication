/**
 * Component tests for the Git connections page (custom-node-source-lifecycle
 * task 12.3; Requirements 2.1, 2.2, 2.4, 2.5, 2.7, 2.8, 2.9).
 *
 * Covers: the Use_Case-scoped table with provider, URL, branch, and
 * verification status; create sending the token exactly once and never
 * rendering it back (2.1, 2.4); client-side rejection of non-https or
 * malformed URLs before any request (2.2); the verifying → verified /
 * failed transitions through polling with the failure explained (2.5);
 * edit sending only changed fields and a blank token keeping the stored
 * one (2.7); Verify and Delete behind a confirmation (2.8); and Viewer
 * role gating (2.9).
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GitConnections, { CONNECTIONS_POLL_MS, changedFields, repoUrlError } from './GitConnections';
import type { GitConnection } from './types';

const {
  authRole,
  listUseCases,
  listGitConnections,
  createGitConnection,
  updateGitConnection,
  deleteGitConnection,
  verifyGitConnection,
} = vi.hoisted(() => ({
  authRole: { value: 'UseCaseAdmin' as string },
  listUseCases: vi.fn(),
  listGitConnections: vi.fn(),
  createGitConnection: vi.fn(),
  updateGitConnection: vi.fn(),
  deleteGitConnection: vi.fn(),
  verifyGitConnection: vi.fn(),
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      user_id: 'u-1',
      email: 'user@example.com',
      username: 'user',
      role: authRole.value,
      is_super_user: false,
    },
  }),
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('../../services/api', () => ({
  apiService: { listUseCases },
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    listGitConnections,
    createGitConnection,
    updateGitConnection,
    deleteGitConnection,
    verifyGitConnection,
  },
}));

// -------------------------------------------------------------- fixtures

const TOKEN = 'ghp_SECRET_TOKEN_VALUE_1234567890';

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

// --------------------------------------------------------------- helpers

async function renderPage() {
  render(<GitConnections />);
  await waitFor(() => expect(listGitConnections).toHaveBeenCalledWith('uc-1'));
  await act(async () => {});
}

const isDisabled = (button: HTMLElement) =>
  button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';

async function openCreateModal() {
  fireEvent.click(screen.getByRole('button', { name: 'Create connection' }));
  const heading = await screen.findByText('Create Git connection');
  return heading.closest('[role="dialog"]') as HTMLElement;
}

function fill(dialog: HTMLElement, name: string, value: string) {
  fireEvent.change(within(dialog).getByRole('textbox', { name }), { target: { value } });
}

beforeEach(() => {
  vi.clearAllMocks();
  authRole.value = 'UseCaseAdmin';
  listUseCases.mockResolvedValue({ usecases: [{ usecase_id: 'uc-1', name: 'Line 1' }] });
  listGitConnections.mockResolvedValue({ connections: [connection()], count: 1 });
});

afterEach(() => {
  vi.useRealTimers();
});

// ----------------------------------------------------------------- tests

describe('repoUrlError', () => {
  it('accepts https URLs and rejects everything else (2.2)', () => {
    expect(repoUrlError('https://github.com/acme/plugins.git')).toBeNull();
    expect(repoUrlError('  https://gitlab.example.com/acme/plugins ')).toBeNull();
    expect(repoUrlError('')).toBe('Enter the repository URL.');
    expect(repoUrlError('http://github.com/acme/plugins.git')).toBe('The repository URL must use https.');
    expect(repoUrlError('git@github.com:acme/plugins.git')).toMatch(/valid URL/);
    expect(repoUrlError('ssh://git@github.com/acme/plugins.git')).toBe('The repository URL must use https.');
    expect(repoUrlError('not a url')).toMatch(/valid URL/);
  });
});

describe('changedFields', () => {
  it('sends only what changed and the token only when entered (2.7)', () => {
    const current = connection();
    expect(
      changedFields(current, {
        name: 'plugins-repo',
        provider: 'github',
        repo_url: 'https://github.com/acme/plugins.git',
        default_branch: 'main',
        token: '',
      })
    ).toEqual({});
    expect(
      changedFields(current, {
        name: 'renamed',
        provider: 'gitlab',
        repo_url: 'https://gitlab.com/acme/plugins.git ',
        default_branch: 'develop',
        token: TOKEN,
      })
    ).toEqual({
      name: 'renamed',
      provider: 'gitlab',
      repo_url: 'https://gitlab.com/acme/plugins.git',
      default_branch: 'develop',
      token: TOKEN,
    });
  });
});

describe('GitConnections table', () => {
  it('lists the use case connections with provider, URL, branch, and status', async () => {
    listGitConnections.mockResolvedValue({
      connections: [
        connection(),
        connection({
          connection_id: 'c-2',
          name: 'gitlab-mirror',
          provider: 'gitlab',
          repo_url: 'https://gitlab.example.com/acme/mirror.git',
          default_branch: 'develop',
          status: 'failed',
          verification: { category: 'authentication', message: '401 Unauthorized' },
        }),
        connection({ connection_id: 'c-3', name: 'pending', status: 'verifying' }),
      ],
      count: 3,
    });
    await renderPage();

    expect(screen.getByText('plugins-repo')).toBeInTheDocument();
    // Two GitHub connections (plugins-repo, pending) and one GitLab. Scope
    // to the table: the (hidden) create modal's provider radios also read
    // GitHub / GitLab.
    const table = screen.getByRole('table');
    expect(within(table).getAllByText('GitHub')).toHaveLength(2);
    expect(within(table).getAllByText('GitLab')).toHaveLength(1);
    const repoLinks = within(table).getAllByRole('link', { name: /github.com\/acme\/plugins.git/ });
    expect(repoLinks).toHaveLength(2);
    expect(repoLinks[0]).toHaveAttribute('href', 'https://github.com/acme/plugins.git');
    expect(
      within(table).getByRole('link', { name: /gitlab.example.com\/acme\/mirror.git/ })
    ).toHaveAttribute('href', 'https://gitlab.example.com/acme/mirror.git');
    expect(screen.getByText('develop')).toBeInTheDocument();
    expect(screen.getByText('Verified')).toBeInTheDocument();
    expect(screen.getByText('Verifying')).toBeInTheDocument();

    // The failed status explains itself in plain language.
    fireEvent.click(screen.getByText('Failed'));
    await screen.findByText('Access token rejected');
    expect(screen.getByText('The Git host rejected the access token (401 Unauthorized).')).toBeInTheDocument();
  });

  it('shows the empty state per role', async () => {
    listGitConnections.mockResolvedValue({ connections: [], count: 0 });
    await renderPage();
    expect(
      screen.getByText('No Git connections yet. Create one to push plugin source to a repository.')
    ).toBeInTheDocument();
  });
});

describe('GitConnections create', () => {
  it('sends the token exactly once and never renders it back (2.1, 2.4)', async () => {
    createGitConnection.mockResolvedValue({
      connection: connection({ connection_id: 'c-9', name: 'new-repo', status: 'verifying' }),
      operation: { operation_id: 'op-1', kind: 'verify', status: 'queued' },
    });
    await renderPage();
    const dialog = await openCreateModal();

    fill(dialog, 'Connection name', 'new-repo');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'GitLab' }));
    fill(dialog, 'Repository URL', 'https://gitlab.com/acme/new.git');
    fill(dialog, 'Default branch', 'trunk');
    const tokenInput = within(dialog).getByLabelText('Access token') as HTMLInputElement;
    expect(tokenInput).toHaveAttribute('type', 'password');
    fireEvent.change(tokenInput, { target: { value: TOKEN } });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() =>
      expect(createGitConnection).toHaveBeenCalledWith({
        usecase_id: 'uc-1',
        name: 'new-repo',
        provider: 'gitlab',
        repo_url: 'https://gitlab.com/acme/new.git',
        default_branch: 'trunk',
        token: TOKEN,
      })
    );
    expect(createGitConnection).toHaveBeenCalledTimes(1);
    // The new connection shows as verifying; the token is nowhere in the DOM.
    await screen.findByText('new-repo');
    expect(screen.getByText('Verifying')).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(TOKEN);
    await waitFor(() => expect(dialog.className).toContain('hidden'));
  });

  it('rejects a non-https or malformed URL before any request (2.2)', async () => {
    await renderPage();
    const dialog = await openCreateModal();

    fill(dialog, 'Connection name', 'bad-url');
    fill(dialog, 'Repository URL', 'http://github.com/acme/plugins.git');
    expect(screen.getByText('The repository URL must use https.')).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText('Access token'), { target: { value: TOKEN } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(createGitConnection).not.toHaveBeenCalled();

    fill(dialog, 'Repository URL', 'git@github.com:acme/plugins.git');
    expect(screen.getByText(/Enter a valid URL/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(createGitConnection).not.toHaveBeenCalled();
  });

  it('requires a token on create and shows the backend error inline', async () => {
    createGitConnection.mockRejectedValue(new Error('Repository URL already connected'));
    await renderPage();
    const dialog = await openCreateModal();

    fill(dialog, 'Connection name', 'dup');
    fill(dialog, 'Repository URL', 'https://github.com/acme/plugins.git');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(screen.getByText('Enter an access token.')).toBeInTheDocument();
    expect(createGitConnection).not.toHaveBeenCalled();

    fireEvent.change(within(dialog).getByLabelText('Access token'), { target: { value: TOKEN } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));

    await screen.findByText('Repository URL already connected');
    expect(createGitConnection).toHaveBeenCalledTimes(1);
  });

  it('polls while a connection is verifying until it settles (2.5)', async () => {
    vi.useFakeTimers();
    listGitConnections
      .mockResolvedValueOnce({ connections: [connection({ status: 'verifying' })], count: 1 })
      .mockResolvedValueOnce({ connections: [connection({ status: 'verifying' })], count: 1 })
      .mockResolvedValue({
        connections: [
          connection({
            status: 'failed',
            verification: { category: 'not_found', message: 'repository not found' },
          }),
        ],
        count: 1,
      });
    render(<GitConnections />);
    await act(async () => {});
    await act(async () => {});
    expect(listGitConnections).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Verifying')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONNECTIONS_POLL_MS);
    });
    expect(listGitConnections).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Verifying')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONNECTIONS_POLL_MS);
    });
    expect(listGitConnections).toHaveBeenCalledTimes(3);
    expect(screen.getByText('Failed')).toBeInTheDocument();

    // Settled: polling stops.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONNECTIONS_POLL_MS * 3);
    });
    expect(listGitConnections).toHaveBeenCalledTimes(3);
  });
});

describe('GitConnections edit, verify, delete', () => {
  it('edits with the token blank keeping the stored one and sends only changed fields (2.7)', async () => {
    updateGitConnection.mockResolvedValue({
      connection: connection({ name: 'renamed', default_branch: 'develop' }),
    });
    await renderPage();

    fireEvent.click(screen.getByRole('button', { name: 'Edit plugins-repo' }));
    const heading = await screen.findByText('Edit plugins-repo');
    const dialog = heading.closest('[role="dialog"]') as HTMLElement;

    // The token field is never pre-filled.
    expect((within(dialog).getByLabelText('Access token') as HTMLInputElement).value).toBe('');
    expect(screen.getByText(/Leave blank to keep the stored token/)).toBeInTheDocument();

    fill(dialog, 'Connection name', 'renamed');
    fill(dialog, 'Default branch', 'develop');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(updateGitConnection).toHaveBeenCalledWith('c-1', { name: 'renamed', default_branch: 'develop' })
    );
    expect(updateGitConnection.mock.calls[0][1]).not.toHaveProperty('token');
    await screen.findByText('renamed');
  });

  it('a new token on edit is sent once and the connection re-verifies (2.7)', async () => {
    updateGitConnection.mockResolvedValue({
      connection: connection({ status: 'verifying' }),
      operation: { operation_id: 'op-2', kind: 'verify', status: 'queued' },
    });
    await renderPage();

    fireEvent.click(screen.getByRole('button', { name: 'Edit plugins-repo' }));
    const dialog = (await screen.findByText('Edit plugins-repo')).closest('[role="dialog"]') as HTMLElement;
    fireEvent.change(within(dialog).getByLabelText('Access token'), { target: { value: TOKEN } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(updateGitConnection).toHaveBeenCalledWith('c-1', { token: TOKEN }));
    await screen.findByText('Verifying');
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it('Verify starts a verification and shows the connection verifying (2.5)', async () => {
    listGitConnections.mockResolvedValue({
      connections: [connection({ status: 'failed', verification: { category: 'unreachable', message: 'timeout' } })],
      count: 1,
    });
    verifyGitConnection.mockResolvedValue({
      connection: connection({ status: 'verifying' }),
      operation: { operation_id: 'op-3', kind: 'verify', status: 'queued' },
    });
    await renderPage();

    fireEvent.click(screen.getByRole('button', { name: 'Verify plugins-repo' }));

    await waitFor(() => expect(verifyGitConnection).toHaveBeenCalledWith('c-1'));
    await screen.findByText('Verifying');
    expect(isDisabled(screen.getByRole('button', { name: 'Verify plugins-repo' }))).toBe(true);
  });

  it('deletes behind a confirmation and removes the row (2.8)', async () => {
    deleteGitConnection.mockResolvedValue({ deleted: true, connection_id: 'c-1' });
    await renderPage();

    fireEvent.click(screen.getByRole('button', { name: 'Delete plugins-repo' }));
    const message = await screen.findByText(/Delete the Git connection "plugins-repo"\?/);
    const dialog = message.closest('[role="dialog"]') as HTMLElement;

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(dialog.className).toContain('hidden'));
    expect(deleteGitConnection).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Delete plugins-repo' }));
    await screen.findByText(/Delete the Git connection "plugins-repo"\?/);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(deleteGitConnection).toHaveBeenCalledWith('c-1'));
    await waitFor(() => expect(screen.queryByText('plugins-repo')).toBeNull());
  });

  it('surfaces a delete failure in the page alert', async () => {
    deleteGitConnection.mockRejectedValue(new Error('Connection is linked to 2 versions'));
    await renderPage();

    fireEvent.click(screen.getByRole('button', { name: 'Delete plugins-repo' }));
    const dialog = (await screen.findByText(/Delete the Git connection/)).closest('[role="dialog"]') as HTMLElement;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await screen.findByText('Connection is linked to 2 versions');
    expect(screen.getByText('plugins-repo')).toBeInTheDocument();
  });
});

describe('GitConnections role gating (2.9)', () => {
  it('Viewers see the table but no mutating actions', async () => {
    authRole.value = 'Viewer';
    await renderPage();

    expect(screen.getByText('plugins-repo')).toBeInTheDocument();
    expect(screen.getByText('Verified')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Verify / })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Delete / })).toBeNull();
    expect(screen.queryByText('Actions')).toBeNull();
  });

  it('PortalAdmins may mutate', async () => {
    authRole.value = 'PortalAdmin';
    await renderPage();

    expect(screen.getByRole('button', { name: 'Create connection' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit plugins-repo' })).toBeInTheDocument();
  });
});
